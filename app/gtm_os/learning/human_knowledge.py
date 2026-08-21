"""Human-Provided Knowledge -- the learning-loop input the spec calls "teach the GTM OS
something" (architecture upgrade, Parts 6-9).

CLOSEST EXISTING ANALOG, REUSED IN SHAPE (not code): ConfirmedPattern (overrides_evals.py) is the
one existing durable, human-confirmed, provenance-tracked record in this codebase. This model
follows the same discipline -- explicit status lifecycle, a human identity field, created_at --
but is NOT the same table, because a ConfirmedPattern is DERIVED from real system events
(rejected messages, lost-meeting reasons) whereas HumanKnowledge is a human's own direct free-text
statement with no system-derived trigger. Conflating the two would blur exactly the provenance
distinction Part 6 requires.

PROVENANCE (Part 6) -- every row always has: original_text (verbatim, never edited), source
(always "human_input" -- the only source this table records), created_at, created_by, tenant_id,
status, interpretation (nullable, inspectable). This is NOT a "confirmed business rule" the moment
it's submitted: `status` starts "pending_review" the moment original_text + interpretation are
saved, and only becomes "confirmed" via an explicit human action (confirm_human_knowledge()) or
"dismissed" (dismiss_human_knowledge()) -- exactly the "pending / confirmed" states the spec's own
example response shows.

INTERPRETATION (Part 6) -- one, single, bounded Claude call (reuses call_claude(), the existing
single-turn helper the rest of this backend already uses -- no new LLM client) turns the free-text
statement into a structured, INSPECTABLE heuristic (a plain dict, always shown alongside the
original text, never replacing it). If the call fails for any reason (no API key configured, rate
limit, malformed response), the original_text is still saved -- interpretation=None,
status="pending_interpretation" -- the row is never lost or blocked on the LLM being available.

GOVERNANCE (Part 9) -- THIS MODULE NEVER WRITES TO ICP CONFIG, OFFERING CONFIG, GTM MOTION CONFIG,
BUSINESS-CONTEXT GOALS, MESSAGEDRAFT, OR ANY OTHER AUTHORITATIVE CONFIGURATION. confirm_/dismiss_
human_knowledge() are the ONLY writers of this table, and neither ever touches anything else --
same explicit boundary confirm_pattern()/dismiss_pattern() already establish. A confirmed
HumanKnowledge row is surfaced as read-only context to a human (see get_overrides_evals()'s
human_knowledge field) and, optionally, as a labeled note alongside an offering recommendation
(offering_recommendation.py) -- it never mechanically changes what that recommendation is."""

import json
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session

from app.claude_client import ClaudeError, call_claude
from app.db.models import Base

VALID_STATUSES = {"pending_review", "pending_interpretation", "confirmed", "dismissed"}

_INTERPRETATION_SYSTEM_PROMPT = (
    "You turn one free-text business observation about go-to-market strategy into a small, "
    "structured JSON heuristic. Respond with ONLY a JSON object, no prose, no markdown fences. "
    'Shape: {"summary": "<one plain-language sentence restating the statement as a heuristic>", '
    '"signal_type": "<one of: icp_signal, offering_signal, motion_signal, general>", '
    '"condition": "<short plain-language condition, e.g. \'sales_hiring_count > 10\'>", '
    '"implication": "<short plain-language implication, e.g. \'sales_enablement demand signal\'>"}. '
    "Never invent facts not present in the statement. If the statement is too vague to extract a "
    'condition/implication, use null for those two fields but still provide a summary.'
)


class HumanKnowledge(Base):
    """One human-submitted knowledge statement. Never auto-created, never auto-confirmed -- see
    module docstring."""
    __tablename__ = "human_knowledge"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    original_text = Column(Text, nullable=False)
    source = Column(String, nullable=False, default="human_input")
    interpretation = Column(Text, nullable=True)  # JSON string, or None if interpretation failed
    status = Column(String, nullable=False, default="pending_review")

    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    confirmed_by = Column(String, nullable=True)
    confirmed_at = Column(DateTime, nullable=True)


def _interpret(db: Session, tenant_id: int, text: str) -> dict | None:
    try:
        raw = call_claude(text, db, tenant_id, system=_INTERPRETATION_SYSTEM_PROMPT, max_tokens=400)
        parsed = json.loads(raw.strip())
        if not isinstance(parsed, dict):
            return None
        return parsed
    except (ClaudeError, json.JSONDecodeError, ValueError):
        return None


def _serialize(row: HumanKnowledge) -> dict:
    return {
        "id": row.id,
        "original_text": row.original_text,
        "source": row.source,
        "interpretation": json.loads(row.interpretation) if row.interpretation else None,
        "status": row.status,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "confirmed_by": row.confirmed_by,
        "confirmed_at": row.confirmed_at,
    }


def submit_human_knowledge(db: Session, tenant_id: int, text: str, created_by: str | None) -> dict:
    """The only writer of a brand-new row. original_text is stored exactly as supplied, before any
    interpretation attempt -- a failed interpretation never loses the human's own words."""
    if not text or not text.strip():
        raise ValueError("text is required")

    interpretation = _interpret(db, tenant_id, text.strip())
    row = HumanKnowledge(
        tenant_id=tenant_id,
        original_text=text.strip(),
        source="human_input",
        interpretation=json.dumps(interpretation) if interpretation is not None else None,
        status="pending_review" if interpretation is not None else "pending_interpretation",
        created_by=created_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize(row)


def list_human_knowledge(db: Session, tenant_id: int, status: str | None = None, limit: int = 200) -> list[dict]:
    query = db.query(HumanKnowledge).filter(HumanKnowledge.tenant_id == tenant_id)
    if status is not None:
        query = query.filter(HumanKnowledge.status == status)
    rows = query.order_by(HumanKnowledge.id.desc()).limit(limit).all()
    return [_serialize(r) for r in rows]


def _get_owned_row(db: Session, tenant_id: int, knowledge_id: int) -> HumanKnowledge:
    row = db.get(HumanKnowledge, knowledge_id)
    if row is None or row.tenant_id != tenant_id:
        raise LookupError(f"HumanKnowledge {knowledge_id} not found")
    return row


def confirm_human_knowledge(db: Session, tenant_id: int, knowledge_id: int, confirmed_by: str | None) -> dict:
    """Writes ONLY this row's status/confirmed_by/confirmed_at -- see module docstring's
    governance boundary. Never touches ICP/offering/GTM-motion config or any other table."""
    row = _get_owned_row(db, tenant_id, knowledge_id)
    row.status = "confirmed"
    row.confirmed_by = confirmed_by
    row.confirmed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _serialize(row)


def dismiss_human_knowledge(db: Session, tenant_id: int, knowledge_id: int, confirmed_by: str | None) -> dict:
    """Same write-boundary as confirm_human_knowledge() above."""
    row = _get_owned_row(db, tenant_id, knowledge_id)
    row.status = "dismissed"
    row.confirmed_by = confirmed_by
    row.confirmed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _serialize(row)
