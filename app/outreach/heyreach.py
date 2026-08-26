from sqlalchemy.orm import Session

from app.db.models import Contact, Parameter
from app.gtm_os.opportunity.offering_config import get_offering_campaign_id
from app.heyreach_client import HeyReachError, add_leads_to_campaign, get_campaign
from app.outreach.base import OutreachChannel


class HeyReachChannel(OutreachChannel):
    """First real implementation of OutreachChannel -- LinkedIn connection requests via
    HeyReach. Reads its campaign config from this tenant's own Parameter/Credential rows (or,
    per-offering, from offering_config.py -- see push_lead); caches each campaign/sender lookup
    it makes for the lifetime of one run rather than re-fetching per lead."""

    def __init__(self, db: Session, tenant_id: int):
        self.db = db
        self.tenant_id = tenant_id
        self._campaign_id_cache: dict[str | None, int] = {}
        self._sender_account_cache: dict[int, int] = {}

    def _get_default_campaign_id(self) -> int:
        param = (
            self.db.query(Parameter)
            .filter(Parameter.tenant_id == self.tenant_id)
            .filter(Parameter.key == "heyreach_campaign_id")
            .first()
        )
        if not param or not param.value:
            raise HeyReachError("heyreach_campaign_id parameter is not set")
        return int(param.value.get("campaign_id") if isinstance(param.value, dict) else param.value)

    def _resolve_campaign_id(self, offering_name: str | None) -> int:
        if offering_name in self._campaign_id_cache:
            return self._campaign_id_cache[offering_name]
        if offering_name:
            configured = get_offering_campaign_id(self.db, self.tenant_id, offering_name, "heyreach")
            if not configured:
                raise HeyReachError(f"No HeyReach campaign configured for offering {offering_name!r}")
            campaign_id = int(configured)
        else:
            campaign_id = self._get_default_campaign_id()
        self._campaign_id_cache[offering_name] = campaign_id
        return campaign_id

    def _get_sender_account_id(self, campaign_id: int) -> int:
        if campaign_id in self._sender_account_cache:
            return self._sender_account_cache[campaign_id]
        campaign = get_campaign(campaign_id, self.db, self.tenant_id)
        account_ids = campaign.get("campaignAccountIds") or campaign.get("accountIds") or []
        if not account_ids:
            raise HeyReachError(f"Campaign {campaign_id} has no sender LinkedIn accounts configured")
        self._sender_account_cache[campaign_id] = account_ids[0]
        return account_ids[0]

    def push_lead(self, contact: Contact, offering_name: str | None = None) -> dict:
        if not contact.linkedin_url:
            return {"status": "skipped", "error_message": "no linkedin_url", "channel_ref": None}

        try:
            campaign_id = self._resolve_campaign_id(offering_name)
            sender_account_id = self._get_sender_account_id(campaign_id)
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
