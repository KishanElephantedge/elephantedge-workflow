"""V2 Multi-Contact Outreach Sequencing -- Phase 8.

Decides ONLY "which contact/message is eligible next for this Opportunity" -- it never talks to
a provider itself. Once it determines a fallback contact should get a draft, it calls the exact
same, unmodified app.gtm_os.learning.message_draft.generate_message_draft() Phase 3-7 already
use (with the new optional exclude_contact_ids param), and stops there -- the resulting draft
still needs real human approval and still goes through Phase 7's send layer exactly like the
primary's did. This keeps provider adapters / send safety checks / retry logic (Phase 7)
completely separate from fallback sequencing (this module), per explicit instruction.

STATE: no new persisted state beyond what Phases 3-7 already built. "Which contact is currently
active," "has this contact been sent to," "is a fallback eligible" are all DERIVED, every call,
from real existing data:
  - contact ranking             -> app.gtm_os.sales.contact_ranking (Phase 3/4, unmodified)
  - eligible/suppressed contacts -> Contact.excluded_from_push (unmodified)
  - was a contact sent to, when, how -> app.gtm_os.send.send_state.MessageSendAttempt (Phase 7)
  - hard failure                -> MessageSendAttempt.status=="failed" and retryable is False (Phase 7)
  - positive/negative/ambiguous reply -> app.gtm_os.learning.outcome.SalesOutcome (existing, Batch 7)
  - meeting booked               -> CalendarBooking.outcome_opportunity_id (existing, V2 Revenue Pace)
The only genuinely new persisted thing is where a fallback contact's draft itself lives -- the
existing message_drafts table, now allowed one row per contact per strategy version instead of
one row total (see the partial unique indexes in app/db/session.py's ensure_indexes()).

REAL DATA LIMITATION, reported rather than papered over: CalendarBooking.outcome_opportunity_id
is set by a HUMAN recording a meeting outcome (app/gtm_os/revenue/revenue_pace.py), not detected
automatically the instant a booking happens. "Meeting booked" as an account-level stop signal is
real, but only as fresh as whenever that manual step occurs -- there is no automatic
booker_email -> Contact/Opportunity resolution anywhere in this codebase to close that gap.

OUTCOME TREATMENT, a real judgment call flagged here rather than silently assumed: SalesOutcome
has four real categories (reply, positive_reply, not_interested, timing_deferral) -- the phase
spec explicitly named "positive" and "negative" but said nothing about a bare "reply" or
"timing_deferral". Rather than guess whether those count as "no response" (eligible for
fallback) or "a response" (stop), this module treats ANY SalesOutcome row for a contact as a
real, live human engagement and stops sequencing for that contact -- a generic or deferred reply
means someone is genuinely in a conversation with them; auto-falling-back to a different person
at the same company while that's happening is the more dangerous failure mode. Only the STOP
REASON text distinguishes "positive_reply" from the others."""
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import CalendarBooking, Contact
from app.gtm_os.learning.message_draft import MessageDraft, generate_message_draft
from app.gtm_os.learning.outcome import SalesOutcome
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run, get_control_config
from app.gtm_os.sales.contact_discovery import get_eligible_contacts
from app.gtm_os.sales.contact_ranking import rank_contacts
from app.gtm_os.send.send_state import MessageSendAttempt, SUCCESS_STATUSES
from app.gtm_os.strategy.strategy import GtmStrategy

POSITIVE_OUTCOME_CATEGORIES = {"positive_reply"}


def _latest_strategy(db: Session, tenant_id: int, opportunity_id: int) -> GtmStrategy | None:
    return (
        db.query(GtmStrategy)
        .filter(GtmStrategy.tenant_id == tenant_id, GtmStrategy.opportunity_id == opportunity_id)
        .order_by(GtmStrategy.id.desc())
        .first()
    )


def _meeting_booked(db: Session, opportunity_id: int) -> bool:
    return (
        db.query(CalendarBooking.id)
        .filter(CalendarBooking.outcome_opportunity_id == opportunity_id)
        .first()
        is not None
    )


def _outcome_for_contact(db: Session, tenant_id: int, opportunity_id: int, contact_id: int) -> SalesOutcome | None:
    return (
        db.query(SalesOutcome)
        .filter(SalesOutcome.tenant_id == tenant_id, SalesOutcome.opportunity_id == opportunity_id, SalesOutcome.contact_id == contact_id)
        .order_by(SalesOutcome.observed_at.desc().nullslast(), SalesOutcome.id.desc())
        .first()
    )


def _draft_for_contact(db: Session, tenant_id: int, opportunity_id: int, strategy_id: int, contact_id: int) -> MessageDraft | None:
    return (
        db.query(MessageDraft)
        .filter(MessageDraft.tenant_id == tenant_id, MessageDraft.opportunity_id == opportunity_id, MessageDraft.gtm_strategy_id == strategy_id, MessageDraft.contact_id == contact_id)
        .first()
    )


