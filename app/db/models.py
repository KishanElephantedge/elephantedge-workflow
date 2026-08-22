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

from sqlalchemy import JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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
    # Which discovery pipeline created this batch -- "deepline" (Crustdata+TheirStack, the
    # automated flow) or "jobo" (Jobo's job-search API, its own separate credit system).
    # Batches are never mixed-source: every company in a batch comes from the same pipeline,
    # so the dashboard can show them in fully independent tabs.
    source = Column(String, default="deepline", nullable=False)

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
    # Company's own LinkedIn page (not a person's) -- captured free from the same Crustdata
    # company-identify call already made for firmographics (professional_network_url field),
    # so manual verification doesn't require copying the name into LinkedIn's search by hand.
    linkedin_url = Column(String, nullable=True)

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
    product_fit_jd_categories = Column(JSON, nullable=True)  # list of PRODUCT_FIT_KEYWORD_CATEGORIES names matched in the JD
    detected_tech_stack = Column(JSON, nullable=True)
    has_outbound_tooling = Column(Boolean, nullable=True)
    has_ai_sdr_tool = Column(Boolean, nullable=True)
    tech_stack_checked_at = Column(DateTime, nullable=True)

    # Team-composition gate (added after Kishan's manual review of batch 32 -- SmartWinnr had
    # a "gap" by role-slot count alone but was actually a real 12-sales/10-marketing full team
    # whose own hiring manager already covers GTM personally). primary | secondary | excluded.
    team_fit_tier = Column(String, nullable=True)
    team_fit_reasoning = Column(Text, nullable=True)

    # Hot-lead tagging (added 2026-08-13) -- additive only, never a qualification gate; see
    # app/phases/hot_leads.py for the full signal logic and reasoning behind each field.
    hiring_signal_posting_count = Column(Integer, nullable=True)  # how many qualifying postings this company had open at once, not just the one kept
    tofu_keyword_found = Column(Boolean, nullable=True)  # JD contains "top of the funnel pipeline generation" (Signal Framework v2)
    hot_lead = Column(Boolean, nullable=True)
    hot_lead_reasoning = Column(Text, nullable=True)

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
    # Free, already present in search_contact's own response (professional_email preferred,
    # personal_email as fallback) -- confirmed live (2026-08-10) we were paying for this call
    # and silently discarding the email fields on every single one. Also populated via free
    # fallback layers (see email_source below and free_decision_maker.resolve_fallback_email)
    # when the primary paid lookup finds no email. `contacts` is a shared table Synefi owns the schema for -- see that repo's
    # db/models.py and session.py's ensure_indexes() ALTER TABLE pattern; this column is
    # declared here for parity, but the actual ALTER TABLE lives in this backend's own
    # ensure_indexes() since that's the deploy pipeline this change was made through.
    email = Column(String, nullable=True)
    # "deepline" (paid, verified-ish via provider) | "jobo_company" (free, company-level
    # generic address, not personal) | "pattern_guess" (free, unverified firstname@domain --
    # SMTP verification not possible in production, see app/email_verify.py's module
    # docstring for why) -- lets the UI/review step show confidence honestly instead of
    # presenting a guess as fact.
    email_source = Column(String, nullable=True)
    thread_role = Column(String, nullable=True)
    matched_title_reasoning = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Set once this contact + its company have been pushed to HubSpot -- prevents creating
    # duplicate Company/Contact records in HubSpot on a re-run.
    hubspot_synced_at = Column(DateTime, nullable=True)
    # Phase 11 (Personalization) -- dynamically-chosen opening line, pushed to HeyReach as a
    # customUserFields value merged into the sequence template's {{personalization_hook}} tag.
    personalization_hook = Column(Text, nullable=True)
    # Per-contact "don't push to the outreach campaign" flag -- separate from
    # PersonalizedMessage.status (which only governs whether a message gets attached, not
    # whether the contact gets pushed at all). Set via the dashboard during the approval
    # window; run_campaign_execution skips any contact with this set.
    excluded_from_push = Column(Boolean, default=False)

    personalized_message = relationship("PersonalizedMessage", back_populates="contact", uselist=False, cascade="all, delete-orphan")

    company = relationship("Company", back_populates="contacts")
    campaign_pushes = relationship("CampaignPush", back_populates="contact", cascade="all, delete-orphan")


