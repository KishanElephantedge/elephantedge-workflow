"""Google Analytics (GA4) Data API client -- separate integration from Google Calendar
(app/google_calendar_client.py), deliberately not sharing code with it even though both use the
same OAuth2 refresh-token pattern, per explicit instruction to keep this feature standalone.

Auth: same shape as the Calendar integration (OAuth2 client id/secret + a long-lived refresh
token, exchanged for a short-lived access token on every call rather than cached, since calls
are infrequent). The OAuth client itself (google_analytics_client_id/secret) is the same Google
Cloud OAuth client reused for the Search Console integration too (app/google_search_console_client.py)
-- one Google Cloud project, one OAuth client, two separate scopes/refresh tokens, since Google
Workspace's org policy blocks service-account key creation for this org (confirmed live,
2026-08-21) -- OAuth is the only path available, not a stylistic choice.

Requires these Credential rows (see app/db/models.py Credential):
  - google_analytics_client_id
  - google_analytics_client_secret
  - google_analytics_refresh_token
  - google_analytics_property_id (the GA4 numeric property id, e.g. "521427869")
"""
import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

TOKEN_URL = "https://oauth2.googleapis.com/token"
DATA_API_BASE = "https://analyticsdata.googleapis.com/v1beta"


class GoogleAnalyticsError(Exception):
    pass


def _get_credential(name: str, db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == name)
        .first()
    )
    if not cred or not cred.value:
        raise GoogleAnalyticsError(f"{name} credential is not set for this tenant")
    return cred.value


def _get_access_token(db: Session, tenant_id: int) -> str:
    client_id = _get_credential("google_analytics_client_id", db, tenant_id)
    client_secret = _get_credential("google_analytics_client_secret", db, tenant_id)
    refresh_token = _get_credential("google_analytics_refresh_token", db, tenant_id)
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
        raise GoogleAnalyticsError(f"Token refresh failed: {e}") from e
    if response.status_code != 200:
        raise GoogleAnalyticsError(f"Token refresh failed ({response.status_code}): {response.text}")
    return response.json()["access_token"]


def _run_report(db: Session, tenant_id: int, body: dict) -> dict:
    access_token = _get_access_token(db, tenant_id)
    property_id = _get_credential("google_analytics_property_id", db, tenant_id)
    try:
        response = httpx.post(
            f"{DATA_API_BASE}/properties/{property_id}:runReport",
            headers={"Authorization": f"Bearer {access_token}"},
            json=body,
            timeout=20,
        )
    except httpx.HTTPError as e:
        raise GoogleAnalyticsError(f"runReport failed: {e}") from e
    if response.status_code != 200:
        raise GoogleAnalyticsError(f"runReport failed ({response.status_code}): {response.text}")
    return response.json()


def _rows_to_dicts(report: dict) -> list[dict]:
    """GA4's runReport response is column-oriented (dimensionHeaders/metricHeaders + rows of
    positional values) -- this flattens it into a list of plain {name: value} dicts, which is
    both easier for the frontend to consume and easier to unit-reason about than re-zipping
    headers/values at every call site."""
    dim_names = [h["name"] for h in report.get("dimensionHeaders", [])]
    metric_names = [h["name"] for h in report.get("metricHeaders", [])]
    out = []
    for row in report.get("rows", []):
        entry = {}
        for name, v in zip(dim_names, row.get("dimensionValues", [])):
            entry[name] = v.get("value")
        for name, v in zip(metric_names, row.get("metricValues", [])):
            # GA4 always returns metric values as strings -- cast to int/float here once so
            # every caller gets real numbers, not string-typed numbers to re-parse themselves.
            raw = v.get("value")
            try:
                entry[name] = int(raw)
            except (TypeError, ValueError):
                try:
                    entry[name] = float(raw)
                except (TypeError, ValueError):
                    entry[name] = raw
        out.append(entry)
    return out


def get_traffic_overview(db: Session, tenant_id: int, start_date: str = "7daysAgo", end_date: str = "today") -> dict:
    """Sessions/users broken down by traffic channel (Direct, Organic Search, Referral, etc.)
    -- the headline "where is our inbound traffic coming from" view."""
    report = _run_report(db, tenant_id, {
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [{"name": "sessionDefaultChannelGroup"}],
        "metrics": [{"name": "sessions"}, {"name": "activeUsers"}, {"name": "conversions"}],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
    })
    return {"channels": _rows_to_dicts(report)}


def get_top_pages(db: Session, tenant_id: int, start_date: str = "7daysAgo", end_date: str = "today", limit: int = 10) -> dict:
    report = _run_report(db, tenant_id, {
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [{"name": "screenPageViews"}, {"name": "activeUsers"}],
        "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
        "limit": limit,
    })
    return {"pages": _rows_to_dicts(report)}


def get_daily_trend(db: Session, tenant_id: int, start_date: str = "30daysAgo", end_date: str = "today") -> dict:
    """Day-by-day sessions/users -- the time-series data a trend chart needs."""
    report = _run_report(db, tenant_id, {
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "sessions"}, {"name": "activeUsers"}],
        "orderBys": [{"dimension": {"dimensionName": "date"}}],
    })
    return {"days": _rows_to_dicts(report)}
