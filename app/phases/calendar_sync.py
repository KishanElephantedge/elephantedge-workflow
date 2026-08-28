"""Maps raw Google Calendar events (from google_calendar_client.fetch_events) into
CalendarBooking rows. Confirmed live against a real account (2026-08-28): reading the account
owner's calendar returns EVERY event on it, not just Appointment Schedule bookings -- the
owner's own unrelated meetings (coaching calls, standing calls, etc.) come through the same
feed. `_extract_booker` must therefore positively identify a genuine external attendee, not
just "not the organizer" -- the organizer field on a real booking is often a synthetic
scheduling-calendar address, not the owner's own email, so that alone doesn't distinguish
"someone else booked this" from "the owner is just an attendee on their own thing". Events with
no identifiable external attendee are treated as noise and skipped entirely, not stored with a
misleading booker."""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import CalendarBooking
from app.google_calendar_client import fetch_events
from app.notifications import create_notification


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
    """The booker is a real attendee who is neither the account owner (Google marks the
    owner's own attendee entry `self: true`, regardless of what the organizer field says) nor
    the organizer. Falls back to the event creator only when there are no attendees at all.
    Returns (None, None) when no genuine external party can be identified -- the caller treats
    that as "not a real booking", not as "booking with an unknown person"."""
    organizer_email = (event.get("organizer") or {}).get("email")
    for attendee in event.get("attendees", []):
        if attendee.get("self"):
            continue
        if attendee.get("email") == organizer_email:
            continue
        return attendee.get("displayName"), attendee.get("email")
    if not event.get("attendees"):
        creator = event.get("creator", {})
        if creator.get("email") and creator.get("email") != organizer_email:
            return creator.get("displayName"), creator.get("email")
    return None, None


def sync_calendar_bookings(db: Session, tenant_id: int, days_ahead: int = 30, days_back: int = 1) -> dict:
    """Pulls events in [now - days_back, now + days_ahead] and upserts them by
    google_event_id. Small days_back window (default 1 day) since this is meant to run
    frequently and only needs to catch anything just booked/modified -- days_ahead is wider
    since bookings are made in advance."""
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")

    events = fetch_events(db, tenant_id, time_min=time_min, time_max=time_max)

    created = updated = skipped_no_booker = 0
    for event in events:
        google_event_id = event.get("id")
        if not google_event_id:
            continue
        booker_name, booker_email = _extract_booker(event)
        if not booker_email:
            skipped_no_booker += 1
            continue

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
            start = _parse_event_time(event.get("start"))
            create_notification(
                db, tenant_id, "meeting_booked",
                f"Meeting booked — {booker_name or booker_email or 'unknown'}",
                f"Scheduled for {start.strftime('%b %d, %Y %I:%M %p UTC') if start else 'an unspecified time'}.",
                severity="success",
            )
    db.commit()
    return {"fetched": len(events), "created": created, "updated": updated, "skipped_no_booker": skipped_no_booker}