class PersonalizedMessage(Base):
    """Phase 13 -- deep-research personalized outreach message, per the lead's 4-module spec:
    company research, contact/LinkedIn research, fit analysis, message synthesis. Every
    module's raw structured output is kept (not just the final message) so a human can review
    or edit any stage, per the spec's own requirement ("Return the message plus the structured
    context objects so a human can review or edit if needed")."""
    __tablename__ = "personalized_messages"

    id = Column(Integer, primary_key=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False, unique=True)
    company_research = Column(JSON, nullable=True)
    contact_research = Column(JSON, nullable=True)
    fit_analysis = Column(JSON, nullable=True)
    generated_message = Column(Text, nullable=True)
    # Email channel, added 2026-08-10 -- same research/tone as generated_message, just a
    # second rendering (subject + body) for contacts who have a real email captured (see
    # Contact.email). Shares this row's single `status` -- one review decision covers both
    # channels, not independent per-channel approval (not asked for; would be easy to split
    # out later if it ever is).
    email_subject = Column(String, nullable=True)
    email_body = Column(Text, nullable=True)
    status = Column(String, default="draft")  # draft | approved | rejected
    error_message = Column(Text, nullable=True)
    generated_at = Column(DateTime, nullable=True)

    contact = relationship("Contact", back_populates="personalized_message")


class CampaignPush(Base):
    __tablename__ = "campaign_pushes"

    id = Column(Integer, primary_key=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False)
    heyreach_campaign_id = Column(String, nullable=True)
    status = Column(String, default="pending")
    error_message = Column(Text, nullable=True)
    pushed_at = Column(DateTime, nullable=True)

    contact = relationship("Contact", back_populates="campaign_pushes")


class CampaignEvent(Base):
    """Outcome tracking, received via SalesRobot's own outbound webhook (confirmed live --
    SalesRobot pushes events to us, there is no pull/status API). contact_id is nullable
    because the very first real payload's shape is unknown until it arrives -- matched
    best-effort by LinkedIn URL against known contacts, but the full raw payload is always
    kept regardless of whether matching succeeds, so nothing is lost while the matching
    logic gets refined against real data."""
    __tablename__ = "campaign_events"

    id = Column(Integer, primary_key=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True, index=True)
    event_type = Column(String, nullable=True)  # e.g. "connection_accepted", "reply" -- raw value from SalesRobot, not yet normalized
    raw_payload = Column(JSON, nullable=False)
    received_at = Column(DateTime, default=datetime.utcnow)

    contact = relationship("Contact")


class CalendarBooking(Base):
    """A call booked through the lead's Google Calendar Appointment Schedule (clients book
    directly off the website). Pulled via Google Calendar's events.list, not a webhook --
    Calendar has no equivalent push mechanism for appointment-schedule bookings, so this is
    populated by a periodic sync (see google_calendar_client.py) rather than received live."""
    __tablename__ = "calendar_bookings"

    id = Column(Integer, primary_key=True)
    google_event_id = Column(String, nullable=False, unique=True)  # dedupe key across syncs
    booker_name = Column(String, nullable=True)
    booker_email = Column(String, nullable=True)
    start_time = Column(DateTime, nullable=True)
    end_time = Column(DateTime, nullable=True)
    status = Column(String, nullable=True)  # confirmed | cancelled, from Google's own event status
    raw_payload = Column(JSON, nullable=False)
    synced_at = Column(DateTime, default=datetime.utcnow)

    # Meeting outcome (added for V2 Revenue Pace) -- human-recorded, not derived. `outcome_status`
    # is the only new vocabulary this introduces (won | lost, null = pending/unset). See
    # app/gtm_os/revenue/revenue_pace.py for validation and the ICP-snapshot capture logic.
    outcome_status = Column(String, nullable=True)  # won | lost | null (pending)
    outcome_company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    # V2 Overrides & Evals attribution link -- optional, human-supplied at outcome-recording
    # time (never inferred). Lets a won/lost meeting be traced back to the Opportunity/
    # GtmStrategy/MessageDraft that produced it. See app/gtm_os/learning/overrides_evals.py.
    outcome_opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=True)
    outcome_offering_name = Column(String, nullable=True)
    outcome_amount_usd = Column(Float, nullable=True)  # only meaningful when outcome_status == "won"
    outcome_reason = Column(Text, nullable=True)  # only meaningful when outcome_status == "lost"
    outcome_notes = Column(Text, nullable=True)  # free-form summary, usable regardless of status
    outcome_icp_snapshot = Column(JSON, nullable=True)  # system-captured at record time, never user-entered
    outcome_recorded_at = Column(DateTime, nullable=True)
    outcome_recorded_by = Column(String, nullable=True)  # the logged-in user's email, frontend-supplied

    outcome_company = relationship("Company")


