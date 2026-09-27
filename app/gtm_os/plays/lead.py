"""GtmLead -- one person, found by one play, moving forward through fixed states exactly once.

Why this exists: V2 used to re-derive "where is this lead?" on every run from five tables
(Contact, MessageDraft, MessageSendAttempt, CampaignPush, Opportunity.status), and its batch sweeps
re-scanned the whole backlog to find work. That is how weeks-old companies got re-bought on
2026-09-24. A lead here has one row, one state, and a unique key per (tenant, play, person), so the
same person can never be picked up -- or paid for -- twice.

States (forward only):
    signal         -- found by the play, not yet judged
    rejected       -- the Qualifier said no (terminal; costs one cheap LLM call, nothing paid)
    qualified      -- the Qualifier said yes; allowed to spend on a contact lookup
    contact_found  -- verified email + company resolved; Opportunity/Strategy rows written
    contact_missing-- paid lookup found no verified email (terminal)
    drafted        -- a message draft exists and waits for approval in the Messages screen
    failed         -- a non-budget error stopped this lead; see last_error
Budget refusals never change state: the lead waits for tomorrow's budget instead.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text

from app.db.models import Base

STATE_SIGNAL = "signal"
STATE_REJECTED = "rejected"
STATE_QUALIFIED = "qualified"
STATE_CONTACT_FOUND = "contact_found"
STATE_CONTACT_MISSING = "contact_missing"
STATE_DRAFTED = "drafted"
STATE_FAILED = "failed"


class GtmLead(Base):
    __tablename__ = "gtm_leads"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    play = Column(String, nullable=False)                   # e.g. "post_engagement"
    person_linkedin_url = Column(String, nullable=False)    # normalized; unique per tenant+play
    person_name = Column(String, nullable=True)
    signal_id = Column(Integer, ForeignKey("gtm_signals.id"), nullable=True)
    state = Column(String, nullable=False, default=STATE_SIGNAL)

    evidence = Column(Text, nullable=True)                  # the post/comment text that surfaced them
    icp_fit_score = Column(Integer, nullable=True)          # Qualifier's 0-100
    intent = Column(String, nullable=True)                  # Qualifier's intent label
    qualifier_reason = Column(Text, nullable=True)
    qualifier_output = Column(JSON, nullable=True)          # full structured verdict, for audit

    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=True)
    message_draft_id = Column(Integer, ForeignKey("message_drafts.id"), nullable=True)

    spend_usd = Column(Float, nullable=False, default=0.0)  # reserved spend attributed to this lead
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def normalize_linkedin_url(url: str | None) -> str | None:
    """https://www.linkedin.com/in/Jane-Doe/?utm=x -> linkedin.com/in/jane-doe"""
    if not url:
        return None
    u = url.strip().lower().split("?")[0].split("#")[0].rstrip("/")
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    if u.startswith("www."):
        u = u[4:]
    return u or None
