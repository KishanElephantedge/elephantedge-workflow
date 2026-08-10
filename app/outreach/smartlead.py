from sqlalchemy.orm import Session

from app.db.models import Contact, Parameter, PersonalizedMessage
from app.smartlead_client import SmartleadError, add_lead


def _get_campaign_id(db: Session, tenant_id: int) -> int:
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


def push_email(contact: Contact, pm: PersonalizedMessage, db: Session, tenant_id: int) -> dict:
    """Additive email send via Smartlead -- runs alongside the LinkedIn push, never instead
    of it. Only called when contact.email and pm.email_subject/email_body are all set (see
    callers). Best-effort: a Smartlead failure here should never fail the LinkedIn push."""
    if not (contact.email and pm.email_subject and pm.email_body):
        return {"status": "skipped", "error_message": "no email content", "channel_ref": None}
    try:
        campaign_id = _get_campaign_id(db, tenant_id)
        add_lead(campaign_id, contact.email, contact.first_name, pm.email_subject, pm.email_body, db, tenant_id)
        return {"status": "pushed", "error_message": None, "channel_ref": str(campaign_id)}
    except SmartleadError as e:
        return {"status": "failed", "error_message": str(e), "channel_ref": None}
