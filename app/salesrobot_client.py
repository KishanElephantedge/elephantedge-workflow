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


def list_campaigns(db: Session, tenant_id: int) -> dict:
    """Looks up real campaign names from SalesRobot -- so a stored UUID (e.g.
    salesrobot_campaign_uuid) can be matched to the human-readable name it refers to. The
    exact endpoint path isn't documented anywhere available here, so this tries the handful
    of plausible REST conventions and returns whichever one actually responds, tagged with
    which path worked (temporary diagnostic detail, fine to simplify once confirmed)."""
    api_key = _get_api_key(db, tenant_id)
    headers = {"X-API-KEY": api_key, "content-type": "application/json;charset=UTF-8"}
    candidate_paths = ["/campaigns", "/get-campaigns", "/campaign/list", "/list-campaigns", "/get-all-campaigns"]
    attempts = []
    for path in candidate_paths:
        try:
            response = httpx.get(f"{BASE_URL}{path}", headers=headers, timeout=15)
        except httpx.HTTPError as e:
            attempts.append({"path": path, "error": str(e)})
            continue
        attempts.append({"path": path, "status": response.status_code, "body": response.text[:300]})
        if response.status_code == 200:
            return {"working_path": path, "data": response.json()}
    raise SalesRobotError(f"No known campaigns endpoint responded 200. Attempts: {attempts}")


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