class Notification(Base):
    """In-app notification feed -- separate from (not a replacement for) the existing Slack
    alerts, which some teammates without dashboard access still rely on. Created at the same
    real event points Slack already fires on (see app/notifications.py), plus new meeting-
    booked events Slack never covered. Rows older than NOTIFICATION_RETENTION_DAYS are swept
    on every list read (see app/notifications.py) -- no separate scheduled job needed."""
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    type = Column(String, nullable=False)  # run_failed | decision_makers_found | run_completed_empty | outreach_pushed | meeting_booked
    severity = Column(String, default="info")  # info | success | warning | error
    title = Column(String, nullable=False)
    message = Column(Text, nullable=True)
    batch_id = Column(Integer, nullable=True)
    run_id = Column(Integer, nullable=True)
    read_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


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
    # V2 Efficiency -- real count from _dedupe_against_prior_days() (autonomous_orchestrator.py),
    # previously computed and returned in this function's response dict but never persisted. See
    # app/gtm_os/efficiency/efficiency.py.
    duplicates_removed = Column(Integer, nullable=True)

    batch = relationship("Batch")


class Credential(Base):
    __tablename__ = "credentials"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String, nullable=False)
    value = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatConversation(Base):
    """AI chat widget thread. One resumable, tenant-scoped conversation history (this backend
    has no per-user identity concept at all -- auth lives entirely in the gateway -- so this
    matches every other table here: scoped by tenant, not by individual user)."""
    __tablename__ = "chat_conversations"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    title = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatMessage(Base):
    """Only user/assistant TEXT turns are persisted -- not the raw Anthropic tool_use/
    tool_result blocks. Tool-calling only needs to see prior turns as plain conversation
    context (same as any chat product), not literally replay past tool calls; the multi-step
    tool loop itself is entirely in-memory within a single turn (see app/routes/api.py's
    _run_chat_turn). tools_used is kept for UI transparency (a small "used: X, Y" chip),
    not for replaying-into-Claude."""
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("chat_conversations.id"), nullable=False)
    role = Column(String, nullable=False)  # user | assistant
    content = Column(Text, nullable=False)
    tools_used = Column(JSON, nullable=True)  # list of tool names called while producing this message
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("ChatConversation")


