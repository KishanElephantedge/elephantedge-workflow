import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

BASE_URL = "https://server.smartlead.ai/api/v1"


class SmartleadError(Exception):
    pass


def _get_api_key(db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == "smartlead_api_key")
        .first()
    )
    if not cred or not cred.value:
        raise SmartleadError("smartlead_api_key credential is not set for this tenant")
    return cred.value


def add_lead(campaign_id: int, email: str, first_name: str | None, subject: str, body: str, db: Session, tenant_id: int) -> dict:
    """Adds one lead to a Smartlead campaign whose sequence subject/body are the merge tags
    {{email_subject}}/{{email_body}} (confirmed live 2026-08-10 -- Smartlead supports
    per-lead merge tags in the subject line, not just the body).

    ignore_duplicate_leads_in_other_campaign MUST be False here -- found live the hard way:
    True does not mean "add anyway, ignore the fact that a duplicate exists elsewhere" (the
    intuitive reading), it means the opposite -- skip adding if this email already exists in
    any other campaign on the account. With it True, a real test lead was silently dropped
    (upload_count: 1 in the response, but never actually attached to the campaign) with no
    error surfaced anywhere in the response body.
    """
    api_key = _get_api_key(db, tenant_id)
    lead = {
        "email": email,
        "custom_fields": {"email_subject": subject, "email_body": body},
    }
    if first_name:
        lead["first_name"] = first_name
    response = httpx.post(
        f"{BASE_URL}/campaigns/{campaign_id}/leads",
        params={"api_key": api_key},
        json={
            "lead_list": [lead],
            "settings": {
                "ignore_global_block_list": True,
                "ignore_unsubscribe_list": True,
                "ignore_community_bounce_list": True,
                "ignore_duplicate_leads_in_other_campaign": False,
            },
        },
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if response.status_code != 200:
        raise SmartleadError(f"campaigns/{campaign_id}/leads failed ({response.status_code}): {response.text}")
    result = response.json()
    if not result.get("ok") or result.get("total_leads", 0) < 1:
        raise SmartleadError(f"lead not actually attached to campaign {campaign_id}: {result}")
    return result
