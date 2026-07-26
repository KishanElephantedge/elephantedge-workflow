from sqlalchemy.orm import Session

from app.db.models import Contact, Parameter
from app.outreach.base import OutreachChannel
from app.phases.personalization import compute_personalization_hook
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

        if contact.company:
            hook = compute_personalization_hook(contact.company, contact)
            contact.personalization_hook = hook
            self.db.commit()
            prospect["customMap"] = {"personalization_hook": hook}

        try:
            add_single_prospect(campaign_uuid, linkedin_account_uuid, prospect, self.db, self.tenant_id)
            return {"status": "pushed", "error_message": None, "channel_ref": campaign_uuid}
        except SalesRobotError as e:
            return {"status": "failed", "error_message": str(e), "channel_ref": campaign_uuid}
