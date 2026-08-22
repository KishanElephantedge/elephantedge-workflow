"""Google Search Console (searchAnalytics) client -- separate integration, not shared with
Calendar or Analytics code, per explicit instruction to keep this feature standalone.

Auth: reuses the SAME Google Cloud OAuth client as app/google_analytics_client.py
(google_analytics_client_id/secret Credential rows) -- one Google Cloud project/OAuth client
covers both GA and Search Console scopes, but each service has its OWN refresh token (a
refresh token is scope-bound at the time it's issued), so this module has its own
google_search_console_refresh_token credential rather than reusing GA's.

Requires these Credential rows:
  - google_analytics_client_id       (shared OAuth client -- see google_analytics_client.py)
  - google_analytics_client_secret   (shared OAuth client)
  - google_search_console_refresh_token
  - google_search_console_site_url  (e.g. "sc-domain:elephantedge.ai" for a domain property,
    or "https://elephantedge.ai/" for a URL-prefix property -- must match exactly how the
    property is registered in Search Console, confirmed live as "sc-domain:elephantedge.ai")
"""
import urllib.parse

import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEARCH_CONSOLE_API_BASE = "https://searchconsole.googleapis.com/webmasters/v3"


class GoogleSearchConsoleError(Exception):
    pass


def _get_credential(name: str, db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == name)
        .first()
    )
    if not cred or not cred.value:
        raise GoogleSearchConsoleError(f"{name} credential is not set for this tenant")
    return cred.value


def _get_access_token(db: Session, tenant_id: int) -> str:
    client_id = _get_credential("google_analytics_client_id", db, tenant_id)
    client_secret = _get_credential("google_analytics_client_secret", db, tenant_id)
    refresh_token = _get_credential("google_search_console_refresh_token", db, tenant_id)
    try:
        response = httpx.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
    except httpx.HTTPError as e:
        raise GoogleSearchConsoleError(f"Token refresh failed: {e}") from e
    if response.status_code != 200:
        raise GoogleSearchConsoleError(f"Token refresh failed ({response.status_code}): {response.text}")
    return response.json()["access_token"]


def _query(db: Session, tenant_id: int, body: dict) -> list[dict]:
    access_token = _get_access_token(db, tenant_id)
    site_url = _get_credential("google_search_console_site_url", db, tenant_id)
    encoded_site = urllib.parse.quote(site_url, safe="")
    try:
        response = httpx.post(
            f"{SEARCH_CONSOLE_API_BASE}/sites/{encoded_site}/searchAnalytics/query",
            headers={"Authorization": f"Bearer {access_token}"},
            json=body,
            timeout=20,
        )
    except httpx.HTTPError as e:
        raise GoogleSearchConsoleError(f"searchAnalytics query failed: {e}") from e
    if response.status_code != 200:
        raise GoogleSearchConsoleError(f"searchAnalytics query failed ({response.status_code}): {response.text}")
    rows = response.json().get("rows", [])
    return rows


def _rows_to_dicts(rows: list[dict], dimension_names: list[str]) -> list[dict]:
    out = []
    for row in rows:
        entry = dict(zip(dimension_names, row.get("keys", [])))
        entry["clicks"] = row.get("clicks", 0)
        entry["impressions"] = row.get("impressions", 0)
        entry["ctr"] = row.get("ctr", 0)
        entry["position"] = row.get("position", 0)
        out.append(entry)
    return out


def get_top_queries(db: Session, tenant_id: int, start_date: str, end_date: str, limit: int = 20) -> dict:
    rows = _query(db, tenant_id, {
        "startDate": start_date, "endDate": end_date,
        "dimensions": ["query"], "rowLimit": limit,
    })
    return {"queries": _rows_to_dicts(rows, ["query"])}


def get_top_pages(db: Session, tenant_id: int, start_date: str, end_date: str, limit: int = 20) -> dict:
    rows = _query(db, tenant_id, {
        "startDate": start_date, "endDate": end_date,
        "dimensions": ["page"], "rowLimit": limit,
    })
    return {"pages": _rows_to_dicts(rows, ["page"])}


def get_daily_trend(db: Session, tenant_id: int, start_date: str, end_date: str) -> dict:
    rows = _query(db, tenant_id, {
        "startDate": start_date, "endDate": end_date,
        "dimensions": ["date"],
    })
    return {"days": _rows_to_dicts(rows, ["date"])}
