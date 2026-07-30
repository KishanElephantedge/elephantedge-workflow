"""Google Calendar sync -- pulls bookings made through the lead's Appointment Schedule
(clients book a call directly off the website). There's no webhook/push option for
appointment-schedule bookings specifically (unlike SalesRobot's outbound webhook), so this is
a periodic pull via the standard Calendar API v3 events.list, same account the appointment
schedule itself lives on.

Requires one-time OAuth2 setup done manually (Google Cloud Console project, OAuth client,
consent screen, then a refresh token obtained via Google's own OAuth flow or the OAuth
Playground) -- the three resulting values are stored as Credential rows:
  - google_calendar_client_id
  - google_calendar_client_secret
  - google_calendar_refresh_token
and google_calendar_id (which calendar to read -- "primary" if it's the lead's main calendar,
or the specific calendar ID the appointment schedule publishes bookings to).

This module only ever calls the free, standard Calendar API -- no Deepline, no per-call cost."""
import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL_TEMPLATE = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"


class GoogleCalendarError(Exception):
    pass


def _get_credential(name: str, db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == name)
        .first()
    )
    if not cred or not cred.value:
        raise GoogleCalendarError(f"{name} credential is not set for this tenant")
    return cred.value


def _get_access_token(db: Session, tenant_id: int) -> str:
    """Exchanges the stored long-lived refresh token for a short-lived access token -- done
    on every sync rather than caching, since access tokens expire in ~1 hour and syncs are
    infrequent (periodic, not per-request)."""
    client_id = _get_credential("google_calendar_client_id", db, tenant_id)
    client_secret = _get_credential("google_calendar_client_secret", db, tenant_id)
    refresh_token = _get_credential("google_calendar_refresh_token", db, tenant_id)

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
        raise GoogleCalendarError(f"Token refresh failed: {e}") from e
    if response.status_code != 200:
        raise GoogleCalendarError(f"Token refresh failed ({response.status_code}): {response.text}")
    return response.json()["access_token"]


def fetch_events(db: Session, tenant_id: int, time_min: str, time_max: str | None = None, max_results: int = 250) -> list[dict]:
    """time_min/time_max are RFC3339 timestamps (e.g. "2026-07-01T00:00:00Z"). Returns raw
    Google Calendar event objects -- caller (sync_calendar_bookings) maps them to
    CalendarBooking rows."""
    access_token = _get_access_token(db, tenant_id)
    calendar_id = _get_credential("google_calendar_id", db, tenant_id)

    params = {
        "timeMin": time_min,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": str(max_results),
    }
    if time_max:
        params["timeMax"] = time_max

    try:
        response = httpx.get(
            EVENTS_URL_TEMPLATE.format(calendar_id=calendar_id),
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
    except httpx.HTTPError as e:
        raise GoogleCalendarError(f"events.list failed: {e}") from e
    if response.status_code != 200:
        raise GoogleCalendarError(f"events.list failed ({response.status_code}): {response.text}")
    return response.json().get("items", [])
