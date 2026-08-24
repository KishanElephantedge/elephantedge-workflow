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


# ---- CRM read/write/delete (V2 CRM page) -- everything below this line reads back from and
# mutates real HubSpot data, unlike the push-only functions above. Curated property sets only
# (not every HubSpot field) -- name/domain/industry/city/state/phone/website for companies,
# firstname/lastname/email/phone/jobtitle for contacts -- a deliberate scope decision, not a
# limitation: HubSpot has many custom/system properties, and exposing all of them risks a wrong
# edit on a real production CRM for fields nobody asked to manage here.

COMPANY_PROPERTIES = ["name", "domain", "industry", "city", "state", "phone", "website"]
CONTACT_PROPERTIES = ["firstname", "lastname", "email", "phone", "jobtitle"]

# HubSpot's per-page cap for both list and search endpoints.
MAX_PAGE_LIMIT = 100


def _list_or_search(object_type: str, properties: list[str], db: Session, tenant_id: int, limit: int, after: str | None, search: str | None) -> dict:
    """Plain paginated list when there's no search term (cheap, cursor-based); HubSpot's own
    /search endpoint (a free-text `query` across default searchable properties) when there is --
    two different HubSpot endpoints, unified into one return shape so the route layer doesn't
    need to know which path was taken."""
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    headers = _headers(db, tenant_id)
    if search:
        body = {"query": search, "limit": limit, "properties": properties}
        if after:
            body["after"] = after
        response = httpx.post(f"{BASE_URL}/crm/v3/objects/{object_type}/search", headers=headers, json=body, timeout=30)
    else:
        params = {"limit": limit, "properties": ",".join(properties)}
        if after:
            params["after"] = after
        response = httpx.get(f"{BASE_URL}/crm/v3/objects/{object_type}", headers=headers, params=params, timeout=30)

    if response.status_code != 200:
        raise HubSpotError(f"{object_type} list/search failed ({response.status_code}): {response.text}")
    data = response.json()
    return {
        "results": [{"id": r["id"], "properties": r.get("properties", {})} for r in data.get("results", [])],
        "next_after": data.get("paging", {}).get("next", {}).get("after"),
    }


def _get_object(object_type: str, object_id: str, properties: list[str], db: Session, tenant_id: int) -> dict:
    """Single-object fetch, used by the V2 edit page so a direct URL / page refresh always has
    real data to show instead of relying on state handed off from the list page."""
    response = httpx.get(
        f"{BASE_URL}/crm/v3/objects/{object_type}/{object_id}",
        headers=_headers(db, tenant_id),
        params={"properties": ",".join(properties)},
        timeout=30,
    )
    if response.status_code != 200:
        raise HubSpotError(f"{object_type} fetch failed ({response.status_code}): {response.text}")
    result = response.json()
    return {"id": result["id"], "properties": result.get("properties", {})}


def get_company(company_id: str, db: Session, tenant_id: int) -> dict:
    return _get_object("companies", company_id, COMPANY_PROPERTIES, db, tenant_id)


def get_contact(contact_id: str, db: Session, tenant_id: int) -> dict:
    return _get_object("contacts", contact_id, CONTACT_PROPERTIES, db, tenant_id)


def list_companies(db: Session, tenant_id: int, limit: int = 25, after: str | None = None, search: str | None = None) -> dict:
    return _list_or_search("companies", COMPANY_PROPERTIES, db, tenant_id, limit, after, search)


def list_contacts(db: Session, tenant_id: int, limit: int = 25, after: str | None = None, search: str | None = None) -> dict:
    return _list_or_search("contacts", CONTACT_PROPERTIES, db, tenant_id, limit, after, search)


def _update_object(object_type: str, object_id: str, properties: dict, allowed: list[str], db: Session, tenant_id: int) -> dict:
    """Silently drops any property not in the curated allowed set -- a deliberate, non-fabricating
    restriction (never partially-apply a request with unexpected fields), not a validation error,
    since the caller (the route layer) already only ever sends curated fields from the UI."""
    filtered = {k: v for k, v in properties.items() if k in allowed}
    response = httpx.patch(
        f"{BASE_URL}/crm/v3/objects/{object_type}/{object_id}",
        headers=_headers(db, tenant_id),
        json={"properties": filtered},
        timeout=30,
    )
    if response.status_code != 200:
        raise HubSpotError(f"{object_type} update failed ({response.status_code}): {response.text}")
    result = response.json()
    return {"id": result["id"], "properties": result.get("properties", {})}


def update_company(company_id: str, properties: dict, db: Session, tenant_id: int) -> dict:
    return _update_object("companies", company_id, properties, COMPANY_PROPERTIES, db, tenant_id)


def update_contact(contact_id: str, properties: dict, db: Session, tenant_id: int) -> dict:
    return _update_object("contacts", contact_id, properties, CONTACT_PROPERTIES, db, tenant_id)


def _delete_object(object_type: str, object_id: str, db: Session, tenant_id: int) -> None:
    response = httpx.delete(f"{BASE_URL}/crm/v3/objects/{object_type}/{object_id}", headers=_headers(db, tenant_id), timeout=30)
    # HubSpot's own delete semantics: 204 on success, 404 if it's already gone -- both treated
    # as "the object is no longer there," the actual thing the caller wants, not as a failure.
    if response.status_code not in (204, 404):
        raise HubSpotError(f"{object_type} delete failed ({response.status_code}): {response.text}")


def delete_company(company_id: str, db: Session, tenant_id: int) -> None:
    _delete_object("companies", company_id, db, tenant_id)


def delete_contact(contact_id: str, db: Session, tenant_id: int) -> None:
    _delete_object("contacts", contact_id, db, tenant_id)
