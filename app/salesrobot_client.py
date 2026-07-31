import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

BASE_URL = "https://api.boomtechinc.com/api"


class SalesRobotError(Exception):
    pass


def _get_api_key(db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == "salesrobot_api_key")
        .first()
    )
    if not cred or not cred.value:
        raise SalesRobotError("salesrobot_api_key credential is not set for this tenant")
    return cred.value


def list_campaigns(linkedin_account_uuid: str, db: Session, tenant_id: int, page: int = 0, size: int = 50) -> dict:
    """Real, documented endpoint (docs.salesrobot.co/reference/getcampaigns) -- the earlier
    version of this function guessed at paths and got a 400 from the correct one (/campaigns)
    because linkedinAccountUuid is a required query param, not because the path was wrong."""
    api_key = _get_api_key(db, tenant_id)
    response = httpx.get(
        f"{BASE_URL}/campaigns",
        params={"linkedinAccountUuid": linkedin_account_uuid, "page": page, "size": size},
        headers={"X-API-KEY": api_key, "content-type": "application/json;charset=UTF-8"},
        timeout=30,
    )
    if response.status_code != 200:
        raise SalesRobotError(f"campaigns failed ({response.status_code}): {response.text}")
    return response.json()


def get_campaign_prospects(campaign_uuid: str, linkedin_account_uuid: str, db: Session, tenant_id: int, page: int = 0, size: int = 50) -> dict:
    """Pulls prospect records (with real status flags: isConnected, isReplied, inMailSent,
    meetingLinkSent, etc.) for a campaign directly -- per SalesRobot's own published API
    reference (docs.salesrobot.co/reference/getprospectsforcampaign), which describes a real
    GET /campaign/prospects endpoint. This contradicts an earlier finding this session that no
    pull/status API existed -- that finding was based on guessed endpoint paths
    (/campaign/{uuid}, /campaign/{uuid}/prospects, etc.) that all 404'd; this is the real,
    documented path and hasn't been tested live yet."""
    api_key = _get_api_key(db, tenant_id)
    response = httpx.get(
        f"{BASE_URL}/campaign/prospects",
        params={"linkedinAccountUuid": linkedin_account_uuid, "campaignUuid": campaign_uuid, "page": page, "size": size},
        headers={"X-API-KEY": api_key, "content-type": "application/json;charset=UTF-8"},
        timeout=30,
    )
    if response.status_code != 200:
        raise SalesRobotError(f"campaign/prospects failed ({response.status_code}): {response.text}")
    return response.json()


def add_single_prospect(campaign_uuid: str, linkedin_account_uuid: str, prospect: dict, db: Session, tenant_id: int) -> dict:
    """prospect: {"profileUrl": str, "firstName": str, "lastName": str, "jobTitle": str,
    "companyName": str, "customMap": {...}}"""
    api_key = _get_api_key(db, tenant_id)
    response = httpx.post(
        f"{BASE_URL}/add-single-prospect",
        params={"campaignUuid": campaign_uuid, "linkedinAccountUuid": linkedin_account_uuid},
        json=prospect,
        headers={"X-API-KEY": api_key, "content-type": "application/json;charset=UTF-8"},
        timeout=30,
    )
    if response.status_code != 200:
        raise SalesRobotError(f"add-single-prospect failed ({response.status_code}): {response.text}")
    return response.json()
