"""What the system does with a meeting once it has one.

Four capabilities, in the order they earn their keep:

  1. draft_outcome_for_booking  -- proposes the outcome a human has never once typed in
  2. get_meeting_brief          -- what happened last time, and what we owe this person
  3. get_open_commitments       -- what we promised across all meetings and never closed
  4. summarise_meeting_history  -- the corpus the chat agent reasons over

NOTHING HERE WRITES AN OUTCOME. Every function returns a proposal or a briefing. Confirmation
goes through record_meeting_outcome(), which validates against real companies and configured
offerings. An LLM reading a transcript is a good summariser and an unreliable witness; letting it
book revenue unattended would put invented numbers into the one place the business reads as fact.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import CalendarBooking
from app.gtm_os.meetings.meeting_notes import MeetingNote
from app.llm_client import generate_json

logger = logging.getLogger(__name__)

# A transcript can run tens of thousands of tokens. Granola's own AI summary is the high-signal
# part, so it is always sent whole and the transcript is truncated behind it -- that keeps the
# prompt affordable on the free LLM tier without discarding the part that matters most.
MAX_TRANSCRIPT_CHARS = 12_000


def _note_for_booking(db: Session, tenant_id: int, booking_id: int) -> MeetingNote | None:
    return (db.query(MeetingNote)
            .filter(MeetingNote.tenant_id == tenant_id,
                    MeetingNote.calendar_booking_id == booking_id)
            .order_by(MeetingNote.id.desc()).first())


def _context(note: MeetingNote) -> str:
    parts = []
    if note.title:
        parts.append(f"MEETING: {note.title}")
    if note.note_created_at:
        parts.append(f"DATE: {note.note_created_at:%Y-%m-%d %H:%M} UTC")
    if note.summary:
        parts.append(f"\nGRANOLA'S SUMMARY:\n{note.summary}")
    if note.transcript_text:
        body = note.transcript_text[:MAX_TRANSCRIPT_CHARS]
        truncated = " [truncated]" if len(note.transcript_text) > MAX_TRANSCRIPT_CHARS else ""
        parts.append(f"\nTRANSCRIPT{truncated}:\n{body}")
    return "\n".join(parts)


def draft_outcome_for_booking(db: Session, tenant_id: int, booking_id: int) -> dict:
    """Propose the outcome fields for one meeting. Writes nothing."""
    from app.gtm_os.opportunity.offering_config import get_offering_config

    booking = db.get(CalendarBooking, booking_id)
    if booking is None:
        return {"status": "failed", "reason": f"booking {booking_id} not found"}
    note = _note_for_booking(db, tenant_id, booking_id)
    if note is None:
        return {"status": "no_note", "reason": "no Granola note is linked to this booking"}

    offerings = [o["name"] for o in get_offering_config(db, tenant_id)]
    prompt = f"""You are reviewing a real sales meeting for Elephant Edge, a B2B sales consulting company.

{_context(note)}

Configured offerings you may reference: {', '.join(offerings)}

Return JSON only:
{{
  "outcome_status": "won" | "lost" | null,
  "status_reasoning": "one sentence on why, quoting what was actually said",
  "amount_usd": number or null,
  "offering_name": one of the configured offerings, or null,
  "loss_reason": "why it was lost, if lost" or null,
  "summary": "3-4 sentences: what this meeting was, what was decided",
  "next_steps": ["concrete next actions that were actually agreed"],
  "our_commitments": ["things WE said we would do"],
  "their_commitments": ["things THEY said they would do"],
  "objections": ["real objections or hesitations raised"],
  "buying_signals": ["evidence they intend to move forward"],
  "confidence": "high" | "medium" | "low"
}}

RULES, and they matter more than completeness:
- outcome_status is "won" ONLY if money was actually agreed. A warm call is NOT a win.
- outcome_status is "lost" ONLY if they declined. An unresolved conversation is null.
- amount_usd only if a real figure was discussed. Never estimate one. Null otherwise.
- Every field must be grounded in what was said. If it was not discussed, use null or [].
- Do not infer enthusiasm into a commitment."""

    try:
        draft = generate_json(prompt, db, tenant_id, max_tokens=1500)
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "reason": f"LLM unavailable: {type(e).__name__}: {e}"[:200]}

    return {
        "status": "drafted",
        "booking_id": booking_id,
        "note_id": note.id,
        "meeting_title": note.title,
        "meeting_date": note.note_created_at.isoformat() if note.note_created_at else None,
        "draft": draft,
        "confirm_with": ("record_meeting_outcome(db, tenant_id, booking_id, status, company_id, "
                         "offering_name, amount_usd, reason, notes, recorded_by) -- a human must confirm; "
                         "this draft is not recorded anywhere"),
    }


def get_meeting_brief(db: Session, tenant_id: int, person_email: str | None = None,
                      booking_id: int | None = None) -> dict:
    """What you need in your head before walking into the next call with this person.

    Answers the real question -- "what did we agree last time and what do I still owe them" --
    from actual transcripts rather than memory.
    """
    q = db.query(MeetingNote).filter(MeetingNote.tenant_id == tenant_id)
    if booking_id is not None:
        booking = db.get(CalendarBooking, booking_id)
        if booking is None:
            return {"status": "failed", "reason": f"booking {booking_id} not found"}
        person_email = person_email or booking.booker_email
    if person_email:
        bookings = [b.id for b in db.query(CalendarBooking)
                    .filter(CalendarBooking.booker_email == person_email).all()]
        if not bookings:
            return {"status": "no_history", "person": person_email,
                    "reason": "no past meeting with this email"}
        q = q.filter(MeetingNote.calendar_booking_id.in_(bookings))
    notes = q.order_by(MeetingNote.note_created_at.desc()).limit(5).all()
    if not notes:
        return {"status": "no_history", "person": person_email,
                "reason": "meetings exist but no Granola note is linked to them"}

    history = "\n\n---\n\n".join(_context(n) for n in notes)
    prompt = f"""You are briefing the CEO of Elephant Edge before a call. Below are the real notes
