"""V2 Send channel adapters -- Phase 7. One function per channel, each a thin wrapper over an
EXISTING, already-proven provider client (app.smartlead_client / app.salesrobot_client) -- no
new provider client is created here, matching the explicit "do not create a second SmartLead
client" instruction (and the same discipline applied to SalesRobot). Each adapter's own tiny
campaign/account Parameter lookup mirrors the exact pattern every existing outreach adapter in
app/outreach/*.py already uses (its own private helper, not a shared one) -- config lookup is
cheap and channel-specific, not worth centralizing.

Return contract (both adapters): {"status": "enrolled"|"request_submitted"|"failed",
"provider_ref": str|None, "error_message": str|None, "retryable": bool|None}. `retryable` is
only meaningful on a "failed" result -- False means this exact failure cannot succeed on retry
(structural gap, or a provider rejection that's inherently permanent), True means a transient
failure (network/timeout/5xx) worth the configured retry attempts."""
from sqlalchemy.orm import Session

from app.db.models import Contact, Credential, Parameter
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.opportunity.opportunity import Opportunity
from app.salesrobot_client import SalesRobotError, send_connection_request
from app.smartlead_client import SmartleadError, add_lead
from app.smtp_client import SmtpError, send_email


def _get_smartlead_campaign_id(db: Session, tenant_id: int) -> int:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == "smartlead_campaign_id")
        .first()
    )
    if not param or not param.value:
        raise SmartleadError("smartlead_campaign_id parameter is not set")
    value = param.value
    return int(value.get("value") if isinstance(value, dict) else value)


def send_via_smartlead(db: Session, tenant_id: int, draft: MessageDraft, contact: Contact, opportunity: Opportunity) -> dict:
    # opportunity is unused here (kept for the shared adapter signature -- see
    # send_via_salesrobot's own note on this).
    if not contact.email:
        return {"status": "failed", "provider_ref": None, "error_message": "contact has no email address", "retryable": False}
    if not draft.message_text:
        return {"status": "failed", "provider_ref": None, "error_message": "draft has no message_text", "retryable": False}
    if not draft.subject:
        # V2 Phase 7 follow-up -- the send layer must NEVER generate or invent message content,
        # including a subject line. subject is produced ONLY by generate_message_draft()'s own
        # single LLM call (see MESSAGE_GENERATION_PROMPT); a draft missing one (an old
        # pre-this-change row, or one whose LLM response omitted it) is a real, permanent gap
        # this send stage cannot fill -- not retryable, since retrying won't add a subject that
        # was never generated.
        return {"status": "failed", "provider_ref": None, "error_message": "draft has no subject", "retryable": False}

    try:
        campaign_id = _get_smartlead_campaign_id(db, tenant_id)
    except SmartleadError as e:
        return {"status": "failed", "provider_ref": None, "error_message": str(e), "retryable": False}

    try:
        add_lead(campaign_id, contact.email, contact.first_name, draft.subject, draft.message_text, db, tenant_id)
    except SmartleadError as e:
        # add_lead() itself already distinguishes a real HTTP failure from a "not actually
        # attached" response and raises SmartleadError for both -- from here, a non-2xx/network
        # failure is treated as transient (retryable), a clean-but-rejected response (duplicate,
        # bad email format, etc.) would need the response body to tell them apart, which
        # add_lead() does not currently expose separately -- see Phase 7 report's flagged items.
        return {"status": "failed", "provider_ref": None, "error_message": str(e), "retryable": True}

    # "enrolled", never "sent" -- add_lead() only confirms the provider ACCEPTED the lead into
    # the campaign; Smartlead's own async sequence engine is what actually sends the email,
    # on its own schedule, which this call has no visibility into.
    return {"status": "enrolled", "provider_ref": f"campaign:{campaign_id}", "error_message": None, "retryable": None}


def _get_smtp_credential(db: Session, tenant_id: int, name: str) -> str | None:
    cred = db.query(Credential).filter(Credential.tenant_id == tenant_id, Credential.name == name).first()
    return cred.value if cred and cred.value else None


