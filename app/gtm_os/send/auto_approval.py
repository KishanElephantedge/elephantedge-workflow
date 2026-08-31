"""Approval -> send coupling, and the unattended-approval window.

TWO BEHAVIOURS, BOTH REQUESTED EXPLICITLY (2026-08-31):

1. Approving a draft sends it IMMEDIATELY. Before this, the only caller of the send path was the
   once-daily sweep, so an approval could sit unsent for up to 24 hours -- confirmed live: a draft
   approved at 03:19 was still unsent hours later, because the next run hung before reaching its
   send stage. Nothing was broken; the send simply had no trigger of its own.

2. A draft nobody reviews within AUTO_APPROVAL_HOURS is approved and sent on its own. The human
   review step becomes an opportunity to intervene rather than a requirement to proceed, so an
   unattended day does not silently stop all outbound.

WHY THIS IS A SEPARATE MODULE, not a change to approve_message_draft(): that function documents
itself as "the ONLY function that ever sets status='approved'... Does not send, execute, or write
to any external system -- purely a state transition on this row." That separation is worth
keeping -- state transition and external side effect stay independently testable, and approval
still cannot half-happen because a provider was down. This module composes the two rather than
entangling them.

SAFETY: an auto-approved draft is recorded as approved by AUTO_APPROVAL_ACTOR, never by a person
who did not look at it. Anyone auditing later can tell exactly which messages a human actually
read and which went out on the timer.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.gtm_os.learning.message_draft import MessageDraft, approve_message_draft
from app.gtm_os.send.send import send_message_draft

logger = logging.getLogger(__name__)

# How long a draft may sit in ready_for_review before it is approved unattended.
AUTO_APPROVAL_HOURS = 2

# Recorded as approved_by/reviewed_by so an unattended approval is never mistaken for a human one.
AUTO_APPROVAL_ACTOR = "system:auto-approval"


def send_approved_draft(db: Session, tenant_id: int, draft: MessageDraft) -> dict:
    """Sends one already-approved draft. Never raises -- a provider failure must not undo the
    approval or break the caller (an API request or a sweep tick)."""
    if draft.status != "approved":
        return {"status": "skipped", "reason": f"draft {draft.id} is {draft.status!r}, not approved"}
    try:
        return send_message_draft(db, tenant_id, draft)
    except Exception as e:  # noqa: BLE001 -- see docstring
        db.rollback()
        logger.exception("send_approved_draft: draft_id=%s failed", draft.id)
        return {"status": "failed", "error": str(e)}


def approve_and_send(db: Session, tenant_id: int, message_draft_id: int, approved_by: str) -> dict:
    """Approve, then immediately push to the offering's campaign.

    Approval is committed FIRST and independently: if the provider is unreachable, the draft
    stays approved and the next sweep picks it up, rather than the approval being lost because a
    send failed."""
    draft = approve_message_draft(db, tenant_id, message_draft_id, approved_by=approved_by)
    send_result = send_approved_draft(db, tenant_id, draft)
    logger.info("approve_and_send: draft_id=%s approved_by=%r send=%s", message_draft_id, approved_by, send_result.get("status"))
    return {"draft_id": draft.id, "status": draft.status, "send": send_result}


def run_auto_approval_sweep(db: Session, tenant_id: int, hours: int = AUTO_APPROVAL_HOURS, limit: int = 50) -> dict:
    """Approves and sends drafts left unreviewed longer than `hours`.

    Age is measured from created_at -- when the draft was put in front of a human -- not from
    last_updated_at, which an unrelated edit would reset and thereby restart the clock forever.

    Only ready_for_review drafts are eligible, so this can never promote a draft that failed the
    quality gate ("draft"/"insufficient_context") or resurrect a rejected one."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    stale = (
        db.query(MessageDraft)
        .filter(
            MessageDraft.tenant_id == tenant_id,
            MessageDraft.status == "ready_for_review",
            MessageDraft.created_at <= cutoff,
        )
        .order_by(MessageDraft.created_at)
        .limit(limit)
        .all()
    )
    result = {"status": "succeeded", "window_hours": hours, "eligible": len(stale), "approved": 0, "sent": 0, "failed": 0, "details": []}
    for draft in stale:
        try:
            outcome = approve_and_send(db, tenant_id, draft.id, approved_by=AUTO_APPROVAL_ACTOR)
            result["approved"] += 1
            if outcome["send"].get("status") in ("enrolled", "succeeded", "request_submitted"):
                result["sent"] += 1
            result["details"].append({"draft_id": draft.id, "send": outcome["send"].get("status"), "reason": outcome["send"].get("reason") or outcome["send"].get("error")})
        except Exception as e:  # noqa: BLE001 -- one draft must never abort the sweep
            db.rollback()
            result["failed"] += 1
            logger.error("run_auto_approval_sweep: draft_id=%s failed -- %s", draft.id, e)
    if result["approved"]:
        logger.info("run_auto_approval_sweep: %s", {k: v for k, v in result.items() if k != "details"})
    return result
