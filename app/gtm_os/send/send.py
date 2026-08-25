"""V2 Send orchestration -- Phase 7. send_message_draft() is the ONLY function in this codebase
that turns an approved MessageDraft into a real outbound provider call. Every check below runs
BEFORE the provider call; the provider call is the final side effect (per explicit instruction).

Channel selection happens once, here -- app.gtm_os.send.channels holds the actual per-provider
logic, so no provider-specific branching leaks into the sweep/orchestration layer (sweep.py just
calls run_v2_send_sweep(), which calls this, which picks the adapter)."""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import Contact
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run, get_control_config, is_within_business_hours
from app.gtm_os.send.channels import send_via_salesrobot, send_via_smtp
from app.gtm_os.send.send_state import (
    MessageSendAttempt,
    count_successful_sends_since,
    get_attempts_for_draft,
    has_successful_send,
    is_within_cooldown,
    last_attempt_permanently_failed,
)

# "email" -> "smtp" (2026-08-25, explicit instruction): email sends bypass Smartlead entirely and
# go via real SMTP from a specific, fixed mailbox (see app/smtp_client.py, app/gtm_os/send/
# channels.py's send_via_smtp). Smartlead is no longer used for any channel here.
CHANNEL_PROVIDERS = {"email": "smtp", "linkedin": "salesrobot"}

# smtp needs two credentials (a mailbox address + its app password), not the single
# "{provider}_api_key" shape every other provider here uses -- checked separately below.
PROVIDER_CREDENTIAL_NAMES = {"smtp": ("smtp_email", "smtp_app_password")}

# Every one of these must be explicitly configured (not None) before ANY send proceeds -- per
# explicit instruction, an unconfigured send limit must never be silently interpreted as
# unlimited. retry.max_attempts/backoff_minutes are NOT in this list -- they already carry real
# technical defaults from Phase 0 (never None), so they're never "unconfigured".
REQUIRED_LIMIT_FIELDS = ("max_sends_per_day", "company_cooldown_days", "contact_cooldown_days")


def _has_credential(db: Session, tenant_id: int, name: str) -> bool:
    from app.db.models import Credential
    return (
        db.query(Credential.id)
        .filter(Credential.tenant_id == tenant_id, Credential.name == name, Credential.value.isnot(None))
        .first()
        is not None
    )


def _has_active_campaign_push(db: Session, contact_id: int) -> bool:
    """Reuses V1's own campaign_pushes log exactly as-is -- "respect active campaign state
    where relevant" means checking whether V1 has ALREADY actively pushed this same contact
    into an outreach campaign, not building a second concept of "active"."""
    from app.db.models import CampaignPush
    return (
        db.query(CampaignPush.id)
        .filter(CampaignPush.contact_id == contact_id, CampaignPush.status == "pushed")
        .first()
        is not None
    )