def send_via_smtp(db: Session, tenant_id: int, draft: MessageDraft, contact: Contact, opportunity: Opportunity) -> dict:
    """Real email sending, 2026-08-25 explicit instruction -- replaces send_via_smartlead for the
    "email" channel entirely (Smartlead is not used for email sends; this bypasses it). Sends
    directly via a fixed, real Gmail mailbox (smtp_email/smtp_app_password Credentials, same
    Credential model/CRUD every other provider credential here already uses -- no new storage
    mechanism). opportunity is unused here (kept for the shared adapter signature, same as
    send_via_salesrobot's own note on this)."""
    if not contact.email:
        return {"status": "failed", "provider_ref": None, "error_message": "contact has no email address", "retryable": False}
    if not draft.message_text:
        return {"status": "failed", "provider_ref": None, "error_message": "draft has no message_text", "retryable": False}
    if not draft.subject:
        # Same discipline as send_via_smartlead's own subject check -- the send layer must never
        # invent content, including a subject line; a draft missing one is a real, permanent gap.
        return {"status": "failed", "provider_ref": None, "error_message": "draft has no subject", "retryable": False}

    sender_email = _get_smtp_credential(db, tenant_id, "smtp_email")
    app_password = _get_smtp_credential(db, tenant_id, "smtp_app_password")
    if not sender_email or not app_password:
        return {"status": "failed", "provider_ref": None, "error_message": "smtp_email/smtp_app_password credentials not configured", "retryable": False}

    to_name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or None
    try:
        send_email(sender_email, app_password, contact.email, draft.subject, draft.message_text, to_name=to_name)
    except SmtpError as e:
        # An auth failure (wrong/expired app password) will not fix itself on retry; anything
        # else (connection/transient) is worth the configured retry attempts.
        retryable = "authentication failed" not in str(e).lower()
        return {"status": "failed", "provider_ref": None, "error_message": str(e), "retryable": retryable}

    # "sent", not "enrolled" -- unlike Smartlead's async campaign engine, this call IS the actual
    # delivery attempt to the mailbox's SMTP server; a successful return here means the email was
    # actually handed off for delivery, not just accepted into a queue.
    return {"status": "sent", "provider_ref": f"smtp:{sender_email}", "error_message": None, "retryable": None}


def send_via_salesrobot(db: Session, tenant_id: int, draft: MessageDraft, contact: Contact, opportunity: Opportunity) -> dict:
    # opportunity is unused here (SalesRobot's connection request has no subject line to
    # derive) -- kept in the signature so both adapters share one uniform call shape for the
    # orchestrator (send.py) to call through without per-channel branching there.
    if not contact.linkedin_url:
        return {"status": "failed", "provider_ref": None, "error_message": "contact has no linkedin_url", "retryable": False}
    if not draft.message_text:
        return {"status": "failed", "provider_ref": None, "error_message": "draft has no message_text", "retryable": False}

    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == "salesrobot_linkedin_account_uuid")
        .first()
    )
    if not param or not param.value:
        return {"status": "failed", "provider_ref": None, "error_message": "salesrobot_linkedin_account_uuid parameter is not set", "retryable": False}
    linkedin_account_uuid = param.value.get("value") if isinstance(param.value, dict) else param.value

    try:
        result = send_connection_request(contact.linkedin_url, linkedin_account_uuid, draft.message_text, db, tenant_id)
    except SalesRobotError as e:
        return {"status": "failed", "provider_ref": None, "error_message": str(e), "retryable": True}

    data = result.get("data") or {}
    if not result.get("success"):
        # A clean, well-formed API response that itself reports failure (invalid profile,
        # already connected, etc.) -- the request was submitted and rejected, not a
        # network/transient issue, so not retryable.
        return {
            "status": "failed",
            "provider_ref": data.get("invitationId"),
            "error_message": data.get("failureReason") or result.get("message") or "connection request rejected",
            "retryable": False,
        }

    # "request_submitted", never "sent"/"delivered" -- this confirms the connection request was
    # SUBMITTED, not that it was accepted or that any conversation resulted. See this module's
    # own docstring and send_connection_request()'s docstring for why that distinction matters.
    return {
        "status": "request_submitted",
        "provider_ref": data.get("invitationId"),
        "error_message": data.get("failureReason"),
        "retryable": None,
    }
