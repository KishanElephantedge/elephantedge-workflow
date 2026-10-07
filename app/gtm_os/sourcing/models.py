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

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint

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


class IcpExclusion(Base):
    """A value this partner's runs keep rejecting, pushed back into the provider query.

    Per tenant AND per provider: "Public Safety" may be a real Icypeas industry label and spelled
    differently (or absent) at another provider, so an exclusion learned against one cannot be
    assumed valid at another.

    `rejection_count` is the evidence threshold -- one bad company is not a verdict on its whole
    industry. `suppressed_at` is the human override for an inference we got wrong, which keeps the
    evidence while stopping it being acted on.
    """

    __tablename__ = "icp_exclusions"
    __table_args__ = (UniqueConstraint("tenant_id", "provider", "atom", "normalized_value",
                                       name="uq_icp_exclusion"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    provider = Column(String, nullable=False)
    atom = Column(String, nullable=False)
    value = Column(String, nullable=False)
    normalized_value = Column(String, nullable=False)
    rejection_count = Column(Integer, nullable=False, default=0)
    reason = Column(String, nullable=True)
    first_rejected_at = Column(DateTime, default=datetime.utcnow)
    last_rejected_at = Column(DateTime, default=datetime.utcnow)
    suppressed_at = Column(DateTime, nullable=True)


class RouteAttempt(Base):
    """One provider call and how it ended, in the closed vocabulary of outcomes.py.

    This is the memory the planner ranks by: a provider that just failed three times is skipped
    rather than retried, and a provider nobody has called is not assumed broken. It is also the
    audit trail for "why did this partner get these companies from that tool today".

    Health is OBSERVED here rather than configured, which is what separates an adaptive router
    from a fixed waterfall -- but it only ever reorders candidates. It never replaces the free
    pre-flight checks, because how a provider behaved an hour ago is not evidence about now.
    """

    __tablename__ = "route_attempts"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    endpoint = Column(String, nullable=True)
    outcome = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    rows = Column(Integer, nullable=True)
    cost_usd = Column(Float, nullable=True)
    attempted_at = Column(DateTime, default=datetime.utcnow, index=True)


class CompanyPool(Base):
    """One real company, shared across every tenant -- bought once, usable by anyone whose ICP
    matches it.

    WHY THIS EXISTS. We have ~10-15 partners today and growing, and professional-services/B2B-SaaS
    ICPs overlap heavily. A company bought for one partner is free for the next one whose ICP it
    also matches, but today we would re-buy it. The first step of any sourcing run should be a
    query against OUR OWN data, not a provider.

    `identity_key` is how a company already in the pool is recognized again:
    linkedin_company_id > domain (shortener-stripped, matching the same normalization already
    applied to per-tenant Company rows) > normalized name, in that preference order, because
    LinkedIn URL is the most stable identity Icypeas/HarvestAPI both key on.

    This table is intentionally SEPARATE from `companies`: `companies` rows are tenant-scoped
    (via batch_id -> Batch.tenant_id) and carry tenant-specific pipeline state (decision_maker_
    searched_at, icp_evaluation_attempts, outreach). The pool carries only the shared facts about
    the company itself, so one company can be the source of many different tenants' own `companies`
    rows without those tenants' pipeline state ever leaking into each other.
    """

    __tablename__ = "company_pool"
    __table_args__ = (UniqueConstraint("identity_key", name="uq_company_pool_identity"),)

    id = Column(Integer, primary_key=True)
    identity_key = Column(String, nullable=False)
    linkedin_company_id = Column(String, nullable=True, index=True)
    linkedin_url = Column(String, nullable=True)
    domain = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False)
    industry_raw = Column(String, nullable=True)
    headcount = Column(Integer, nullable=True)
    revenue_low_usd = Column(Integer, nullable=True)
    revenue_high_usd = Column(Integer, nullable=True)
    location = Column(String, nullable=True)
    country = Column(String, nullable=True)
    source_provider = Column(String, nullable=True)
    source_endpoint = Column(String, nullable=True)
    fetched_at = Column(DateTime, default=datetime.utcnow)
    cost_usd = Column(Float, nullable=True)
    last_verified_at = Column(DateTime, default=datetime.utcnow)


class PoolDelivery(Base):
    """Which pool company has already been given to which tenant.

    This is what keeps the pool shareable WITHOUT breaking tenant isolation: the company fact
    lives once in company_pool, but each tenant's own GtmLead/Company/Opportunity rows (their real
    pipeline, never shared) are created fresh from it on first delivery, and this row is what stops
    the same company being "discovered" for that tenant a second time by a later run.
    """

    __tablename__ = "pool_deliveries"
    __table_args__ = (UniqueConstraint("tenant_id", "company_pool_id", "play",
                                       name="uq_pool_delivery"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    company_pool_id = Column(Integer, ForeignKey("company_pool.id"), nullable=False, index=True)
    play = Column(String, nullable=False)
    delivered_at = Column(DateTime, default=datetime.utcnow)