def send_message_draft(db: Session, tenant_id: int, draft: MessageDraft) -> dict:
    """The full safety-gated send for ONE MessageDraft. Never raises for a routine skip/failure
    reason -- returns a structured result. Every check below can turn this into a "skipped"
    result with NO write to message_send_attempts (skips are free, re-evaluated fresh every
    call, same discipline as every other V2 stage's control-plane check) EXCEPT the final
    provider call, whose outcome (success or failure) is always persisted."""
    try:
        check_can_run(db, tenant_id)
    except ControlPlaneHalted as e:
        return {"status": "skipped", "reason": str(e)}

    if draft.status != "approved":
        return {"status": "skipped", "reason": f"draft status is {draft.status!r}, not 'approved'"}

    if has_successful_send(db, draft.id):
        return {"status": "skipped", "reason": "already successfully sent"}

    config = get_control_config(db, tenant_id)
    retry_cfg = config["retry"]
    attempts = get_attempts_for_draft(db, draft.id)
    if last_attempt_permanently_failed(db, draft.id):
        return {"status": "skipped", "reason": "most recent attempt failed permanently (not retryable)"}
    if len(attempts) >= retry_cfg["max_attempts"]:
        return {"status": "skipped", "reason": f"retry limit reached ({len(attempts)}/{retry_cfg['max_attempts']})"}
    if attempts:
        last_attempt_at = attempts[-1].attempted_at
        backoff_minutes = retry_cfg["backoff_minutes"]
        elapsed_minutes = (datetime.utcnow() - last_attempt_at).total_seconds() / 60
        if elapsed_minutes < backoff_minutes:
            return {"status": "skipped", "reason": f"retry backoff not yet elapsed ({elapsed_minutes:.1f}m / {backoff_minutes}m)"}

    limits = config["limits"]
    missing_limits = [f for f in REQUIRED_LIMIT_FIELDS if limits.get(f) is None]
    if missing_limits:
        return {"status": "skipped", "reason": f"required safety configuration missing: {', '.join(f'limits.{f}' for f in missing_limits)}"}

    if draft.contact_id is None:
        return {"status": "skipped", "reason": "draft has no selected contact"}
    contact = db.get(Contact, draft.contact_id)
    if contact is None:
        return {"status": "skipped", "reason": "selected contact no longer exists"}
    if contact.excluded_from_push:
        return {"status": "skipped", "reason": "contact is suppressed (excluded_from_push)"}

    if contact.company_id is not None:
        company_contacts = db.query(Contact).filter(Contact.company_id == contact.company_id).all()
        if company_contacts and all(c.excluded_from_push for c in company_contacts):
            return {"status": "skipped", "reason": "all contacts at this company are suppressed"}

    if _has_active_campaign_push(db, contact.id):
        return {"status": "skipped", "reason": "contact already has an active V1 campaign push"}

    within_cooldown, cooldown_reason = is_within_cooldown(db, contact, limits["company_cooldown_days"], limits["contact_cooldown_days"])
    if within_cooldown:
        return {"status": "skipped", "reason": cooldown_reason}

    since_midnight = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = count_successful_sends_since(db, tenant_id, since_midnight)
    if sent_today >= limits["max_sends_per_day"]:
        return {"status": "skipped", "reason": f"limits.max_sends_per_day reached ({sent_today}/{limits['max_sends_per_day']})"}

    if not is_within_business_hours(config):
        return {"status": "skipped", "reason": "outside configured business hours"}

    provider = CHANNEL_PROVIDERS.get(draft.channel)
    if provider is None:
        return {"status": "skipped", "reason": f"unsupported or missing channel {draft.channel!r}"}

    required_credential_names = PROVIDER_CREDENTIAL_NAMES.get(provider, (f"{provider}_api_key",))
    missing_credentials = [name for name in required_credential_names if not _has_credential(db, tenant_id, name)]
    if missing_credentials:
        return {"status": "skipped", "reason": f"{', '.join(missing_credentials)} credential(s) not configured"}

    opportunity = db.get(Opportunity, draft.opportunity_id)

    # THE PROVIDER CALL -- the final side effect. Everything above is a pure read/skip check.
    if provider == "smtp":
        result = send_via_smtp(db, tenant_id, draft, contact, opportunity)
    else:
        result = send_via_salesrobot(db, tenant_id, draft, contact, opportunity)

    attempt = MessageSendAttempt(
        tenant_id=tenant_id,
        message_draft_id=draft.id,
        contact_id=contact.id,
        channel=draft.channel,
        provider=provider,
        status=result["status"],
        provider_ref=result.get("provider_ref"),
        error_message=result.get("error_message"),
        retryable=result.get("retryable"),
    )
    db.add(attempt)
    db.commit()

    return {
        "status": result["status"],
        "provider": provider,
        "attempt_id": attempt.id,
        "provider_ref": result.get("provider_ref"),
        "error_message": result.get("error_message"),
    }


def run_v2_send_sweep(db: Session, tenant_id: int, limit: int = 50) -> dict:
    """Hourly-sweep-integrated entrypoint -- iterates approved MessageDrafts and attempts to
    send each via send_message_draft(). Never raises: per-draft failures are isolated, same
    discipline as every other sweep stage."""
    try:
        check_can_run(db, tenant_id)
    except ControlPlaneHalted as e:
        return {"status": "skipped", "reason": str(e)}

    counts = {
        "status": "succeeded",
        "evaluated": 0,
        "enrolled": 0,
        "request_submitted": 0,
        "failed": 0,
        "skipped": 0,
    }

    draft_ids = [
        row[0]
        for row in db.query(MessageDraft.id)
        .filter(MessageDraft.tenant_id == tenant_id, MessageDraft.status == "approved")
        .order_by(MessageDraft.id)
        .limit(limit)
        .all()
    ]

    for draft_id in draft_ids:
        draft = db.get(MessageDraft, draft_id)
        if draft is None or draft.tenant_id != tenant_id:
            continue
        counts["evaluated"] += 1
        try:
            result = send_message_draft(db, tenant_id, draft)
        except Exception:  # noqa: BLE001 -- one draft's failure must never block the others
            counts["failed"] += 1
            continue

        if result["status"] in ("enrolled", "request_submitted"):
            counts[result["status"]] += 1
        elif result["status"] == "failed":
            counts["failed"] += 1
        else:
            counts["skipped"] += 1

    return counts