and transcripts of the last {len(notes)} meeting(s) with this person.

{history}

Return JSON only:
{{
  "who_they_are": "one line",
  "relationship_state": "where this stands right now",
  "what_we_promised": ["commitments WE made that are still open"],
  "what_they_promised": ["commitments THEY made that are still open"],
  "open_questions": ["things left unresolved"],
  "last_meeting_summary": "3 sentences",
  "suggested_focus": ["what to actually cover in the next call, most important first"],
  "risks": ["anything that could go wrong, e.g. an unanswered ask or a stalled decision"]
}}

Ground every item in what was said. Empty list if it was not discussed."""
    try:
        brief = generate_json(prompt, db, tenant_id, max_tokens=1500)
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "reason": f"LLM unavailable: {type(e).__name__}: {e}"[:200]}

    return {"status": "ready", "person": person_email, "meetings_reviewed": len(notes),
            "most_recent": notes[0].note_created_at.isoformat() if notes[0].note_created_at else None,
            "brief": brief}


def get_open_commitments(db: Session, tenant_id: int, limit: int = 20) -> dict:
    """Everything we said we would do across recent meetings, and never closed.

    This is the CEO question the system could not answer before: not "what happened", but
    "what did I promise people and which of those have I not done".
    """
    notes = (db.query(MeetingNote)
             .filter(MeetingNote.tenant_id == tenant_id,
                     MeetingNote.summary.isnot(None))
             .order_by(MeetingNote.note_created_at.desc()).limit(limit).all())
    if not notes:
        return {"status": "no_notes", "commitments": []}

    blocks = []
    for n in notes:
        who = ""
        if n.calendar_booking_id:
            b = db.get(CalendarBooking, n.calendar_booking_id)
            who = f" with {b.booker_email}" if b and b.booker_email else ""
        blocks.append(f"MEETING: {n.title}{who} ({n.note_created_at:%Y-%m-%d})\n{n.summary}"
                      if n.note_created_at else f"MEETING: {n.title}{who}\n{n.summary}")
    prompt = f"""Across these {len(notes)} recent meetings for Elephant Edge, list every commitment
made and who owns it.

{chr(10).join(blocks)}

Return JSON only:
{{"commitments": [
  {{"owner": "us" | "them", "what": "the commitment", "for_whom": "person or company",
    "meeting": "meeting title", "due": "any date mentioned, else null",
    "status_guess": "open" | "probably_done", "why": "one line"}}
]}}
Only real commitments that were actually stated. Do not invent follow-ups that sound sensible."""
    try:
        data = generate_json(prompt, db, tenant_id, max_tokens=2000)
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "reason": f"LLM unavailable: {type(e).__name__}: {e}"[:200]}
    return {"status": "ready", "meetings_reviewed": len(notes),
            "commitments": data.get("commitments", [])}


def meeting_coverage(db: Session, tenant_id: int) -> dict:
    """How much of the meeting history the system can actually see -- the honest denominator."""
    total_bookings = db.query(CalendarBooking).count()
    notes = db.query(MeetingNote).filter(MeetingNote.tenant_id == tenant_id).count()
    linked = (db.query(MeetingNote)
              .filter(MeetingNote.tenant_id == tenant_id,
                      MeetingNote.calendar_booking_id.isnot(None)).count())
    with_outcome = (db.query(CalendarBooking)
                    .filter(CalendarBooking.outcome_status.isnot(None)).count())
    return {"calendar_bookings": total_bookings, "granola_notes": notes,
            "notes_linked_to_a_booking": linked,
            "bookings_with_a_confirmed_outcome": with_outcome,
            "bookings_we_could_draft_an_outcome_for": linked - with_outcome}