class DailyReview(Base):
    """Async, cross-timezone review layer -- lets two people in different places look at the
    same day's automated activity (companies/decision-makers/messages found that day) and
    leave a verdict + comment for each other, without needing to be online at the same time.
    Deliberately NOT wired to any real pipeline action (message approve/reject, push) -- this
    is a human tracking/communication layer on top of what already happened, not a control
    surface. One row per calendar date; review_date is a "YYYY-MM-DD" string (matches the
    date-string convention already used elsewhere in this codebase, e.g. get_overview_stats'
    start_date/end_date), not a real Date column, to avoid timezone-conversion edge cases at
    the JSON API boundary."""
    __tablename__ = "daily_reviews"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    review_date = Column(String, nullable=False)  # "YYYY-MM-DD"
    status = Column(String, default="pending")  # pending | approved | rejected
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ReviewComment(Base):
    """Free-text comments on a given day's review -- no author column by design (this backend
    has no user-identity concept at all); the comment textarea's placeholder prompts
    "@name ..." so whoever's commenting types their own name inline, same convention as a
    Slack thread with no real accounts."""
    __tablename__ = "review_comments"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    review_date = Column(String, nullable=False, index=True)  # "YYYY-MM-DD"
    comment = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Parameter(Base):
    __tablename__ = "parameters"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    key = Column(String, nullable=False)
    value = Column(JSON, nullable=False)
    description = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LinkedinMonitorProfile(Base):
    """A LinkedIn profile being watched for new posts, matched against a keyword taxonomy
    (see app/phases/linkedin_monitor.py) to surface GTM signals -- competitor/partner
    activity relevant to Elephant Edge's own positioning, not general prospecting."""
    __tablename__ = "linkedin_monitor_profiles"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    name = Column(String, nullable=True)
    linkedin_url = Column(String, nullable=False)
    company = Column(String, nullable=True)
    active = Column(Boolean, default=True)
    last_checked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # GTM partner categorization (see app/phases/gtm_partner_classification.py) -- which
    # industry this profile/company operates in, and who THEY sell to, so the lead can spot
    # non-competing companies serving the same buyer type as candidate referral partners. Always
    # grounded in real evidence (their own captured posts + the company name on file); never
    # fabricated when that evidence is too thin -- see classification_status below.
    industry = Column(String, nullable=True)
    sells_to = Column(String, nullable=True)
    classification_status = Column(String, nullable=True)  # "classified" | "insufficient_evidence"
    classification_confidence = Column(String, nullable=True)  # "high" | "medium" | "low"
    classification_reasoning = Column(Text, nullable=True)
    classification_evidence_excerpt = Column(Text, nullable=True)
    classified_at = Column(DateTime, nullable=True)

    # Captured from bulk imports (e.g. the GTM Partners community roster) but discarded until
    # now -- title/team_size/location/company_website give the partner-matching engine (see
    # app/phases/gtm_partner_matching.py) more signal than industry/sells_to alone.
    # team_size is a band string ("0-1", "2-10", "50-200"), not a literal headcount, matching
    # how the source data is actually reported -- do not coerce it into employee_count.
    title = Column(String, nullable=True)
    employee_count = Column(Integer, nullable=True)
    team_size = Column(String, nullable=True)
    location = Column(String, nullable=True)
    company_website = Column(String, nullable=True)

    # Full raw record from GTM University's public partners.json (icp, worksWith, keywords,
    # partnerType, detailHtml, etc.) -- kept as one JSON blob rather than a column per field
    # since the upstream schema isn't ours to define and may add/drop fields over time. This is
    # the authoritative evidence source once synced -- more reliable than the LLM-inferred
    # industry/sells_to below, which get overwritten from this data when available (see the
    # one-off sync in this session; app/phases/gtm_partner_classification.py's evidence-gathering
    # should prefer this over Google AI Overview/posts once that refactor lands).
    gtm_university_data = Column(JSON, nullable=True)
    gtm_university_synced_at = Column(DateTime, nullable=True)

    # Where this profile came from -- "linkedin" (default, original monitor seed),
    # "gtm_university_pdf", "gtm_university", "slack", "manual", etc. One profile table,
    # multiple sources, so a given person doesn't need a new table each time a new roster is
    # folded in.
    source = Column(String, nullable=True, default="linkedin")

    # Slack member id (e.g. "U0123ABC456") in the GTM Partners community workspace, used to
    # actually deliver a recommendation message via DM (see app/slack_bot_client.py). Set only
    # via an exact email lookup (users.lookupByEmail) or manual human confirmation -- deliberately
    # NEVER fuzzy-matched by name, after a real prior incident this session's history flagged (a
    # wrong-person LinkedIn message that couldn't be un-sent). A blank value here just means
    # "can't auto-send yet," not an error -- the message stays deliverable manually either way.
    slack_user_id = Column(String, nullable=True)


