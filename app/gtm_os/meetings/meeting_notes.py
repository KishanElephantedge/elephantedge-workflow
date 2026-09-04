"""Granola meeting notes: ingest, link to a booking, and draft the outcome.

THE PROBLEM. record_meeting_outcome/get_revenue_pace have existed for weeks and hold ZERO rows
against 15 real bookings. The loop is not broken -- the human step in front of it never happens.
Nobody stops after a call to type in what happened, so revenue pace has nothing to report and the
"AI CEO" loop the product is named for has no input.

THE FIX IS NOT MORE UI. It is to remove the step: Granola already recorded the meeting, wrote a
summary and kept the transcript. Read it, draft the outcome, and ask a human to confirm rather
than to compose.

A DRAFT IS NOT A RECORD. draft_outcome_for_booking() returns a proposal and writes nothing to the
outcome fields. Confirmation goes through record_meeting_outcome() exactly as a human-entered one
does, with its existing validation (real company, configured offering, valid status). An LLM
reading a transcript is a good summariser and a bad witness -- it should never be able to book
revenue on its own.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Session

from app.db.models import Base, CalendarBooking
from app.granola_client import GranolaNotConfigured, get_note, iter_notes, transcript_to_text

logger = logging.getLogger(__name__)

# How far apart a Granola note and a calendar booking may start and still be the same meeting.
# Granola timestamps when RECORDING began, which is routinely a few minutes after the scheduled
# start (people join late) and occasionally before (people join early), so this is deliberately
# generous in both directions -- but far tighter than a day, or back-to-back calls would collide.
MATCH_WINDOW_MINUTES = 90


class MeetingNote(Base):
    """One Granola note. Its own table, not a column on CalendarBooking.

    Transcripts are large and client-confidential; keeping them separate means every existing
    query over bookings stays cheap and nothing accidentally selects a transcript into a payload
    that was never meant to carry one.
    """
    __tablename__ = "meeting_notes"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    granola_note_id = Column(String, nullable=False)   # unique per tenant, see ensure_indexes
    title = Column(String, nullable=True)
    owner_name = Column(String, nullable=True)
    owner_email = Column(String, nullable=True)
    summary = Column(Text, nullable=True)              # Granola's own AI summary
    transcript_text = Column(Text, nullable=True)      # flattened, speaker-labelled
    note_created_at = Column(DateTime, nullable=True)

    # Null when no booking could be matched with confidence. A wrong link is worse than none:
    # it would attribute one client's conversation to another's account.
    calendar_booking_id = Column(Integer, ForeignKey("calendar_bookings.id"), nullable=True)
    match_method = Column(String, nullable=True)
    match_confidence = Column(String, nullable=True)   # "high" | "medium" | "none"

    raw_payload = Column(JSON, nullable=True)
    ingested_at = Column(DateTime, default=datetime.utcnow)


def _parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _fold(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def match_booking(db: Session, tenant_id: int, note: dict, note_created: datetime | None) -> tuple[int | None, str, str]:
    """Link a note to the booking it belongs to. Returns (booking_id, method, confidence).

    Time alone is not enough -- two calls can sit in the same window -- and title alone is not
    enough either, since Granola titles are often the person's name while the calendar summary is
    something else. So: narrow by time first, then prefer a title or attendee-email corroboration.
    Anything uncorroborated with more than one candidate is left UNMATCHED on purpose.
    """
    if note_created is None:
        return None, "no_timestamp_on_note", "none"

    window = timedelta(minutes=MATCH_WINDOW_MINUTES)
    candidates = (
        db.query(CalendarBooking)
        .filter(CalendarBooking.start_time >= note_created - window,
                CalendarBooking.start_time <= note_created + window)
        .all()
    )
    if not candidates:
        return None, "no_booking_in_time_window", "none"
    if len(candidates) == 1:
        return candidates[0].id, "single_booking_in_time_window", "high"

    title = _fold(note.get("title"))
    owner_email = ((note.get("owner") or {}).get("email") or "").lower()
    for booking in candidates:
        summary = _fold((booking.raw_payload or {}).get("summary"))
        booker = (booking.booker_email or "").lower()
        booker_name = _fold(booking.booker_name)
        if title and summary and (title in summary or summary in title):
            return booking.id, "time_window_plus_title", "high"
        if owner_email and booker and owner_email == booker:
            return booking.id, "time_window_plus_email", "high"
        if title and booker_name and booker_name in title:
            return booking.id, "time_window_plus_booker_name", "high"

    # Several plausible bookings and nothing to separate them. Attributing one client's transcript
    # to another client's account is the failure that matters here, so we decline.
    return None, f"ambiguous_{len(candidates)}_bookings_in_window", "none"


def ingest_granola_notes(db: Session, tenant_id: int, created_after: str | None = None,
                         max_pages: int = 20, fetch_transcripts: bool = True) -> dict:
    """Pull notes and store them, linked to a booking where that can be established."""
    result = {"status": "completed", "seen": 0, "created": 0, "updated": 0,
              "matched": 0, "unmatched": 0, "errors": []}
    try:
        notes = list(iter_notes(db, tenant_id, created_after=created_after, max_pages=max_pages))
    except GranolaNotConfigured as e:
        return {"status": "not_configured", "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "reason": str(e)[:300]}

    for note in notes:
        result["seen"] += 1
        note_id = note.get("id")
        if not note_id:
            continue
        try:
            if fetch_transcripts and "transcript" not in note:
                note = get_note(db, tenant_id, note_id, include_transcript=True)

            created = _parse_dt(note.get("created_at") or note.get("createdAt"))
            row = (db.query(MeetingNote)
                   .filter(MeetingNote.tenant_id == tenant_id,
                           MeetingNote.granola_note_id == note_id).first())
            is_new = row is None
            if is_new:
                row = MeetingNote(tenant_id=tenant_id, granola_note_id=note_id)
                db.add(row)

            owner = note.get("owner") or {}
            row.title = note.get("title")
            row.owner_name = owner.get("name")
            row.owner_email = owner.get("email")
            row.summary = note.get("summary")
            row.transcript_text = transcript_to_text(note.get("transcript") or [])
            row.note_created_at = created
            row.raw_payload = note

            booking_id, method, confidence = match_booking(db, tenant_id, note, created)
            row.calendar_booking_id = booking_id
            row.match_method = method
            row.match_confidence = confidence
            db.commit()

            result["created" if is_new else "updated"] += 1
            result["matched" if booking_id else "unmatched"] += 1
        except Exception as e:  # noqa: BLE001 -- one bad note must not lose the sync
            db.rollback()
            result["errors"].append(f"{note_id}: {type(e).__name__}: {e}"[:200])
    return result
