from sqlalchemy.orm import Session

from app.db.models import Contact, Parameter
from app.heyreach_client import HeyReachError, add_leads_to_campaign, get_campaign
from app.outreach.base import OutreachChannel


class HeyReachChannel(OutreachChannel):
    """First real implementation of OutreachChannel -- LinkedIn connection requests via
    HeyReach. Reads its campaign config from this tenant's own Parameter/Credential rows;
    caches the campaign lookup for the lifetime of one run rather than re-fetching per lead."""

    def __init__(self, db: Session, tenant_id: int):
        self.db = db
        self.tenant_id = tenant_id
        self._campaign_id: int | None = None
        self._sender_account_id: int | None = None

    def _get_campaign_id(self) -> int:
        if self._campaign_id is not None:
            return self._campaign_id
        param = (
            self.db.query(Parameter)
            .filter(Parameter.tenant_id == self.tenant_id)
            .filter(Parameter.key == "heyreach_campaign_id")
            .first()
        )
        if not param or not param.value:
            raise HeyReachError("heyreach_campaign_id parameter is not set")
        self._campaign_id = int(param.value.get("campaign_id") if isinstance(param.value, dict) else param.value)
        return self._campaign_id

    def _get_sender_account_id(self) -> int:
        if self._sender_account_id is not None:
            return self._sender_account_id
        campaign_id = self._get_campaign_id()
        campaign = get_campaign(campaign_id, self.db, self.tenant_id)
        account_ids = campaign.get("campaignAccountIds") or campaign.get("accountIds") or []
        if not account_ids:
            raise HeyReachError(f"Campaign {campaign_id} has no sender LinkedIn accounts configured")
        self._sender_account_id = account_ids[0]
        return self._sender_account_id

    def push_lead(self, contact: Contact) -> dict:
        if not contact.linkedin_url:
            return {"status": "skipped", "error_message": "no linkedin_url", "channel_ref": None}

        try:
            campaign_id = self._get_campaign_id()
            sender_account_id = self._get_sender_account_id()
        except HeyReachError as e:
            return {"status": "failed", "error_message": str(e), "channel_ref": None}

        lead = {"profileUrl": contact.linkedin_url}
        if contact.first_name:
            lead["firstName"] = contact.first_name
        if contact.last_name:
            lead["lastName"] = contact.last_name
        if contact.title:
            lead["position"] = contact.title
        if contact.company and contact.company.name:
            lead["companyName"] = contact.company.name

        try:
            add_leads_to_campaign(
                campaign_id,
                [{"linkedInAccountId": sender_account_id, "lead": lead}],
                self.db,
                self.tenant_id,
            )
            return {"status": "pushed", "error_message": None, "channel_ref": str(campaign_id)}
        except HeyReachError as e:
            return {"status": "failed", "error_message": str(e), "channel_ref": str(campaign_id)}
