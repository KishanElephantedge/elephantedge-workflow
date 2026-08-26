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
from app.gtm_os.opportunity.offering_config import get_offering_campaign_id
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.strategy.strategy import GtmStrategy
from app.salesrobot_client import SalesRobotError, add_single_prospect
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


def _get_followup_message_text(db: Session, tenant_id: int, draft: MessageDraft) -> str | None:
    followup = (
        db.query(MessageDraft)
        .filter(
            MessageDraft.tenant_id == tenant_id,
            MessageDraft.opportunity_id == draft.opportunity_id,
            MessageDraft.gtm_strategy_id == draft.gtm_strategy_id,
            MessageDraft.contact_id == draft.contact_id,
            MessageDraft.channel == draft.channel,
            MessageDraft.message_role == "followup",
        )
        .first()
    )
    return followup.message_text if followup else None


def send_via_salesrobot(db: Session, tenant_id: int, draft: MessageDraft, contact: Contact, opportunity: Opportunity) -> dict:
    """2026-08-26, explicit instruction -- rewritten from a one-shot send_connection_request()
    call to real SalesRobot Campaign enrollment (add_single_prospect), because the real
    campaigns created this session (Playbook, Sales OS, Execution, Workshop) each have their own
    Step 1 (connect) / Step 2 (message) / Step 3 (follow-up) sequence that only fires when the
    contact is actually added to that campaign -- a raw connection request has no way to trigger
    Step 2/3 at all. Routed per the Opportunity's matched offering (GtmStrategy.
    matched_offering_name), never one tenant-wide default -- a Fractional-VP-Sales-matched
    contact must never land in the Sales OS campaign just because that happened to be configured
    first (see get_offering_campaign_id's own docstring)."""
    if not contact.linkedin_url:
        return {"status": "failed", "provider_ref": None, "error_message": "contact has no linkedin_url", "retryable": False}
    if not draft.message_text:
        return {"status": "failed", "provider_ref": None, "error_message": "draft has no message_text", "retryable": False}

    strategy = db.get(GtmStrategy, draft.gtm_strategy_id)
    offering_name = strategy.matched_offering_name if strategy else None
    campaign_uuid = get_offering_campaign_id(db, tenant_id, offering_name, "salesrobot") if offering_name else None
    if not campaign_uuid:
        return {
            "status": "failed", "provider_ref": None,
            "error_message": f"no SalesRobot campaign configured for offering {offering_name!r}",
            "retryable": False,
        }

    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == "salesrobot_linkedin_account_uuid")
        .first()
    )
    if not param or not param.value:
        return {"status": "failed", "provider_ref": None, "error_message": "salesrobot_linkedin_account_uuid parameter is not set", "retryable": False}
    linkedin_account_uuid = param.value.get("value") if isinstance(param.value, dict) else param.value

    # customMap keys match the merge tags configured in these specific SalesRobot campaigns'
    # Step 2/Step 3 templates ({personalisedmessage}/{followup}, all-lowercase -- confirmed
    # directly against the real campaign editor, not our own naming convention). No
    # connectionNote: these campaigns' Step 1 doesn't reference one.
    prospect = {"profileUrl": contact.linkedin_url}
    if contact.first_name:
        prospect["firstName"] = contact.first_name
    if contact.last_name:
        prospect["lastName"] = contact.last_name
    if contact.title:
        prospect["jobTitle"] = contact.title
    if contact.company and contact.company.name:
        prospect["companyName"] = contact.company.name
    custom_map = {"personalisedmessage": draft.message_text}
    followup_text = _get_followup_message_text(db, tenant_id, draft)
    if followup_text:
        custom_map["followup"] = followup_text
    prospect["customMap"] = custom_map

    try:
        add_single_prospect(campaign_uuid, linkedin_account_uuid, prospect, db, tenant_id)
    except SalesRobotError as e:
        return {"status": "failed", "provider_ref": campaign_uuid, "error_message": str(e), "retryable": True}

    # "enrolled", not "sent"/"request_submitted" -- this confirms the contact was added to the
    # campaign; SalesRobot's own sequence engine is what actually sends Step 1/2/3, on its own
    # schedule, same "enrolled" semantics send_via_smartlead already uses in this module.
    return {"status": "enrolled", "provider_ref": campaign_uuid, "error_message": None, "retryable": None}
