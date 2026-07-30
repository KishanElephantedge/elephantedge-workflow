"""Maps raw Google Calendar events (from google_calendar_client.fetch_events) into
CalendarBooking rows. The exact shape of an Appointment Schedule booking's attendee/creator
fields hasn't been seen against a real account yet -- same "store everything, refine matching
once real data arrives" approach as the SalesRobot webhook (see CampaignEvent): the full raw
event is always kept regardless of whether name/email extraction below guesses right."""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import CalendarBooking
from app.google_calendar_client import fetch_events


def _parse_event_time(time_obj: dict | None) -> datetime | None:
    if not time_obj:
        return None
    raw = time_obj.get("dateTime") or time_obj.get("date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_booker(event: dict) -> tuple[str | None, str | None]:
    """Best-effort: the booker is usually the non-organizer attendee on an appointment-schedule
    event. Falls back to the event creator if there are no attendees (shape not yet confirmed
    against a real booking)."""
    organizer_email = (event.get("organizer") or {}).get("email")
    for attendee in event.get("attendees", []):
        if attendee.get("email") != organizer_email:
            return attendee.get("displayName"), attendee.get("email")
    creator = event.get("creator", {})
    return creator.get("displayName"), creator.get("email")


def sync_calendar_bookings(db: Session, tenant_id: int, days_ahead: int = 30, days_back: int = 1) -> dict:
    """Pulls events in [now - days_back, now + days_ahead] and upserts them by
    google_event_id. Small days_back window (default 1 day) since this is meant to run
    frequently and only needs to catch anything just booked/modified -- days_ahead is wider
    since bookings are made in advance."""
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")

    events = fetch_events(db, tenant_id, time_min=time_min, time_max=time_max)

    created = updated = 0
    for event in events:
        google_event_id = event.get("id")
        if not google_event_id:
            continue
        booker_name, booker_email = _extract_booker(event)

        existing = db.query(CalendarBooking).filter(CalendarBooking.google_event_id == google_event_id).first()
        if existing:
            existing.booker_name = booker_name
            existing.booker_email = booker_email
            existing.start_time = _parse_event_time(event.get("start"))
            existing.end_time = _parse_event_time(event.get("end"))
            existing.status = event.get("status")
            existing.raw_payload = event
            existing.synced_at = datetime.utcnow()
            updated += 1
        else:
            db.add(CalendarBooking(
                google_event_id=google_event_id,
                booker_name=booker_name,
                booker_email=booker_email,
                start_time=_parse_event_time(event.get("start")),
                end_time=_parse_event_time(event.get("end")),
                status=event.get("status"),
                raw_payload=event,
                synced_at=datetime.utcnow(),
            ))
            created += 1
    db.commit()
    return {"fetched": len(events), "created": created, "updated": updated}