def _last_successful_attempt(db: Session, message_draft_id: int) -> MessageSendAttempt | None:
    return (
        db.query(MessageSendAttempt)
        .filter(MessageSendAttempt.message_draft_id == message_draft_id, MessageSendAttempt.status.in_(SUCCESS_STATUSES))
        .order_by(MessageSendAttempt.attempted_at.desc())
        .first()
    )


def _last_attempt(db: Session, message_draft_id: int) -> MessageSendAttempt | None:
    return (
        db.query(MessageSendAttempt)
        .filter(MessageSendAttempt.message_draft_id == message_draft_id)
        .order_by(MessageSendAttempt.attempted_at.desc())
        .first()
    )


def evaluate_contact_outcome(db: Session, tenant_id: int, opportunity: Opportunity, contact: Contact, draft: MessageDraft | None, fallback_delay_hours: int, now: datetime | None = None) -> dict:
    """Returns the real, current state of outreach to ONE contact for ONE opportunity:
    {"state": ..., "reason": str}. `state` is one of: "never_sent" (no draft/send yet --
    nothing for sequencing to act on, someone else's job), "pending_send" (draft exists,
    approved or not, but no successful send attempt yet -- Phase 7's job, not ours),
    "positive" / "meeting_booked" (account-level stop), "negative" / "engaged" (contact-level
    stop, no auto-fallback), "suppressed" (contact-level stop, move to next),
    "hard_failure" (contact-level stop, move to next immediately), "awaiting_response"
    (successful send, no outcome yet, fallback delay not elapsed -- wait),
    "no_response_timeout" (successful send, no outcome yet, fallback delay elapsed -- move to
    next)."""
    now = now or datetime.utcnow()

    if contact.excluded_from_push:
        return {"state": "suppressed", "reason": "contact is suppressed (excluded_from_push)"}

    if draft is None:
        return {"state": "never_sent", "reason": "no draft exists yet for this contact"}

    if _meeting_booked(db, opportunity.id):
        return {"state": "meeting_booked", "reason": "a CalendarBooking is linked to this opportunity"}

    outcome = _outcome_for_contact(db, tenant_id, opportunity.id, contact.id)
    if outcome is not None:
        if outcome.outcome_category in POSITIVE_OUTCOME_CATEGORIES:
            return {"state": "positive", "reason": f"positive SalesOutcome recorded ({outcome.outcome_category})"}
        # Any other real reply (not_interested, timing_deferral, generic reply) -- see module
        # docstring's OUTCOME TREATMENT note for why this stops rather than falls back.
        return {"state": "engaged" if outcome.outcome_category != "not_interested" else "negative", "reason": f"a real reply was recorded ({outcome.outcome_category}) -- not auto-advancing per policy"}

    last_success = _last_successful_attempt(db, draft.id)
    if last_success is None:
        last_any = _last_attempt(db, draft.id)
        if last_any is not None and last_any.status == "failed" and last_any.retryable is False:
            return {"state": "hard_failure", "reason": f"permanent send failure: {last_any.error_message}"}
        return {"state": "pending_send", "reason": "draft exists but has not been successfully sent yet"}

    elapsed_hours = (now - last_success.attempted_at).total_seconds() / 3600
    if elapsed_hours < fallback_delay_hours:
        return {"state": "awaiting_response", "reason": f"sent {elapsed_hours:.1f}h ago, fallback delay is {fallback_delay_hours}h"}
    return {"state": "no_response_timeout", "reason": f"sent {elapsed_hours:.1f}h ago, fallback delay ({fallback_delay_hours}h) elapsed with no response"}