class PartnerCompanyRecommendation(Base):
    """One row = one Elephant Edge company proposed as a lead for one GTM partner (see
    app/phases/gtm_partner_matching.py). Deliberately NOT folded into PersonalizedMessage --
    that model is 1:1 with a Contact (unique FK), whereas this is many-companies-per-partner and
    has its own approval gate (proposed/approved/rejected) before any message is ever drafted.

    unique(profile_id, company_id) -- a company is never recommended twice to the same partner,
    checked before insert (not just relied on at the DB level) so a re-run of the sweep just
    skips what's already there instead of erroring."""
    __tablename__ = "partner_company_recommendations"
    __table_args__ = (UniqueConstraint("profile_id", "company_id", name="uq_partner_company_reco"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    profile_id = Column(Integer, ForeignKey("linkedin_monitor_profiles.id"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    match_reasoning = Column(Text, nullable=True)
    match_confidence = Column(String, nullable=True)  # "high" | "medium" | "low"
    status = Column(String, default="proposed")  # proposed | approved | rejected
    created_at = Column(DateTime, default=datetime.utcnow)
    reviewed_at = Column(DateTime, nullable=True)

    profile = relationship("LinkedinMonitorProfile")
    company = relationship("Company")


class PartnerRecommendationMessage(Base):
    """The outreach message drafted for one partner once (some of) their recommended companies
    are approved -- covers potentially several PartnerCompanyRecommendation rows at once (a
    partner gets one message naming their 3-ish matched companies, not one message per company).
    Second approval gate, mirroring PersonalizedMessage's draft/approved/rejected pattern
    (app/phases/personalized_outreach.py) but for this partner-facing shape.

    send_channel is set only once actually sent -- Slack/LinkedIn/email dispatch isn't wired up
    yet (channel choice explicitly undecided as of this build), so "sent" here just means a human
    manually delivered it and recorded how, giving future automation a real field to key off."""
    __tablename__ = "partner_recommendation_messages"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    profile_id = Column(Integer, ForeignKey("linkedin_monitor_profiles.id"), nullable=False)
    recommendation_ids = Column(JSON, nullable=False)  # list[int] of the approved PartnerCompanyRecommendation.id it covers
    generated_message = Column(Text, nullable=True)
    status = Column(String, default="draft")  # draft | approved | rejected | sent
    generated_at = Column(DateTime, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    send_channel = Column(String, nullable=True)  # "slack" | "linkedin" | "email" -- set at send time

    profile = relationship("LinkedinMonitorProfile")


class WebsiteVisitor(Base):
    """One row per page-view beacon received from the marketing site's tracking snippet (see
    app/website_visitor_tracking.py) -- company-level identification only (via Deepline's
    deepline_ip_to_company_find_company_by_ip), never person-level. An IP maps to a network,
    not an individual, so this table intentionally has no name/email/contact fields for the
    visitor themselves -- see that module's docstring for the fuller reasoning (and why a
    person-level tool would be a different, separate integration, not an extension of this one).

    company_lookup_status distinguishes "we tried Deepline and it found nothing" from "we
    haven't successfully looked this IP up yet" (e.g. Deepline was down) -- never silently
    conflated, so a later reprocessing pass can tell which rows are worth retrying."""
    __tablename__ = "website_visitors"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    ip_address = Column(String, nullable=False)
    page_path = Column(String, nullable=True)
    referrer = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)

    company_name = Column(String, nullable=True)
    company_domain = Column(String, nullable=True)
    company_website = Column(String, nullable=True)
    company_industry = Column(String, nullable=True)
    company_employee_range = Column(String, nullable=True)
    company_city = Column(String, nullable=True)
    company_state = Column(String, nullable=True)
    company_country = Column(String, nullable=True)
    is_fuzzy_match = Column(Boolean, nullable=True)  # Deepline's own "fuzzy" flag -- probabilistic, not confirmed
    company_lookup_status = Column(String, nullable=True)  # "resolved" | "no_match" | "lookup_failed"

    created_at = Column(DateTime, default=datetime.utcnow)


class LinkedinMonitorSignal(Base):
    """A new post from a monitored profile that matched at least one keyword -- one row per
    (profile, post), never re-alerted once created (see the unique index on profile_id+post_urn
    in ensure_indexes)."""
    __tablename__ = "linkedin_monitor_signals"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    profile_id = Column(Integer, ForeignKey("linkedin_monitor_profiles.id"), nullable=False)
    post_urn = Column(String, nullable=False)
    post_url = Column(String, nullable=True)
    post_text = Column(Text, nullable=True)
    author_name = Column(String, nullable=True)
    posted_at = Column(DateTime, nullable=True)
    matched_keywords = Column(JSON, nullable=True)
    tier = Column(String, nullable=True)
    # LLM relevance screen (added 2026-08-13) -- a keyword match alone is noisy (e.g. the bare
    # word "partnership" matches unrelated partner-marketing content); this is a second pass
    # judging whether the post is ACTUALLY about AI SDR/sales-automation, not just containing
    # the matched word. Every keyword-matched post is still stored and visible either way
    # (never silently dropped) -- only the alert (Slack/notification) is gated on this.
    relevance_score = Column(Integer, nullable=True)
    recommended_action = Column(String, nullable=True)  # "engage" | "monitor" | "ignore"
    classifier_reason = Column(Text, nullable=True)
    alerted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    profile = relationship("LinkedinMonitorProfile")


class ReverseDiscoveryCandidate(Base):
    """Mode B from the hot-leads research (app/phases/reverse_discovery.py) -- a person found
    via a broad LinkedIn keyword search (not a known watch-list) publicly discussing/exploring
    AI SDR / autonomous sales themselves, with no job posting involved. Kept as its own table
    and its own dashboard tab, deliberately separate from linkedin_monitor_* (which watches
    KNOWN competitor/partner profiles) -- different mechanism, different purpose."""
    __tablename__ = "reverse_discovery_candidates"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    post_urn = Column(String, nullable=False)
    post_url = Column(String, nullable=True)
    post_text = Column(Text, nullable=True)
    matched_keyword = Column(String, nullable=True)
    author_name = Column(String, nullable=True)
    author_profile_url = Column(String, nullable=True)
    author_occupation = Column(String, nullable=True)
    guessed_company_name = Column(String, nullable=True)
    relevance_score = Column(Integer, nullable=True)
    recommended_action = Column(String, nullable=True)
    classifier_reason = Column(Text, nullable=True)
    icp_status = Column(String, nullable=True)  # "qualified" | "rejected" | "unknown"
    icp_reasoning = Column(Text, nullable=True)
    posted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class EfficiencyActivityEvent(Base):
    """V2 Efficiency -- one recorded batch of REAL automated activity volume, inserted once per
    (tenant, activity_type, activity_date, source, source_run_id) and never updated in place.
    See app/gtm_os/efficiency/activity_recorder.py for the idempotent insert helper and
    app/gtm_os/efficiency/efficiency.py for how this is aggregated into an hours-saved readout.

    Deliberately NOT a place for every activity: message drafting (MessageDraft) and GTM-signal
    capture (GtmSignal) are already real, insert-only event tables in their own right --
    efficiency.py reads those directly at aggregation time instead of duplicating them here,
    which would create a second, driftable source of truth for the same fact."""
    __tablename__ = "efficiency_activity_events"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    activity_type = Column(String, nullable=False)
    activity_date = Column(Date, nullable=False)  # calendar day this volume represents
    volume = Column(Integer, nullable=False)
    source = Column(String, nullable=False)  # e.g. "autonomous_run", "linkedin_monitor_sweep"
    source_run_id = Column(Integer, ForeignKey("autonomous_runs.id"), nullable=True)
    event_metadata = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
