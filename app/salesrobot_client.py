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
