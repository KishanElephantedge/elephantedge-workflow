import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

BASE_URL = "https://api.hubapi.com"


class HubSpotError(Exception):
    pass


def _get_api_key(db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == "hubspot_api_key")
        .first()
    )
    if not cred or not cred.value:
        raise HubSpotError("hubspot_api_key credential is not set for this tenant")
    return cred.value


def _headers(db: Session, tenant_id: int) -> dict:
    return {"Authorization": f"Bearer {_get_api_key(db, tenant_id)}", "Content-Type": "application/json"}


def find_company_by_domain(domain: str, db: Session, tenant_id: int) -> int | None:
    """Returns the HubSpot company id if one already exists for this domain, else None --
    checked before creating, so re-running the pipeline never creates duplicate companies."""
    response = httpx.post(
        f"{BASE_URL}/crm/v3/objects/companies/search",
        headers=_headers(db, tenant_id),
        json={"filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}]},
        timeout=30,
    )
    if response.status_code != 200:
        raise HubSpotError(f"companies/search failed ({response.status_code}): {response.text}")
    results = response.json().get("results", [])
    return int(results[0]["id"]) if results else None


def create_company(name: str, domain: str, db: Session, tenant_id: int) -> int:
    response = httpx.post(
        f"{BASE_URL}/crm/v3/objects/companies",
        headers=_headers(db, tenant_id),
        json={"properties": {"name": name, "domain": domain}},
        timeout=30,
    )
    if response.status_code != 201:
        raise HubSpotError(f"companies create failed ({response.status_code}): {response.text}")
    return int(response.json()["id"])


def create_contact(first_name: str | None, last_name: str | None, title: str | None, db: Session, tenant_id: int) -> int:
    properties = {"firstname": first_name or "", "lastname": last_name or ""}
    if title:
        properties["jobtitle"] = title
    response = httpx.post(
        f"{BASE_URL}/crm/v3/objects/contacts",
        headers=_headers(db, tenant_id),
        json={"properties": properties},
        timeout=30,
    )
    if response.status_code != 201:
        raise HubSpotError(f"contacts create failed ({response.status_code}): {response.text}")
    return int(response.json()["id"])


def associate_contact_to_company(contact_id: int, company_id: int, db: Session, tenant_id: int) -> None:
    response = httpx.put(
        f"{BASE_URL}/crm/v4/objects/contacts/{contact_id}/associations/default/companies/{company_id}",
        headers=_headers(db, tenant_id),
        timeout=30,
    )
    if response.status_code not in (200, 201):
        raise HubSpotError(f"association failed ({response.status_code}): {response.text}")
