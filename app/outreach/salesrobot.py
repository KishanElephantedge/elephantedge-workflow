from sqlalchemy.orm import Session

from app.db.models import Contact, Parameter
from app.outreach.base import OutreachChannel
from app.outreach.smartlead import push_email
from app.salesrobot_client import SalesRobotError, add_single_prospect


class SalesRobotChannel(OutreachChannel):
    """Second real implementation of OutreachChannel -- proof that swapping outreach tools
    doesn't require touching the calling orchestration code (Phase 12's design intent).
    Reads its campaign/account config from this tenant's own Parameter rows, same pattern as
    HeyReachChannel."""

    def __init__(self, db: Session, tenant_id: int):
        self.db = db
        self.tenant_id = tenant_id

    def _get_param(self, key: str) -> str:
        param = (
            self.db.query(Parameter)
            .filter(Parameter.tenant_id == self.tenant_id)
            .filter(Parameter.key == key)
            .first()
        )
        if not param or not param.value:
            raise SalesRobotError(f"{key} parameter is not set")
        value = param.value
        return value.get("value") if isinstance(value, dict) else value

    def push_lead(self, contact: Contact) -> dict:
        if not contact.linkedin_url:
            return {"status": "skipped", "error_message": "no linkedin_url", "channel_ref": None}

        try:
            campaign_uuid = self._get_param("salesrobot_campaign_uuid")
            linkedin_account_uuid = self._get_param("salesrobot_linkedin_account_uuid")
        except SalesRobotError as e:
            return {"status": "failed", "error_message": str(e), "channel_ref": None}

        prospect = {"profileUrl": contact.linkedin_url}
        if contact.first_name:
            prospect["firstName"] = contact.first_name
        if contact.last_name:
            prospect["lastName"] = contact.last_name
        if contact.title:
            prospect["jobTitle"] = contact.title
        if contact.company and contact.company.name:
            prospect["companyName"] = contact.company.name

        # Only an APPROVED Phase 13 message is ever sent -- draft/rejected/no-message
        # contacts still get pushed (this gate is deliberately separate from message
        # approval, see autonomous_orchestrator.py), just without a personalized note.
        # SalesRobot's connection-request/message step templates have to reference these
        # exact keys ({{connectionNote}} / {{personalizedMessage}}) for them to actually
        # appear in what gets sent -- configured in SalesRobot's own campaign editor, not
        # here. Two separate fields, not one shared one: found live that a single shared
        # field meant the connection note and the post-acceptance follow-up message were
        # literally the same text, since both steps' templates referenced the same
        # placeholder -- there was nothing else for the second step to display. connectionNote
        # is a short, free, template-based line (no extra LLM cost) specifically sized for a
        # connection request; personalizedMessage remains the real, full Phase 13 message,
        # meant only for the follow-up step sent after acceptance.
        pm = contact.personalized_message
        if pm and pm.status == "approved" and pm.generated_message:
            company_name = contact.company.name if contact.company and contact.company.name else None
            note = f"Hi {contact.first_name}, I'd love to connect"
            note += f" -- following what {company_name} is building." if company_name else "."
            prospect["customMap"] = {
                "connectionNote": note,
                "personalizedMessage": pm.generated_message,
            }

        try:
            add_single_prospect(campaign_uuid, linkedin_account_uuid, prospect, self.db, self.tenant_id)
            result = {"status": "pushed", "error_message": None, "channel_ref": campaign_uuid}
        except SalesRobotError as e:
            result = {"status": "failed", "error_message": str(e), "channel_ref": campaign_uuid}

        # Email is a separate, additive channel via Smartlead (SalesRobot's own email step
        # was found untested/unreliable -- see progress-log.md) -- best-effort, never lets an
        # email failure override the LinkedIn push's own real result.
        if pm and pm.status == "approved":
            email_result = push_email(contact, pm, self.db, self.tenant_id)
            if email_result["status"] == "failed":
                result["error_message"] = (result.get("error_message") + " | " if result.get("error_message") else "") + f"email: {email_result['error_message']}"

        return result
