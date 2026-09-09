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

    from app.gtm_os.content.content_business_context import get_content_business_context

    business_name = get_content_business_context(db, tenant_id).get("business_name") or "the business"
    history = "\n\n---\n\n".join(_context(n) for n in notes)
    prompt = f"""You are briefing the CEO of {business_name} before a call. Below are the real notes
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
    from app.gtm_os.content.content_business_context import get_content_business_context

    business_name = get_content_business_context(db, tenant_id).get("business_name") or "the business"
    prompt = f"""Across these {len(notes)} recent meetings for {business_name}, list every commitment
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
        # 2000 was tuned for a handful of meetings; with the default limit=20 real meetings each
        # contributing several commitments, the response was measured truncating mid-JSON-string
        # (both Gemini and its Claude fallback) rather than a model-quality issue -- raised to give
        # real headroom for the actual output size, not a guess.
        data = generate_json(prompt, db, tenant_id, max_tokens=4000)
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "reason": f"LLM unavailable: {type(e).__name__}: {e}"[:200]}
    return {"status": "ready", "meetings_reviewed": len(notes),
            "commitments": data.get("commitments", [])}


def draft_post_from_meetings(db: Session, tenant_id: int, angle: str, platform: str = "linkedin",
                              limit: int = 10) -> dict:
    """Write a real, ready-to-post draft grounded in recent meetings directly -- deliberately NOT
    routed through content_opportunity.py's approve/generate_opportunity_for_topic pipeline, which
    requires market-trend evidence (competitor content, web search signals) that a meeting-grounded
    request has no reason to need. Meetings are their own evidence source; forcing this request
    through the trend-opportunity gate is what previously produced "not enough market trend
    evidence" dead ends and multi-turn back-and-forth for something that should draft directly.
    """
    from app.gtm_os.content.content_opportunity import PLATFORM_BRIEF, VALID_PLATFORMS

    if platform not in VALID_PLATFORMS:
        return {"status": "invalid_platform", "reason": f"platform must be one of {sorted(VALID_PLATFORMS)}, got {platform!r}"}

    notes = (db.query(MeetingNote)
             .filter(MeetingNote.tenant_id == tenant_id, MeetingNote.summary.isnot(None))
             .order_by(MeetingNote.note_created_at.desc()).limit(limit).all())
    if not notes:
        return {"status": "no_notes", "reason": "no meeting notes with a summary exist yet"}

    blocks = "\n\n---\n\n".join(
        f"MEETING: {n.title} ({n.note_created_at:%Y-%m-%d})\n{n.summary}" if n.note_created_at
        else f"MEETING: {n.title}\n{n.summary}"
        for n in notes
    )

    prompt = f"""Write a real, publishable {PLATFORM_BRIEF[platform]}

Angle to write about: {angle}

Real recent meetings this should be grounded in (the underlying insight/theme, not a transcript
to copy from):
{blocks}

RULES, and they matter more than sounding polished:
- Extract the real THEME or insight these meetings actually show about this angle -- do not quote
  or closely paraphrase any single meeting, and never name a real person, client, or company that
  appears in the meeting notes above inside the post itself.
- NEVER invent a statistic, dollar amount, percentage, count, or timeframe that is not explicitly
  stated in the meetings above (no "$200K", no "15-20 per quarter", no invented years of
  experience). If you don't have a real number, make the point qualitatively instead of inventing
  a precise-sounding one -- a false-precision number is worse than none.
- This must be genuinely ready to post as-is: the post itself only, no meta-commentary explaining
  why it works, no placeholders, no bracketed notes.

Return JSON only:
{{"draft_text": "the post, ready to publish as-is", "grounded_in": ["one short phrase per real meeting insight actually used, for the human's own reference -- not part of the post"]}}"""

    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=1200)
    except Exception as e:  # noqa: BLE001
        return {"status": "llm_unavailable", "error": str(e)}

    draft_text = response.get("draft_text") if isinstance(response, dict) else None
    if not draft_text:
        return {"status": "discarded", "reason": "no draft_text returned"}

    return {
        "status": "ok", "platform": platform, "draft_text": draft_text,
        "grounded_in": response.get("grounded_in", []),
        "meetings_considered": len(notes),
    }


# Real, structural fact, not a policy choice: CalendarBooking has no tenant_id column at all --
# it's populated by ONE periodic sync of Elephant Edge's own Google Calendar
# (google_calendar_client.py), never scoped per tenant. Real bug found live (2026-09-09): this
# function used to run `db.query(CalendarBooking).count()` unconditionally, so a partner tenant's
# content chat reported ELEPHANT EDGE'S OWN real booking count as if it were that partner's --
# confirmed live, Jeff Platt's chat said "you have 24 calendar bookings" using Elephant Edge's
# real number. Gated behind ELEPHANT_EDGE_TENANT_ID until CalendarBooking is genuinely
# multi-tenant (a real schema change, not done here) -- any other tenant gets an honest 0/None
# rather than someone else's real numbers.
ELEPHANT_EDGE_TENANT_ID = 2


def meeting_coverage(db: Session, tenant_id: int) -> dict:
    """How much of the meeting history the system can actually see -- the honest denominator."""
    if tenant_id != ELEPHANT_EDGE_TENANT_ID:
        notes = db.query(MeetingNote).filter(MeetingNote.tenant_id == tenant_id).count()
        return {"calendar_bookings": None, "granola_notes": notes,
                "notes_linked_to_a_booking": 0,
                "bookings_with_a_confirmed_outcome": None,
                "bookings_we_could_draft_an_outcome_for": None,
                "note": "Calendar booking sync isn't available for this tenant yet -- only Granola notes not linked to a booking are visible."}
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