def evaluate_sequence_state(db: Session, tenant_id: int, opportunity: Opportunity, strategy: GtmStrategy, fallback_delay_hours: int) -> dict:
    """Walks the ranked, eligible contact list for this opportunity's company IN ORDER,
    determining the real current sequence state. Returns:
    {"status": "stopped"|"waiting"|"fallback_eligible"|"exhausted"|"no_eligible_contacts",
    "reason": str, "next_contact_id": int|None, "tried_contact_ids": list[int]}."""
    if opportunity.company_id is None:
        return {"status": "no_eligible_contacts", "reason": "opportunity has no company_id", "next_contact_id": None, "tried_contact_ids": []}

    eligible_contacts = get_eligible_contacts(db, opportunity.company_id)
    if not eligible_contacts:
        return {"status": "no_eligible_contacts", "reason": "no eligible (non-suppressed) contacts for this company", "next_contact_id": None, "tried_contact_ids": []}

    ranked = rank_contacts(eligible_contacts, opportunity.affected_function)
    tried_contact_ids: list[int] = []

    for entry in ranked:
        contact = entry["contact"]
        draft = _draft_for_contact(db, tenant_id, opportunity.id, strategy.id, contact.id)
        outcome = evaluate_contact_outcome(db, tenant_id, opportunity, contact, draft, fallback_delay_hours)
        state = outcome["state"]

        if state == "never_sent":
            if not tried_contact_ids:
                # This is the primary -- generating their first draft is
                # run_message_generation_sweep's job (Phase 5/6/7, unmodified), not ours.
                return {"status": "waiting", "reason": "primary contact has no draft yet -- message_generation's job", "next_contact_id": None, "tried_contact_ids": tried_contact_ids}
            return {"status": "fallback_eligible", "reason": f"contact {contact.id} is next-ranked and has no draft yet", "next_contact_id": contact.id, "tried_contact_ids": tried_contact_ids}

        if state in ("meeting_booked", "positive"):
            return {"status": "stopped", "reason": outcome["reason"], "next_contact_id": None, "tried_contact_ids": tried_contact_ids}

        if state in ("negative", "engaged", "pending_send", "awaiting_response"):
            return {"status": "waiting", "reason": outcome["reason"], "next_contact_id": None, "tried_contact_ids": tried_contact_ids}

        # suppressed / hard_failure / no_response_timeout -- this contact's chance is over,
        # try the next ranked one.
        tried_contact_ids.append(contact.id)

    return {"status": "exhausted", "reason": f"all {len(ranked)} eligible contact(s) tried, none succeeded", "next_contact_id": None, "tried_contact_ids": tried_contact_ids}


def advance_outreach_sequence(db: Session, tenant_id: int, opportunity: Opportunity) -> dict:
    """The Phase 8 entrypoint for one Opportunity. Never raises: a failure here must not crash
    the sweep, same discipline as every other V2 stage."""
    try:
        check_can_run(db, tenant_id)
    except ControlPlaneHalted as e:
        return {"status": "skipped", "reason": str(e)}

    config = get_control_config(db, tenant_id)
    fallback_delay_hours = config["sequencing"]["fallback_delay_hours"]
    if not fallback_delay_hours:
        return {"status": "skipped", "reason": "sequencing.fallback_delay_hours not configured"}

    strategy = _latest_strategy(db, tenant_id, opportunity.id)
    if strategy is None:
        return {"status": "skipped", "reason": "no GtmStrategy exists yet for this opportunity"}

    try:
        state = evaluate_sequence_state(db, tenant_id, opportunity, strategy, fallback_delay_hours)
    except Exception as e:  # noqa: BLE001 -- a read-path failure must not crash the sweep
        return {"status": "failed", "opportunity_id": opportunity.id, "error": str(e)}

    if state["status"] != "fallback_eligible":
        return {"status": "skipped", "reason": state["reason"]}

    try:
        draft = generate_message_draft(db, tenant_id, opportunity, strategy, exclude_contact_ids=state["tried_contact_ids"])
    except Exception as e:  # noqa: BLE001 -- one opportunity's failure must never block the others
        return {"status": "failed", "opportunity_id": opportunity.id, "error": str(e)}

    return {
        "status": "succeeded",
        "opportunity_id": opportunity.id,
        "fallback_contact_id": state["next_contact_id"],
        "draft_id": draft.id,
        "draft_status": draft.status,
        "reason": state["reason"],
    }


def run_v2_outreach_sequencing_sweep(db: Session, tenant_id: int, limit: int = 50) -> dict:
    """Hourly-sweep-integrated entrypoint -- iterates Opportunities that already have at least
    one MessageDraft (message_generation already bootstrapped the primary) and decides whether
    to advance each to a fallback contact. Never raises."""
    try:
        check_can_run(db, tenant_id)
    except ControlPlaneHalted as e:
        return {"status": "skipped", "reason": str(e)}

    counts = {"status": "succeeded", "evaluated": 0, "advanced": 0, "skipped": 0, "failed": 0}

    opportunity_ids = [
        row[0]
        for row in db.query(MessageDraft.opportunity_id)
        .filter(MessageDraft.tenant_id == tenant_id)
        .distinct()
        .order_by(MessageDraft.opportunity_id)
        .limit(limit)
        .all()
    ]

    for opp_id in opportunity_ids:
        opportunity = db.get(Opportunity, opp_id)
        if opportunity is None or opportunity.tenant_id != tenant_id:
            continue
        counts["evaluated"] += 1
        result = advance_outreach_sequence(db, tenant_id, opportunity)
        if result["status"] == "succeeded":
            counts["advanced"] += 1
        elif result["status"] == "failed":
            counts["failed"] += 1
        else:
            counts["skipped"] += 1

    return counts
