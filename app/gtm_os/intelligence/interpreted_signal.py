"""InterpretedSignal -- Step 4's observation-level restatement of a single GtmSignal.

Strictly one GtmSignal -> one InterpretedSignal (no many-to-many yet, see the Step 4 design
doc's Open Decisions -- that's deferred, not decided here). This layer only ever says what
appears to have happened/changed and which business function it touches -- it never determines
demand, opportunity, ICP fit, offering fit, urgency, or buying intent. Those are later,
not-yet-built layers that will read FROM this table; nothing here writes toward them."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.db.models import Base


class InterpretedSignal(Base):
    __tablename__ = "interpreted_signals"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    source_signal_id = Column(Integer, ForeignKey("gtm_signals.id"), nullable=False)

    event_type = Column(String, nullable=False)  # what kind of thing was observed, e.g. "hiring_activity"
    affected_function = Column(String, nullable=True)  # "sales" | "marketing" | "unknown"
    business_change = Column(Text, nullable=False)  # plain-language, source-grounded restatement -- never a demand/opportunity claim

    evidence_excerpt = Column(Text, nullable=True)  # the specific text/field this was grounded in
    extraction_method = Column(String, nullable=False)  # "deterministic:<rule_name>" -- no LLM in this version
    extraction_confidence = Column(String, nullable=True)  # confidence in the EXTRACTION itself (did we read this right) -- NOT a demand/opportunity confidence

    # Entity fields passed through unchanged from the raw signal -- not resolved, merged, or judged here.
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    company_name_raw = Column(String, nullable=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)
    person_name_raw = Column(String, nullable=True)

    observed_at = Column(DateTime, nullable=True)  # inherited unchanged from the raw signal -- freshness/decay is a later layer's concern
    interpreted_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
