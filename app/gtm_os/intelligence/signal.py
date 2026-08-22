"""GtmSignal -- a raw, observation-only record of something sensed from an external source
(a LinkedIn job posting, a LinkedIn post, a web search result, a company website page, a
TheirStack job record).

Deliberately NOT a demand/opportunity classification and NOT a fit/offering-match score --
this table only records what was observed and where it came from. Classifying whether a signal
means real demand, how strong it is, or which offering it maps to is later intelligence-layer
work (app/gtm_os/intelligence/ classification, not yet built) that reads FROM this table; it
does not write into it. Keeping that split is what lets raw signals be re-classified later
without re-sensing them.

Adapters live in app/gtm_os/intelligence/sensing.py and call existing CLIENT-level functions
directly (not app/phases/* orchestration), so this has zero coupling to the production pipeline.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text

from app.db.models import Base


class GtmSignal(Base):
    """One raw observation from a sensing source. Never deleted or overwritten on a repeat
    observation -- repeats are identified via dedup_key and left for the caller to interpret,
    not silently collapsed (see app/gtm_os/intelligence/sensing.py)."""
    __tablename__ = "gtm_signals"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    # Where this observation came from.
    source = Column(String, nullable=False)  # "linkedin_job" | "linkedin_post" | "web_search" | "company_website" | "theirstack_job"
    source_ref = Column(String, nullable=False)  # source's own identifier for this item (posting id, post urn, query, domain)
    signal_type = Column(String, nullable=False)  # raw kind of thing observed within that source, e.g. "job_posting", "post", "search_result", "page_content" -- NOT a demand/opportunity category

    # Timing.
    observed_at = Column(DateTime, nullable=True)  # when the underlying event happened (post date, job-posted date) -- null if the source doesn't expose one
    captured_at = Column(DateTime, default=datetime.utcnow)  # when this system pulled it

    # Loose entity linkage -- nullable because raw signals often arrive before company/contact resolution.
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    company_name_raw = Column(String, nullable=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)
    person_name_raw = Column(String, nullable=True)

    raw_evidence = Column(JSON, nullable=True)  # untouched source payload, for audit
    extracted_info = Column(JSON, nullable=True)  # minimal source-specific structured pull (title, snippet, matched keywords, etc.)

    dedup_key = Column(String, nullable=False)  # hash of (source, source_ref) -- used to flag repeats, never to block insert

    created_at = Column(DateTime, default=datetime.utcnow)

    # Person -> Company resolution bookkeeping (app/gtm_os/intelligence/company_resolution.py).
    # All four NULL means "never attempted" -- distinguishable from every real outcome. Populated
    # only for signals whose evidence tier already qualifies to open a hypothesis (contextual-tier
    # evidence, the majority of real signals today, is never resolved -- see that module's own
    # docstring). Categorical status only, never a numeric confidence score.
    company_resolution_status = Column(String, nullable=True)  # "resolved" | "unresolved" | "ambiguous"
    company_resolution_method = Column(String, nullable=True)  # "explicit" | "exact_name_match" | "enrichment_domain_match"
    company_resolution_reason = Column(Text, nullable=True)  # human-readable explanation, esp. for unresolved/ambiguous
    company_resolved_at = Column(DateTime, nullable=True)
