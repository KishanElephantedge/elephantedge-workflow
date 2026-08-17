"""DemandHypothesis -- Step 7's evidence ledger for "is this company/function also showing
outward-looking, solution-space orientation for an already-established problem," built from
InterpretedSignal rows, per demand-hypothesis-design.md.

Mirrors ProblemHypothesis's shape exactly (two tables, same reasoning): DemandHypothesis is the
living claim, DemandHypothesisEvidence is the join to the InterpretedSignal rows that support or
contradict it -- kept as its OWN, separate ledger from ProblemHypothesisEvidence, even though a
single InterpretedSignal can legitimately be linked into both (see demand_detection.py's own
docstring for why that's intentional, not a data-modeling mistake).

A DemandHypothesis can never exist without an underlying ProblemHypothesis -- problem_hypothesis_id
is required, never nullable. Never reads app/gtm_os/context/business_context.py, never determines
buying intent, ICP fit, offering fit, or opportunity. Those are later, not-yet-built layers."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text

from app.db.models import Base


class DemandHypothesis(Base):
    __tablename__ = "demand_hypotheses"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    company_name_raw = Column(String, nullable=True)
    problem_hypothesis_id = Column(Integer, ForeignKey("problem_hypotheses.id"), nullable=False)
    affected_function = Column(String, nullable=False)  # inherited from the linked ProblemHypothesis, not re-derived

    demand_statement = Column(Text, nullable=False)  # plain language -- never a buying-intent/opportunity claim
    reasoning_note = Column(Text, nullable=True)

    # Structured, multi-axis confidence -- NOT a single numeric score, same discipline as
    # ProblemHypothesis.confidence. Keys: best_evidence_tier ("demand"|"buying_intent_adjacent"),
    # evidence_count, independent_evidence_count, distinct_evidence_types, distinct_people_count,
    # has_contradiction, supporting_count, contradicting_count.
    confidence = Column(JSON, nullable=True)

    first_observed_at = Column(DateTime, nullable=True)  # earliest contributing evidence's observed_at
    last_updated_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)


class DemandHypothesisEvidence(Base):
    """One link between a DemandHypothesis and the InterpretedSignal that supports or contradicts
    it. Never deleted or overwritten. Independence is enforced at link time the same way as
    ProblemHypothesisEvidence: the same underlying GtmSignal (via dedup_key) is never linked twice
    to the same hypothesis, regardless of which source produced it."""
    __tablename__ = "demand_hypothesis_evidence"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    demand_hypothesis_id = Column(Integer, ForeignKey("demand_hypotheses.id"), nullable=False)
    interpreted_signal_id = Column(Integer, ForeignKey("interpreted_signals.id"), nullable=False)

    role = Column(String, nullable=False)  # "supporting" | "contradicting"
    evidence_tier = Column(String, nullable=False)  # "demand" | "buying_intent_adjacent" -- the Demand-layer tier, distinct from ProblemHypothesisEvidence.evidence_tier
    note = Column(Text, nullable=True)

    added_at = Column(DateTime, default=datetime.utcnow)
