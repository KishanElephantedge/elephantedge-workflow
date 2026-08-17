"""Content Topic data model -- Step 16E-1.

Persistence only. No extraction, matching, LLM classification, or promotion logic lives here --
see topic-extraction-normalization-design.md for the full architecture this implements one layer
of. Three tables:

- ContentTopic: the living topic itself (configured or discovered), tenant-scoped, no
  evidence_count -- always derived from ContentTopicEvidence, never stored redundantly (same
  "read, don't recompute" discipline as ProblemHypothesis.confidence).
- ContentTopicEvidence: the join between a ContentTopic and the GtmSignal rows that established
  it -- mirrors ProblemHypothesisEvidence's shape exactly (one row per signal-topic link, never
  merged/collapsed), except linking to GtmSignal directly rather than an interpreted layer, since
  Content Intelligence's topic layer sits directly on top of sensing (Step 16C/16D), not on top
  of InterpretedSignal.
- TopicCandidate: one row per single LLM observation of a possible discovered topic, deliberately
  NOT accumulating internally (no first_seen_at/last_seen_at/evidence_count on the row) -- grouping
  and promotion-condition evaluation happens later, at read time, by querying multiple rows
  sharing the same normalized_name (per the approved Step 16E design review). This mirrors
  ContentTopicEvidence's own "never merge individual observations" philosophy one layer earlier.

Deliberately does NOT add content_topic_id to GtmSignal -- a signal can belong to multiple
topics, a topic can have many signals, and ContentTopicEvidence is the only relationship between
them (many-to-many join table, not a foreign key on the raw signal)."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text

from app.db.models import Base


class ContentTopic(Base):
    """One topic, tenant-scoped. origin="configured" means it came directly from
    content_intelligence_topics (see app/gtm_os/content/topics.py); origin="discovered" means it
    was promoted from accumulated TopicCandidate rows (promotion mechanism not yet built).
    first_seen_at/last_seen_at describe the underlying evidence's observed_at range, not this
    row's own timestamps -- maintained by whatever writes ContentTopicEvidence, not computed
    here."""
    __tablename__ = "content_topics"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    canonical_name = Column(String, nullable=False)
    aliases = Column(JSON, nullable=True)  # list[str], same shape as topics.py's configured aliases

    origin = Column(String, nullable=False)  # "configured" | "discovered"

    first_seen_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)


class ContentTopicEvidence(Base):
    """One link between a ContentTopic and the GtmSignal that connected it. The only relationship
    between ContentTopic and GtmSignal -- never a foreign key on GtmSignal itself, since one
    signal can support multiple topics. Uniqueness on (content_topic_id, gtm_signal_id) enforces
    "one signal can only be linked once to the same topic" without capping how many signals a
    topic can have or how many topics a signal can belong to."""
    __tablename__ = "content_topic_evidence"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    content_topic_id = Column(Integer, ForeignKey("content_topics.id"), nullable=False)
    gtm_signal_id = Column(Integer, ForeignKey("gtm_signals.id"), nullable=False)

    matched_term = Column(String, nullable=True)  # the specific alias/term text that matched, for explainability
    match_method = Column(String, nullable=False)  # "deterministic_alias_match" | "promoted_discovered_topic"

    added_at = Column(DateTime, default=datetime.utcnow)


class TopicCandidate(Base):
    """One single observation of a possible discovered topic, produced by one LLM classification
    call against one unmatched GtmSignal. Never accumulates or gets updated in place -- repeated
    observations of what may be the same emerging topic are separate rows, grouped later (by
    normalized_name, per-tenant) when a promotion condition is evaluated (not built yet). This is
    what stands between a single LLM opinion and a permanent ContentTopic(origin="discovered")."""
    __tablename__ = "topic_candidates"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    candidate_name = Column(String, nullable=False)  # the LLM's proposed topic name, as-is
    normalized_name = Column(String, nullable=False)  # deterministic lowercase/strip grouping key -- NOT semantic/embedding-based

    gtm_signal_id = Column(Integer, ForeignKey("gtm_signals.id"), nullable=False)  # the one signal this observation came from

    evidence_excerpt = Column(Text, nullable=True)  # the specific text this candidate was grounded in
    extraction_method = Column(String, nullable=False)  # e.g. "llm:<model/prompt version>" -- mirrors InterpretedSignal.extraction_method
    confidence = Column(String, nullable=True)  # mirrors InterpretedSignal.extraction_confidence -- confidence in the LLM's proposal itself

    # Step 16E-5 -- candidate normalization/consolidation (see candidate_normalization.py and
    # topic-candidate-normalization-design.md). All three nullable, additive, never required at
    # creation time -- a candidate is fully valid and queryable with these unset (the "singleton,
    # not yet evaluated for clustering" state). Never a new table: a cluster is represented purely
    # as "rows sharing the same cluster_key" per the approved design's own "smallest architecture
    # that preserves correctness" instruction.
    cluster_key = Column(String, nullable=True)  # shared by every row this layer has grouped as the same concept; NULL = not yet clustered
    normalization_method = Column(String, nullable=True)  # "deterministic" | "llm_comparison"
    normalization_reason = Column(Text, nullable=True)  # explains THIS row's current cluster_key -- deterministic rule name, or the LLM's own reason string

    created_at = Column(DateTime, default=datetime.utcnow)
