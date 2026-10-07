"""What the router LEARNS at runtime, as opposed to what it is told in code.

registry.py holds authored knowledge -- a provider's filter names and shapes, taken from its
documentation and reviewed in a diff. These two tables hold the part nobody can author: what
values a provider's taxonomy actually contains, and how a given partner's wording was resolved
against it.

Why this has to be stored rather than recomputed per run:

  - Icypeas' published industry list 404s (checked 2026-10-05). The only remaining source of truth
    for its taxonomy is the values that come back on real rows we already paid for. Throwing those
    away and re-deriving them with an LLM guess every run is how "Professional Services" became a
    filter that matched nothing.
  - A resolution is a decision a partner should be able to see and correct. "We read your
    'Professional Services' as these six categories" is only reviewable if it is a row.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

from app.db.models import Base


class ProviderTaxonomyValue(Base):
    """One real, confirmed value in one provider's value space for one atom.

    `source` records how we learned it, because that determines how much to trust it:
        published_list        -- the provider's own documented enumeration
        resolver              -- the provider's own resolver endpoint (authoritative, free)
        observed_in_response  -- seen on a real row the provider returned to us

    `observed_count` is evidence of how common a value is, used to rank candidates when an LLM
    maps a partner's broad concept onto several real values.
    """

    __tablename__ = "provider_taxonomy_values"
    __table_args__ = (UniqueConstraint("provider", "atom", "normalized_value",
                                       name="uq_provider_taxonomy_value"),)

    id = Column(Integer, primary_key=True)
    provider = Column(String, nullable=False, index=True)
    atom = Column(String, nullable=False, index=True)
    value = Column(String, nullable=False)
    normalized_value = Column(String, nullable=False)
    source = Column(String, nullable=False)
    observed_count = Column(Integer, nullable=False, default=0)
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)


class IcpTermResolution(Base):
    """How one partner's wording was resolved into one provider's real values, and by what method.

    Kept per (tenant, provider, atom, partner_term) so the same words can resolve differently for
    different providers -- which is the entire point, since each has its own value space.

    `validated_count` is what the provider's FREE count endpoint returned for the resolved value
    set. A resolution that has never been count-validated is a hypothesis, not a fact, and the
    executor treats it as such.
    """

    __tablename__ = "icp_term_resolutions"
    __table_args__ = (UniqueConstraint("tenant_id", "provider", "atom", "partner_term",
                                       name="uq_icp_term_resolution"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    provider = Column(String, nullable=False)
    atom = Column(String, nullable=False)
    partner_term = Column(String, nullable=False)
    resolved_values = Column(Text, nullable=True)      # JSON array, kept as text for portability
    target_filter = Column(String, nullable=True)      # which provider filter received them
    method = Column(String, nullable=False)            # see resolution.py's METHOD_* constants
    validated_count = Column(Integer, nullable=True)
    resolved_at = Column(DateTime, default=datetime.utcnow)
    approved_by_human = Column(String, nullable=True)
