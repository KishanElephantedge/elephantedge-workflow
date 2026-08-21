"""ProblemHypothesis -- Step 5's evidence ledger for "does this company/function appear to have
an internal difficulty, gap, or change-pressure," built from InterpretedSignal rows.

Two tables, not one:
- ProblemHypothesis: the living claim itself (company + affected_function scoped), holding only
  descriptive/structured fields -- no opaque numeric score.
- ProblemHypothesisEvidence: the join between a hypothesis and the InterpretedSignal rows that
  support or contradict it, kept as individual rows (never merged), so every hypothesis is fully
  explainable back to its raw evidence.

Deliberately NOT a "business change" object -- per the Step 5 approval, no new object is created
for that concept; hiring-type evidence simply links into this ledger as contextual/corroborating
evidence per the tightened entry rule (see app/gtm_os/intelligence/problem_detection.py).

Never reads app/gtm_os/context/business_context.py, never determines demand/opportunity/ICP/
offering fit. Those are later, not-yet-built layers that will read FROM this table."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text

from app.db.models import Base


class ProblemHypothesis(Base):
    """One living claim, scoped to (company, affected_function). Accumulates evidence over time
    via ProblemHypothesisEvidence rather than being rewritten -- first_observed_at/created_at
    never change once set; last_updated_at moves forward each time evidence is linked."""
    __tablename__ = "problem_hypotheses"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    company_name_raw = Column(String, nullable=True)
    # Fallback identity for the unresolved-company case (company_id AND company_name_raw both
    # None -- e.g. a LinkedIn post whose author's employer couldn't be parsed from their
    # occupation string). Mirrors InterpretedSignal.person_name_raw verbatim, passed through
    # unchanged, never re-derived here -- see _get_open_hypothesis()'s own docstring for why this
    # exists: without SOME real identity to group on, every piece of unresolved-company evidence
    # would open a brand-new hypothesis forever, even from the same real person.
    person_name_raw = Column(String, nullable=True)
    affected_function = Column(String, nullable=False)  # reused from InterpretedSignal's own vocabulary

    problem_statement = Column(Text, nullable=False)  # plain language, source-grounded -- never a demand/opportunity claim
    reasoning_note = Column(Text, nullable=True)

    # Structured, multi-axis confidence -- deliberately NOT a single numeric score (Step 5 design
    # doc §10). Recomputed from linked evidence each time evidence is added; see
    # problem_detection.py::_recompute_confidence. Keys: best_evidence_tier
    # ("declared"|"implied_gap"|"contextual"), evidence_count, independent_evidence_count,
    # distinct_evidence_types (list), distinct_people_count, has_contradiction.
    confidence = Column(JSON, nullable=True)

    first_observed_at = Column(DateTime, nullable=True)  # earliest contributing evidence's observed_at
    last_updated_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProblemHypothesisEvidence(Base):
    """One link between a ProblemHypothesis and the InterpretedSignal that supports or
    contradicts it. Never deleted or overwritten -- a hypothesis's evidence list only ever grows.
    Independence (Step 5 design doc §6) is enforced at link time: the same underlying GtmSignal
    (via dedup_key) is never linked twice to the same hypothesis."""
    __tablename__ = "problem_hypothesis_evidence"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    problem_hypothesis_id = Column(Integer, ForeignKey("problem_hypotheses.id"), nullable=False)
    interpreted_signal_id = Column(Integer, ForeignKey("interpreted_signals.id"), nullable=False)

    role = Column(String, nullable=False)  # "supporting" | "contradicting" -- always kept separate, never netted out
    evidence_tier = Column(String, nullable=False)  # "declared" | "implied_gap" | "contextual" -- the tier this evidence was classified as at link time
    note = Column(Text, nullable=True)  # short explanation of why this was linked as supporting/contradicting

    added_at = Column(DateTime, default=datetime.utcnow)
