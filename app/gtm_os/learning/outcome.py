"""SalesOutcome -- Batch 7 Part C/D/E. Observed reply events, built entirely from the EXISTING
linkedin_reply sensing + deterministic interpretation infrastructure (Steps 10A-11B) -- reuses,
never re-implements, that classification.

REUSE, NOT DUPLICATION (Part D): sense_linkedin_replies() already creates GtmSignal rows for
inbound replies; linkedin_reply_interpretation.py already deterministically classifies them into
InterpretedSignal rows with real event_type values. This module adds nothing to that pipeline --
it only reads InterpretedSignal rows already produced and maps their event_type to an
outcome_category, one hop further than Problem/Demand detection already goes.

A REPLY IS AN OBSERVED EVENT, NOT AN AUTOMATIC JUDGMENT (Part D's own explicit warning): this
module never calls a reply "qualified," "buying," or "won." It only ever reuses whatever
distinction the EXISTING deterministic interpreter already established.

CATEGORY MAPPING (Part E) -- only event_types the existing interpreter actually produces are
mapped, and only to categories that are a genuine, literal restatement of that event_type's own
meaning, never a stronger claim:
    not_interested            -> "not_interested"     (exact restatement)
    timing_deferral           -> "timing_deferral"     (exact restatement)
    pricing_request           -> "positive_reply"      (a concrete commercial ask -- genuinely a
    demo_request               -> "positive_reply"      positive signal, not a claim of "won"/"qualified")
    anything else (problem_statement, solution_question, solution_evaluation_mention, or any
    future/unmapped event_type) -> "reply" (generic: a reply happened; already-existing
    Problem/Demand detection is the correct place for its own specific meaning, not duplicated here)

NOT CURRENTLY OBSERVABLE, DOCUMENTED RATHER THAN FAKED (Part C's own explicit instruction):
"meeting_requested", "meeting_booked", "progressed", "lost" -- no existing interpreter or any
other part of this codebase currently produces evidence for any of these. No mapping logic exists
for them; they are not included in OUTCOME_CATEGORIES."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Session

from app.db.models import Base
from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.opportunity.opportunity import Opportunity

# "connection_accepted" added 2026-08-31: SalesRobot reports it on the campaign record
# (lastActivity == "CONNECTED") and 24 real prospects carried it while the learning layer had no
# way to represent it -- so the single most common real outcome in the funnel was invisible here.
# It is deliberately its OWN category, not folded into "reply": accepting a connection request is
# a materially weaker signal than answering a message, and merging them would inflate reply rates
# and teach the wrong lesson about what actually works.
OUTCOME_CATEGORIES = {"reply", "positive_reply", "not_interested", "timing_deferral", "connection_accepted", "meeting_requested"}

EVENT_TYPE_TO_OUTCOME_CATEGORY: dict[str, str] = {
    "not_interested": "not_interested",
    "timing_deferral": "timing_deferral",
    "pricing_request": "positive_reply",
    "demo_request": "positive_reply",
    "connection_accepted": "connection_accepted",   # exact restatement, same discipline as the two above
    # 2026-08-31: these come from SalesRobot's OWN tagList on the campaign record ("Interested",
    # "Meeting Request") -- a real label applied in the tool, not our inference. The module
    # docstring's note that "meeting_requested" is not observable was true when written and is
    # no longer: it is observable, and faking it is exactly what is NOT happening here.
    "interested_reply": "positive_reply",
    "meeting_requested": "meeting_requested",
}
DEFAULT_OUTCOME_CATEGORY = "reply"


class SalesOutcome(Base):
    """One row per InterpretedSignal (unique constraint on interpreted_signal_id -- see
    ensure_indexes()), reusing InterpretedSignal's own dedup guarantee (built on
    GtmSignal.dedup_key, Step 5 §6) as this table's own idempotency mechanism -- no separate
    dedup logic needed here."""
    __tablename__ = "sales_outcomes"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    interpreted_signal_id = Column(Integer, ForeignKey("interpreted_signals.id"), nullable=False)
    gtm_signal_id = Column(Integer, ForeignKey("gtm_signals.id"), nullable=False)  # denormalized for direct provenance without an extra join, same pattern as Opportunity.problem_hypothesis_id

    # Nullable -- an outcome is only linked to an Opportunity when one can genuinely be traced
    # (same company_id, same tenant); never fabricated (Part L's "preserve the strongest
    # available relationship rather than inventing foreign keys").
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)  # passthrough from the signal, when resolved

    outcome_category = Column(String, nullable=False)  # one of OUTCOME_CATEGORIES
    source_event_type = Column(String, nullable=False)  # the InterpretedSignal.event_type this was derived from -- raw provenance, never lost
    reasoning_note = Column(Text, nullable=True)

    observed_at = Column(DateTime, nullable=True)  # copied from the underlying GtmSignal, never invented
    created_at = Column(DateTime, default=datetime.utcnow)


def _find_opportunity_for_signal(db: Session, tenant_id: int, signal: GtmSignal) -> int | None:
    if signal.company_id is None:
        return None
    row = (
        db.query(Opportunity.id)
        .filter(Opportunity.tenant_id == tenant_id, Opportunity.company_id == signal.company_id)
        .order_by(Opportunity.id.desc())
        .first()
    )
    return row[0] if row else None


def classify_outcome(event_type: str) -> str:
    return EVENT_TYPE_TO_OUTCOME_CATEGORY.get(event_type, DEFAULT_OUTCOME_CATEGORY)


def _already_recorded(db: Session, tenant_id: int, interpreted_signal_id: int) -> bool:
    return (
        db.query(SalesOutcome.id)
        .filter(SalesOutcome.tenant_id == tenant_id, SalesOutcome.interpreted_signal_id == interpreted_signal_id)
        .first()
        is not None
    )


def record_outcome(db: Session, tenant_id: int, interpreted_signal: InterpretedSignal, signal: GtmSignal) -> SalesOutcome:
    category = classify_outcome(interpreted_signal.event_type)
    outcome = SalesOutcome(
        tenant_id=tenant_id,
        interpreted_signal_id=interpreted_signal.id,
        gtm_signal_id=signal.id,
        opportunity_id=_find_opportunity_for_signal(db, tenant_id, signal),
        contact_id=signal.contact_id,
        outcome_category=category,
        source_event_type=interpreted_signal.event_type,
        reasoning_note=f"{interpreted_signal.event_type} -> {category}: {interpreted_signal.business_change}",
        observed_at=signal.observed_at,
    )
    db.add(outcome)
    db.commit()
    return outcome


def run_outcome_detection_sweep(db: Session, tenant_id: int, limit: int = 200) -> dict:
    """Evaluates InterpretedSignal rows sourced from linkedin_reply that don't yet have a
    SalesOutcome. Tenant-scoped, bounded, idempotent (unique interpreted_signal_id constraint +
    check-before-insert), per-item failure isolated. Zero LLM calls, zero external calls -- pure
    read of already-produced InterpretedSignal rows."""
    counts = {"evaluated": 0, "recorded": 0, "already_recorded": 0, "failed": 0}

    rows = (
        db.query(InterpretedSignal.id)
        .join(GtmSignal, InterpretedSignal.source_signal_id == GtmSignal.id)
        .filter(InterpretedSignal.tenant_id == tenant_id, GtmSignal.source == "linkedin_reply")
        .order_by(InterpretedSignal.id)
        .limit(limit)
        .all()
    )

    for (interpreted_signal_id,) in rows:
        try:
            counts["evaluated"] += 1
            if _already_recorded(db, tenant_id, interpreted_signal_id):
                counts["already_recorded"] += 1
                continue
            interpreted_signal = db.get(InterpretedSignal, interpreted_signal_id)
            signal = db.get(GtmSignal, interpreted_signal.source_signal_id)
            if interpreted_signal is None or signal is None:
                counts["failed"] += 1
                continue
            record_outcome(db, tenant_id, interpreted_signal, signal)
            counts["recorded"] += 1
        except Exception:  # noqa: BLE001 -- one signal's failure must never block the others
            counts["failed"] += 1

    return counts
