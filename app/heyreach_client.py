import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

BASE_URL = "https://api.heyreach.io/api/public"


class HeyReachError(Exception):
    pass


def _get_api_key(db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == "heyreach_api_key")
        .first()
    )
    if not cred or not cred.value:
        raise HeyReachError("heyreach_api_key credential is not set for this tenant")
    return cred.value


def get_campaign(campaign_id: int, db: Session, tenant_id: int) -> dict:
    api_key = _get_api_key(db, tenant_id)
    response = httpx.get(
        f"{BASE_URL}/campaign/GetById",
        params={"campaignId": campaign_id},
        headers={"X-API-KEY": api_key},
        timeout=30,
    )
    if response.status_code != 200:
        raise HeyReachError(f"GetById failed ({response.status_code}): {response.text}")
    return response.json()


def add_leads_to_campaign(campaign_id: int, account_lead_pairs: list[dict], db: Session, tenant_id: int) -> dict:
    """Push leads into an existing HeyReach campaign.
    account_lead_pairs items: {"linkedInAccountId": int, "lead": {"profileUrl": str, ...}}
    """
    api_key = _get_api_key(db, tenant_id)
    response = httpx.post(
        f"{BASE_URL}/campaign/AddLeadsToCampaignV2",
        json={"campaignId": campaign_id, "accountLeadPairs": account_lead_pairs},
        headers={"X-API-KEY": api_key},
        timeout=60,
    )
    if response.status_code != 200:
        raise HeyReachError(f"AddLeadsToCampaignV2 failed ({response.status_code}): {response.text}")
    return response.json()
