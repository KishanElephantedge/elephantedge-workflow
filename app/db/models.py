"""
Mirrors the shared database schema owned by synefi/app/db/models.py. This is intentional
duplication, not a copy-paste mistake: the shared-database/siloed-compute pattern means two
separate backend codebases both read/write the SAME physical Postgres tables, so both must
agree on the schema -- there is no shared Python package between them by design (that would
re-couple the two backends' deploys). If the shared schema changes, both this file and
synefi/app/db/models.py need to be updated together; the database itself is the contract.

This backend only touches tables scoped to its own tenant_id (Elephant Edge) -- see each
route in app/routes/api.py for the tenant_id filter that enforces that boundary.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    slug = Column(String, nullable=False, unique=True)
    backend_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Batch(Base):
    __tablename__ = "batches"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    current_phase = Column(String, default="signal_discovery")
    status = Column(String, default="in_progress")

    companies = relationship("Company", back_populates="batch", cascade="all, delete-orphan")


class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    name = Column(String, nullable=False)
    domain = Column(String, nullable=True)
    industry = Column(String, nullable=True)
    employee_count = Column(Integer, nullable=True)
    location = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Set the moment a Decision Maker search runs for this company, regardless of outcome.
    # Distinguishes "searched, found nothing" from "never searched" -- without this, a company
    # with no findable contact gets re-billed on every re-run (including a future autonomous
    # daily cycle) forever, since it never acquires a Contact row to skip on.
    decision_maker_searched_at = Column(DateTime, nullable=True)

    # Phase 3 (Discovery) -- captured free, same Crustdata query, per phase5's Finding 1 addendum.
    estimated_revenue_lower_usd = Column(Integer, nullable=True)
    estimated_revenue_higher_usd = Column(Integer, nullable=True)
    last_funding_round_type = Column(String, nullable=True)
    last_funding_date = Column(DateTime, nullable=True)
    crunchbase_total_investment_usd = Column(Float, nullable=True)
    headcount_growth_12m_percent = Column(Float, nullable=True)
    source = Column(String, nullable=True)

    # Phase 5/8 -- Buying Signal Intelligence. Stage 1 (discovery-time) fields; Stage 2 lives on
    # Contact.thread_role (Phase 4's finding), combined at scoring time, not stored redundantly here.
    active_head_of_sales_posting = Column(Boolean, nullable=True)
    buying_signal_checked_at = Column(DateTime, nullable=True)

    # Signal Framework v2 (from Gokul) -- org-composition data, captured free from the same
    # Crustdata Discovery response (role_distribution_percent), no separate paid call needed.
    sales_headcount_percent = Column(Float, nullable=True)
    marketing_headcount_percent = Column(Float, nullable=True)
    geography_tier = Column(String, nullable=True)  # "tier_1" | "tier_2"
    industry_classification = Column(String, nullable=True)  # "tech" | "non_tech"
    active_job_title = Column(String, nullable=True)  # title of the matched posting, if any
    hiring_signal_role = Column(String, nullable=True)  # head_of_sales | sdr | ae | marketing | gtm
    hiring_signal_hire_type = Column(String, nullable=True)  # first_hire | multiple_hire
    hiring_signal_strength = Column(String, nullable=True)  # strong | medium | weak
    hiring_signal_reasoning = Column(Text, nullable=True)
    detected_tech_stack = Column(JSON, nullable=True)
    has_outbound_tooling = Column(Boolean, nullable=True)
    has_ai_sdr_tool = Column(Boolean, nullable=True)
    tech_stack_checked_at = Column(DateTime, nullable=True)

    batch = relationship("Batch", back_populates="companies")
    signals = relationship("Signal", back_populates="company", cascade="all, delete-orphan")
    score = relationship("Score", back_populates="company", uselist=False, cascade="all, delete-orphan")
    contacts = relationship("Contact", back_populates="company", cascade="all, delete-orphan")


class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    category = Column(String, nullable=False)
    signal_type = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    fired_at = Column(DateTime, default=datetime.utcnow)
    source = Column(String, nullable=True)

    company = relationship("Company", back_populates="signals")


class Score(Base):
    __tablename__ = "scores"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, unique=True)
    signal_strength = Column(Float, default=0)
    icp_fit = Column(Float, default=0)
    financial_growth = Column(Float, default=0)
    compliance_complexity = Column(Float, default=0)
    greenfield_legacy = Column(Float, default=0)
    stacking_bonus = Column(Float, default=0)
    # Elephant Edge only (Phase 9, Component C) -- Synefi has no equivalent, since its
    # decision-maker search isn't two-tiered the way Elephant Edge's is.
    decision_maker_match = Column(Float, default=0)
    total_score = Column(Float, default=0)
    tier = Column(String, default="excluded")
    passed_industry_gate = Column(Boolean, default=False)
    computed_at = Column(DateTime, default=datetime.utcnow)
    breakdown = Column(JSON, nullable=True)

    company = relationship("Company", back_populates="score")


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    title = Column(String, nullable=True)
    linkedin_url = Column(String, nullable=True)
    thread_role = Column(String, nullable=True)
    matched_title_reasoning = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Set once this contact + its company have been pushed to HubSpot -- prevents creating
    # duplicate Company/Contact records in HubSpot on a re-run.
    hubspot_synced_at = Column(DateTime, nullable=True)
    # Phase 11 (Personalization) -- dynamically-chosen opening line, pushed to HeyReach as a
    # customUserFields value merged into the sequence template's {{personalization_hook}} tag.
    personalization_hook = Column(Text, nullable=True)

    company = relationship("Company", back_populates="contacts")
    campaign_pushes = relationship("CampaignPush", back_populates="contact", cascade="all, delete-orphan")


class CampaignPush(Base):
    __tablename__ = "campaign_pushes"

    id = Column(Integer, primary_key=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False)
    heyreach_campaign_id = Column(String, nullable=True)
    status = Column(String, default="pending")
    error_message = Column(Text, nullable=True)
    pushed_at = Column(DateTime, nullable=True)

    contact = relationship("Contact", back_populates="campaign_pushes")


class AutonomousRun(Base):
    __tablename__ = "autonomous_runs"

    id = Column(Integer, primary_key=True)
    run_date = Column(DateTime, default=datetime.utcnow)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=True)
    status = Column(String, default="running")
    companies_discovered = Column(Integer, default=0)
    companies_selected = Column(Integer, default=0)
    contacts_found = Column(Integer, default=0)
    contacts_pushed = Column(Integer, default=0)
    credits_spent_usd = Column(Float, nullable=True)
    budget_stopped_early = Column(Boolean, default=False)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    # Elephant Edge only -- pre-outreach approval window (Signal Framework v2 follow-up
    # request): the cycle pauses here after Decision Maker, sends a review email, and waits
    # this long before actually pushing to the outreach channel, unless cancelled.
    awaiting_approval_until = Column(DateTime, nullable=True)
    cancelled = Column(Boolean, default=False)

    batch = relationship("Batch")


class Credential(Base):
    __tablename__ = "credentials"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String, nullable=False)
    value = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Parameter(Base):
    __tablename__ = "parameters"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    key = Column(String, nullable=False)
    value = Column(JSON, nullable=False)
    description = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
