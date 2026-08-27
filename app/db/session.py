from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.config import settings

# pool_pre_ping guards against Neon's pooled ("-pooler") endpoint handing back a stale
# connection after the app has been idle -- see synefi/app/db/session.py for the full
# explanation; same fix applied here since this backend hits the same endpoint.
#
# connect_timeout bounds how long opening a new connection can hang (real, reproducible
# pattern: a single request times out completely, the very next one succeeds immediately --
# consistent with Neon's compute occasionally taking a while to resume from auto-suspend).
#
# statement_timeout is NOT passed via connect_args -- found live, the hard way: Neon's
# pooled ("-pooler") endpoint rejects "-c statement_timeout=..." as an unsupported startup
# parameter outright ("unsupported startup parameter in options: statement_timeout"),
# which crashed the app on every startup ("Application startup failed. Exiting.") the
# moment this was first added, silently, since Render kept the last-good process alive
# through the failed deploys rather than surfacing it immediately. Setting it via a
# post-connect SET command instead (below) works fine through the pooler, since that's a
# normal query, not a startup packet field.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
)


@event.listens_for(engine, "connect")
def _set_statement_timeout(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("SET statement_timeout = 15000")
    cursor.close()


# Found live (2026-08-21, Neon project migration) -- a pooled ("-pooler") connection can come
# back with search_path effectively empty (confirmed via SHOW search_path returning blank),
# which makes every unqualified table reference fail with "relation does not exist" even
# though the tables genuinely exist in the public schema. An ALTER DATABASE ... SET
# search_path database-level default does NOT reliably fix this for pooled connections --
# same "unsupported startup parameter" class of restriction as statement_timeout above (a
# startup-packet option is rejected outright), so the same fix applies: set it via a normal
# post-connect query instead of relying on the startup packet or the database's own default.
@event.listens_for(engine, "connect")
def _set_search_path(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("SET search_path TO public")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Indexes/tables this backend owns (campaign_events -- the SalesRobot webhook table;
# calendar_bookings -- Google Calendar appointment sync). Shared-table indexes (companies,
# contacts, etc.) are ensured by Synefi's backend, the schema owner -- see
# synefi/app/db/session.py for why this is raw SQL rather than an Alembic migration.
def ensure_indexes():
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_campaign_events_contact_id ON campaign_events (contact_id)"))
        # 2026-08-25 -- real offering/campaign tracking link (Batch.offering_name/campaign_label,
        # CampaignPush.offering_name/campaign_label; see those models' own comments for why).
        conn.execute(text("ALTER TABLE batches ADD COLUMN IF NOT EXISTS offering_name VARCHAR"))
        conn.execute(text("ALTER TABLE batches ADD COLUMN IF NOT EXISTS campaign_label VARCHAR"))
        conn.execute(text("ALTER TABLE campaign_pushes ADD COLUMN IF NOT EXISTS offering_name VARCHAR"))
        conn.execute(text("ALTER TABLE campaign_pushes ADD COLUMN IF NOT EXISTS campaign_label VARCHAR"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS calendar_bookings (
                id SERIAL PRIMARY KEY,
                google_event_id VARCHAR NOT NULL UNIQUE,
                booker_name VARCHAR,
                booker_email VARCHAR,
                start_time TIMESTAMP,
                end_time TIMESTAMP,
                status VARCHAR,
                raw_payload JSON NOT NULL,
                synced_at TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                type VARCHAR NOT NULL,
                severity VARCHAR DEFAULT 'info',
                title VARCHAR NOT NULL,
                message TEXT,
                batch_id INTEGER,
                run_id INTEGER,
                read_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_notifications_tenant_created ON notifications (tenant_id, created_at DESC)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_conversations (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                title VARCHAR,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES chat_conversations(id),
                role VARCHAR NOT NULL,
                content TEXT NOT NULL,
                tools_used JSON,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("ALTER TABLE chat_conversations ADD COLUMN IF NOT EXISTS scope VARCHAR"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_conversations_tenant_updated ON chat_conversations (tenant_id, updated_at DESC)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_messages_conversation_created ON chat_messages (conversation_id, created_at)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS daily_reviews (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                review_date VARCHAR NOT NULL,
                status VARCHAR DEFAULT 'pending',
                updated_at TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS review_comments (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                review_date VARCHAR NOT NULL,
                comment TEXT NOT NULL,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_daily_reviews_tenant_date ON daily_reviews (tenant_id, review_date)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_review_comments_tenant_date ON review_comments (tenant_id, review_date)"))
        # contacts is a shared table Synefi owns the schema for (see that repo's session.py
        # for the same ALTER TABLE IF NOT EXISTS pattern on excluded_from_push/linkedin_url).
        # Adding it here too since this backend's deploy pipeline is what this change is
        # actually going through -- the statement itself is idempotent either way.
        conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS email VARCHAR"))
        conn.execute(text("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS email_source VARCHAR"))
        conn.execute(text("ALTER TABLE personalized_messages ADD COLUMN IF NOT EXISTS email_subject VARCHAR"))
        conn.execute(text("ALTER TABLE personalized_messages ADD COLUMN IF NOT EXISTS email_body TEXT"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS linkedin_monitor_profiles (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                name VARCHAR,
                linkedin_url VARCHAR NOT NULL,
                company VARCHAR,
                active BOOLEAN DEFAULT TRUE,
                last_checked_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_linkedin_monitor_profiles_tenant_url ON linkedin_monitor_profiles (tenant_id, linkedin_url)"))
        # GTM partner categorization -- see app/phases/gtm_partner_classification.py.
        conn.execute(text("ALTER TABLE linkedin_monitor_profiles ADD COLUMN IF NOT EXISTS industry VARCHAR"))
        conn.execute(text("ALTER TABLE linkedin_monitor_profiles ADD COLUMN IF NOT EXISTS sells_to VARCHAR"))
        conn.execute(text("ALTER TABLE linkedin_monitor_profiles ADD COLUMN IF NOT EXISTS classification_status VARCHAR"))
        conn.execute(text("ALTER TABLE linkedin_monitor_profiles ADD COLUMN IF NOT EXISTS classification_confidence VARCHAR"))
        conn.execute(text("ALTER TABLE linkedin_monitor_profiles ADD COLUMN IF NOT EXISTS classification_reasoning TEXT"))
        conn.execute(text("ALTER TABLE linkedin_monitor_profiles ADD COLUMN IF NOT EXISTS classification_evidence_excerpt TEXT"))
        conn.execute(text("ALTER TABLE linkedin_monitor_profiles ADD COLUMN IF NOT EXISTS classified_at TIMESTAMP"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS linkedin_monitor_signals (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                profile_id INTEGER NOT NULL REFERENCES linkedin_monitor_profiles(id),
                post_urn VARCHAR NOT NULL,
                post_url VARCHAR,
                post_text TEXT,
                author_name VARCHAR,
                posted_at TIMESTAMP,
                matched_keywords JSON,
                tier VARCHAR,
                alerted_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_linkedin_monitor_signals_profile_urn ON linkedin_monitor_signals (profile_id, post_urn)"))
        conn.execute(text("ALTER TABLE linkedin_monitor_signals ADD COLUMN IF NOT EXISTS relevance_score INTEGER"))
        conn.execute(text("ALTER TABLE linkedin_monitor_signals ADD COLUMN IF NOT EXISTS recommended_action VARCHAR"))
        conn.execute(text("ALTER TABLE linkedin_monitor_signals ADD COLUMN IF NOT EXISTS classifier_reason TEXT"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS own_linkedin_posts (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                post_urn VARCHAR NOT NULL,
                post_url VARCHAR,
                post_text TEXT,
                posted_at TIMESTAMP,
                raw_evidence JSON,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_own_linkedin_posts_tenant_urn ON own_linkedin_posts (tenant_id, post_urn)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS job_dismissals (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                category VARCHAR NOT NULL,
                subcategory VARCHAR,
                source_type VARCHAR NOT NULL,
                source_id INTEGER NOT NULL,
                reason TEXT,
                dismissed_by VARCHAR,
                dismissed_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_job_dismissals_identity ON job_dismissals (tenant_id, category, source_type, source_id)"))
        conn.execute(text("ALTER TABLE companies ADD COLUMN IF NOT EXISTS hiring_signal_posting_count INTEGER"))
        conn.execute(text("ALTER TABLE companies ADD COLUMN IF NOT EXISTS tofu_keyword_found BOOLEAN"))
        conn.execute(text("ALTER TABLE companies ADD COLUMN IF NOT EXISTS hot_lead BOOLEAN"))
        conn.execute(text("ALTER TABLE companies ADD COLUMN IF NOT EXISTS hot_lead_reasoning TEXT"))
        # 2026-08-26, real fix -- see Company.icp_last_evaluated_at's own comment in models.py:
        # lets run_icp_matching_sweep() skip companies whose last check was already complete.
        conn.execute(text("ALTER TABLE companies ADD COLUMN IF NOT EXISTS icp_last_evaluated_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE companies ADD COLUMN IF NOT EXISTS icp_last_evaluation_had_missing_information BOOLEAN"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS reverse_discovery_candidates (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                post_urn VARCHAR NOT NULL,
                post_url VARCHAR,
                post_text TEXT,
                matched_keyword VARCHAR,
                author_name VARCHAR,
                author_profile_url VARCHAR,
                author_occupation VARCHAR,
                guessed_company_name VARCHAR,
                relevance_score INTEGER,
                recommended_action VARCHAR,
                classifier_reason TEXT,
                icp_status VARCHAR,
                icp_reasoning TEXT,
                posted_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_reverse_discovery_candidates_tenant_urn ON reverse_discovery_candidates (tenant_id, post_urn)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS gtm_signals (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                source VARCHAR NOT NULL,
                source_ref VARCHAR NOT NULL,
                signal_type VARCHAR NOT NULL,
                observed_at TIMESTAMP,
                captured_at TIMESTAMP,
                company_id INTEGER REFERENCES companies(id),
                company_name_raw VARCHAR,
                contact_id INTEGER REFERENCES contacts(id),
                person_name_raw VARCHAR,
                raw_evidence JSON,
                extracted_info JSON,
                dedup_key VARCHAR NOT NULL,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_gtm_signals_dedup_key ON gtm_signals (dedup_key)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_gtm_signals_tenant_source ON gtm_signals (tenant_id, source)"))
        # Person -> Company resolution (app/gtm_os/intelligence/company_resolution.py).
        conn.execute(text("ALTER TABLE gtm_signals ADD COLUMN IF NOT EXISTS company_resolution_status VARCHAR"))
        conn.execute(text("ALTER TABLE gtm_signals ADD COLUMN IF NOT EXISTS company_resolution_method VARCHAR"))
        conn.execute(text("ALTER TABLE gtm_signals ADD COLUMN IF NOT EXISTS company_resolution_reason TEXT"))
        conn.execute(text("ALTER TABLE gtm_signals ADD COLUMN IF NOT EXISTS company_resolved_at TIMESTAMP"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS interpreted_signals (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                source_signal_id INTEGER NOT NULL REFERENCES gtm_signals(id),
                event_type VARCHAR NOT NULL,
                affected_function VARCHAR,
                business_change TEXT NOT NULL,
                evidence_excerpt TEXT,
                extraction_method VARCHAR NOT NULL,
                extraction_confidence VARCHAR,
                company_id INTEGER REFERENCES companies(id),
                company_name_raw VARCHAR,
                contact_id INTEGER REFERENCES contacts(id),
                person_name_raw VARCHAR,
                observed_at TIMESTAMP,
                interpreted_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_interpreted_signals_source_signal_id ON interpreted_signals (source_signal_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_interpreted_signals_tenant ON interpreted_signals (tenant_id)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS problem_hypotheses (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                company_id INTEGER REFERENCES companies(id),
                company_name_raw VARCHAR,
                affected_function VARCHAR NOT NULL,
                problem_statement TEXT NOT NULL,
                reasoning_note TEXT,
                confidence JSON,
                first_observed_at TIMESTAMP,
                last_updated_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_problem_hypotheses_company_function ON problem_hypotheses (tenant_id, company_id, affected_function)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS problem_hypothesis_evidence (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                problem_hypothesis_id INTEGER NOT NULL REFERENCES problem_hypotheses(id),
                interpreted_signal_id INTEGER NOT NULL REFERENCES interpreted_signals(id),
                role VARCHAR NOT NULL,
                evidence_tier VARCHAR NOT NULL,
                note TEXT,
                added_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_problem_hypothesis_evidence_hypothesis ON problem_hypothesis_evidence (problem_hypothesis_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_problem_hypothesis_evidence_interpreted_signal ON problem_hypothesis_evidence (interpreted_signal_id)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS demand_hypotheses (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                company_id INTEGER REFERENCES companies(id),
                company_name_raw VARCHAR,
                problem_hypothesis_id INTEGER NOT NULL REFERENCES problem_hypotheses(id),
                affected_function VARCHAR NOT NULL,
                demand_statement TEXT NOT NULL,
                reasoning_note TEXT,
                confidence JSON,
                first_observed_at TIMESTAMP,
                last_updated_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_demand_hypotheses_problem_hypothesis ON demand_hypotheses (problem_hypothesis_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_demand_hypotheses_company ON demand_hypotheses (tenant_id, company_id)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS demand_hypothesis_evidence (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                demand_hypothesis_id INTEGER NOT NULL REFERENCES demand_hypotheses(id),
                interpreted_signal_id INTEGER NOT NULL REFERENCES interpreted_signals(id),
                role VARCHAR NOT NULL,
                evidence_tier VARCHAR NOT NULL,
                note TEXT,
                added_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_demand_hypothesis_evidence_hypothesis ON demand_hypothesis_evidence (demand_hypothesis_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_demand_hypothesis_evidence_interpreted_signal ON demand_hypothesis_evidence (interpreted_signal_id)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS content_topics (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                canonical_name VARCHAR NOT NULL,
                aliases JSON,
                origin VARCHAR NOT NULL,
                first_seen_at TIMESTAMP,
                last_seen_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_content_topics_tenant ON content_topics (tenant_id)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS content_topic_evidence (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                content_topic_id INTEGER NOT NULL REFERENCES content_topics(id),
                gtm_signal_id INTEGER NOT NULL REFERENCES gtm_signals(id),
                matched_term VARCHAR,
                match_method VARCHAR NOT NULL,
                added_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_content_topic_evidence_topic_signal ON content_topic_evidence (content_topic_id, gtm_signal_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_content_topic_evidence_signal ON content_topic_evidence (gtm_signal_id)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS topic_candidates (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                candidate_name VARCHAR NOT NULL,
                normalized_name VARCHAR NOT NULL,
                gtm_signal_id INTEGER NOT NULL REFERENCES gtm_signals(id),
                evidence_excerpt TEXT,
                extraction_method VARCHAR NOT NULL,
                confidence VARCHAR,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_topic_candidates_signal ON topic_candidates (gtm_signal_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_topic_candidates_tenant_normalized ON topic_candidates (tenant_id, normalized_name)"))
        # Step 16E-5 -- candidate normalization/consolidation additive columns (see
        # app/gtm_os/content/candidate_normalization.py and topic-candidate-normalization-design.md).
        conn.execute(text("ALTER TABLE topic_candidates ADD COLUMN IF NOT EXISTS cluster_key VARCHAR"))
        conn.execute(text("ALTER TABLE topic_candidates ADD COLUMN IF NOT EXISTS normalization_method VARCHAR"))
        conn.execute(text("ALTER TABLE topic_candidates ADD COLUMN IF NOT EXISTS normalization_reason TEXT"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_topic_candidates_tenant_cluster ON topic_candidates (tenant_id, cluster_key)"))
        # Batch 4 -- Opportunity Engine (see app/gtm_os/opportunity/opportunity.py). One
        # Opportunity per DemandHypothesis -- unique index enforces this at the DB level too, not
        # just at the application check-before-insert layer.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                company_id INTEGER REFERENCES companies(id),
                company_name_raw VARCHAR,
                demand_hypothesis_id INTEGER NOT NULL REFERENCES demand_hypotheses(id),
                problem_hypothesis_id INTEGER NOT NULL REFERENCES problem_hypotheses(id),
                affected_function VARCHAR NOT NULL,
                opportunity_statement TEXT NOT NULL,
                reasoning_note TEXT,
                status VARCHAR NOT NULL DEFAULT 'candidate',
                confidence JSON,
                first_observed_at TIMESTAMP,
                last_updated_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_opportunities_demand_hypothesis ON opportunities (demand_hypothesis_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_opportunities_tenant_company ON opportunities (tenant_id, company_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_opportunities_tenant_status ON opportunities (tenant_id, status)"))
        # V2 Phase 2 -- ICP awareness (see app/gtm_os/icp/icp_matching.py::get_icp_context_for_company).
        # Snapshot of already-computed ICPMatch rows at Opportunity-creation time -- a soft
        # signal for Strategy/Account Agent prioritization, never a re-computation and never a
        # hard eligibility gate (Problem+Demand eligibility above is completely unchanged).
        conn.execute(text("ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS icp_context JSON"))
        # Batch 5 -- GTM Strategy + Action Planning (see app/gtm_os/strategy/strategy.py). Multiple
        # rows per opportunity_id are expected (historical versions, never overwritten) -- no
        # unique constraint here, unlike opportunities' own demand_hypothesis_id.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS gtm_strategies (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
                strategy_type VARCHAR NOT NULL,
                recommended_approach TEXT,
                target_function VARCHAR,
                positioning_angle TEXT,
                offering_fit_status VARCHAR,
                matched_offering_name VARCHAR,
                evidence_basis JSON,
                constraints JSON,
                missing_information JSON,
                action_plan JSON,
                recommended_next_step VARCHAR,
                reasoning_note TEXT,
                created_at TIMESTAMP,
                last_updated_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_gtm_strategies_tenant_opportunity ON gtm_strategies (tenant_id, opportunity_id)"))
        # V2 Phase 6 -- decision-maker/contact staleness fix (see strategy.py's GtmStrategy
        # docstring). Existing rows get NULL here (never backfilled with a guess), which
        # _facts_changed() will correctly treat as "changed" against a real True/False the very
        # next time each is evaluated -- a one-time, harmless re-versioning catch-up, not a
        # repeating instability.
        conn.execute(text("ALTER TABLE gtm_strategies ADD COLUMN IF NOT EXISTS decision_maker_known BOOLEAN"))
        # Batch 7 -- Learning/Evaluation foundation (see app/gtm_os/learning/). SalesOutcome
        # reuses InterpretedSignal's own dedup guarantee via the unique interpreted_signal_id
        # index below; MessageDraft is unique per (opportunity, strategy version).
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sales_outcomes (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                interpreted_signal_id INTEGER NOT NULL REFERENCES interpreted_signals(id),
                gtm_signal_id INTEGER NOT NULL REFERENCES gtm_signals(id),
                opportunity_id INTEGER REFERENCES opportunities(id),
                contact_id INTEGER REFERENCES contacts(id),
                outcome_category VARCHAR NOT NULL,
                source_event_type VARCHAR NOT NULL,
                reasoning_note TEXT,
                observed_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_sales_outcomes_interpreted_signal ON sales_outcomes (interpreted_signal_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sales_outcomes_tenant_opportunity ON sales_outcomes (tenant_id, opportunity_id)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS message_drafts (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
                gtm_strategy_id INTEGER NOT NULL REFERENCES gtm_strategies(id),
                contact_id INTEGER REFERENCES contacts(id),
                channel VARCHAR,
                objective TEXT,
                target_role VARCHAR,
                positioning_angle TEXT,
                evidence_basis JSON,
                personalization_inputs JSON,
                message_text TEXT,
                generation_method VARCHAR NOT NULL,
                missing_information JSON,
                status VARCHAR NOT NULL DEFAULT 'insufficient_context',
                quality_gate_reasons JSON,
                approved_at TIMESTAMP,
                approved_by VARCHAR,
                created_at TIMESTAMP,
                last_updated_at TIMESTAMP
            )
        """))
        # V2 Phase 8 -- widened from a single (opportunity_id, gtm_strategy_id) unique index to
        # two PARTIAL unique indexes, so a strategy version can have at most one no-contact
        # draft (contact_id IS NULL, the insufficient_context case -- preserves the exact
        # Phase 5/6/7 guarantee unchanged) AND at most one draft PER CONTACT once contact
        # discovery/sequencing has real people to target (the new Phase 8 guarantee -- lets
        # the primary's and each fallback's drafts coexist, each with their own real send
        # history, rather than overwriting/deleting one to make room for the next). The old
        # single index is dropped first -- it would otherwise block the second contact's draft
        # outright.
        conn.execute(text("DROP INDEX IF EXISTS ix_message_drafts_opportunity_strategy"))
        # 2026-08-25 real schema fix: widened to include `channel` -- a real contact with BOTH a
        # LinkedIn URL and an email now gets a draft PER channel (V1 parity, see
        # message_draft.py's generate_message_draft() docstring). The old (opportunity, strategy,
        # contact)-only index would have rejected the second channel's row for the same contact
        # outright. Safe/additive: only widens what's allowed, never invalidates any existing row
        # (every row created before this change has exactly one (opportunity, strategy, contact)
        # combination, which trivially still satisfies the new, more permissive index too).
        conn.execute(text("DROP INDEX IF EXISTS ix_message_drafts_opp_strategy_no_contact"))
        conn.execute(text("DROP INDEX IF EXISTS ix_message_drafts_opp_strategy_contact"))
        # 2026-08-26, explicit instruction -- real SalesRobot campaigns now have their own Step 3
        # ("no reply yet") that needs its OWN generated MessageDraft (message_role="followup"),
        # distinct from the primary pitch (message_role="primary", the default -- see
        # message_draft.py's MessageDraft.message_role). Widened AGAIN to include message_role, so
        # a primary and a followup row can coexist for the same (opportunity, strategy, contact,
        # channel) -- the old channel-only index would have rejected the followup row outright.
        conn.execute(text("DROP INDEX IF EXISTS ix_message_drafts_opp_strategy_channel_no_contact"))
        conn.execute(text("DROP INDEX IF EXISTS ix_message_drafts_opp_strategy_contact_channel"))
        conn.execute(text("ALTER TABLE message_drafts ADD COLUMN IF NOT EXISTS message_role VARCHAR NOT NULL DEFAULT 'primary'"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_message_drafts_opp_strategy_channel_role_no_contact "
            "ON message_drafts (opportunity_id, gtm_strategy_id, channel, message_role) WHERE contact_id IS NULL"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_message_drafts_opp_strategy_contact_channel_role "
            "ON message_drafts (opportunity_id, gtm_strategy_id, contact_id, channel, message_role) WHERE contact_id IS NOT NULL"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_message_drafts_tenant_status ON message_drafts (tenant_id, status)"))
        # Phase 7 (V2 human review) -- generalized review fields alongside the existing
        # approved_at/approved_by (never renamed/removed -- Phase 3's V2 Messages tab already
        # reads those two verbatim). approve_message_draft() now sets reviewed_at/reviewed_by
        # too, in addition to its existing approved_at/approved_by, so "approved" is just one of
        # three terminal review outcomes sharing one generalized pair of fields -- see
        # app/gtm_os/learning/message_draft.py's reject_message_draft()/
        # request_changes_message_draft() (the other two).
        conn.execute(text("ALTER TABLE message_drafts ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE message_drafts ADD COLUMN IF NOT EXISTS reviewed_by VARCHAR"))
        conn.execute(text("ALTER TABLE message_drafts ADD COLUMN IF NOT EXISTS review_note TEXT"))
        # V2 Phase 7 follow-up -- subject, generated in the SAME LLM call as message_text (see
        # message_draft.py's MESSAGE_GENERATION_PROMPT). Existing rows get NULL, stay fully
        # readable; nothing re-generates them.
        conn.execute(text("ALTER TABLE message_drafts ADD COLUMN IF NOT EXISTS subject TEXT"))
        # V2 Phase 7 -- send state (see app/gtm_os/send/send_state.py). Insert-only, one row per
        # real send ATTEMPT -- same discipline as campaign_pushes, kept as its own table (not a
        # reuse of campaign_pushes) since a V2 send is keyed to a MessageDraft, which
        # campaign_pushes has no concept of at all.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS message_send_attempts (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                message_draft_id INTEGER NOT NULL REFERENCES message_drafts(id),
                contact_id INTEGER NOT NULL REFERENCES contacts(id),
                channel VARCHAR NOT NULL,
                provider VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                provider_ref VARCHAR,
                error_message TEXT,
                retryable BOOLEAN,
                attempted_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_message_send_attempts_draft ON message_send_attempts (message_draft_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_message_send_attempts_contact ON message_send_attempts (contact_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_message_send_attempts_tenant_status ON message_send_attempts (tenant_id, status)"))
        # Backend Batch 8 -- ICP + Trigger Intelligence (see app/gtm_os/icp/icp_matching.py). One
        # row per (company, ICP) pair that CURRENTLY matches -- unique constraint enforces the
        # upsert-in-place idempotency at the DB level too.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS icp_matches (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                company_id INTEGER NOT NULL REFERENCES companies(id),
                icp_id VARCHAR NOT NULL,
                reasons JSON NOT NULL,
                trigger_evidence JSON NOT NULL,
                evaluated_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_icp_matches_tenant_company_icp ON icp_matches (tenant_id, company_id, icp_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_icp_matches_tenant_icp ON icp_matches (tenant_id, icp_id)"))
        # V2 Meeting Outcomes / Revenue Pace -- see app/gtm_os/revenue/revenue_pace.py. Additive
        # columns on the existing calendar_bookings table; outcome_status null means pending/unset.
        conn.execute(text("ALTER TABLE calendar_bookings ADD COLUMN IF NOT EXISTS outcome_status VARCHAR"))
        conn.execute(text("ALTER TABLE calendar_bookings ADD COLUMN IF NOT EXISTS outcome_company_id INTEGER REFERENCES companies(id)"))
        conn.execute(text("ALTER TABLE calendar_bookings ADD COLUMN IF NOT EXISTS outcome_offering_name VARCHAR"))
        conn.execute(text("ALTER TABLE calendar_bookings ADD COLUMN IF NOT EXISTS outcome_amount_usd FLOAT"))
        conn.execute(text("ALTER TABLE calendar_bookings ADD COLUMN IF NOT EXISTS outcome_reason TEXT"))
        conn.execute(text("ALTER TABLE calendar_bookings ADD COLUMN IF NOT EXISTS outcome_notes TEXT"))
        conn.execute(text("ALTER TABLE calendar_bookings ADD COLUMN IF NOT EXISTS outcome_icp_snapshot JSON"))
        # 2026-08-27, real fix -- see CalendarBooking.outcome_channel's own comment in models.py.
        conn.execute(text("ALTER TABLE calendar_bookings ADD COLUMN IF NOT EXISTS outcome_channel VARCHAR"))
        conn.execute(text("ALTER TABLE calendar_bookings ADD COLUMN IF NOT EXISTS outcome_recorded_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE calendar_bookings ADD COLUMN IF NOT EXISTS outcome_recorded_by VARCHAR"))
        # V2 Overrides & Evals -- meeting -> Opportunity attribution link, optional, human-supplied.
        conn.execute(text("ALTER TABLE calendar_bookings ADD COLUMN IF NOT EXISTS outcome_opportunity_id INTEGER REFERENCES opportunities(id)"))
        # V2 Efficiency -- see app/gtm_os/efficiency/. duplicates_removed was already computed by
        # _dedupe_against_prior_days() (autonomous_orchestrator.py) but previously discarded.
        conn.execute(text("ALTER TABLE autonomous_runs ADD COLUMN IF NOT EXISTS duplicates_removed INTEGER"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS efficiency_activity_events (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                activity_type VARCHAR NOT NULL,
                activity_date DATE NOT NULL,
                volume INTEGER NOT NULL,
                source VARCHAR NOT NULL,
                source_run_id INTEGER REFERENCES autonomous_runs(id),
                metadata JSON,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_efficiency_activity_tenant_type_date ON efficiency_activity_events (tenant_id, activity_type, activity_date)"))
        # V2 Overrides & Evals -- see app/gtm_os/learning/overrides_evals.py. Only ConfirmedPattern
        # requires persistence; candidates themselves are computed live, never stored.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS confirmed_patterns (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                category VARCHAR NOT NULL,
                trigger_description TEXT NOT NULL,
                pattern_description TEXT NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'candidate',
                source_event_refs JSON NOT NULL,
                confirmed_by VARCHAR,
                confirmed_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_confirmed_patterns_tenant_category ON confirmed_patterns (tenant_id, category)"))
        # GTM-OS architecture upgrade -- human-provided knowledge (see
        # app/gtm_os/learning/human_knowledge.py). Explicit provenance/status columns; never
        # written to by anything except submit_/confirm_/dismiss_human_knowledge().
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS human_knowledge (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                original_text TEXT NOT NULL,
                source VARCHAR NOT NULL DEFAULT 'human_input',
                interpretation TEXT,
                status VARCHAR NOT NULL DEFAULT 'pending_review',
                created_by VARCHAR,
                created_at TIMESTAMP,
                confirmed_by VARCHAR,
                confirmed_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_human_knowledge_tenant_status ON human_knowledge (tenant_id, status)"))
        # GTM-OS end-to-end wiring -- durable run-state for run_gtm_intelligence_sweep(), see
        # app/gtm_os/orchestration/sweep.py's GtmIntelligenceRun for why this is its own table
        # rather than a reuse of autonomous_runs.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS gtm_intelligence_runs (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'running',
                stage_results JSON,
                error_summary VARCHAR,
                started_at TIMESTAMP,
                completed_at TIMESTAMP
            )
        """))
        # Unresolved-company ProblemHypothesis dedup fix -- see problem_detection.py's
        # _get_open_hypothesis(). person_name_raw is the fallback grouping identity when
        # company_id can't be resolved (e.g. a LinkedIn post whose author's employer couldn't be
        # parsed). The three unique indexes below are the real fix for the duplicate-hypothesis
        # bug confirmed against production on 2026-08-18 (two ProblemHypothesis rows opened from
        # one InterpretedSignal by two concurrent sweep calls) -- app-level "check then create" is
        # not race-safe by itself; these make the DB reject the second concurrent insert instead
        # of silently creating a duplicate, and evaluate_interpreted_signal() catches that
        # IntegrityError and re-reads the real row rather than raising.
        conn.execute(text("ALTER TABLE problem_hypotheses ADD COLUMN IF NOT EXISTS person_name_raw VARCHAR"))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS ix_problem_hypotheses_company_unique
            ON problem_hypotheses (tenant_id, affected_function, company_id)
            WHERE company_id IS NOT NULL
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS ix_problem_hypotheses_person_unique
            ON problem_hypotheses (tenant_id, affected_function, person_name_raw)
            WHERE company_id IS NULL AND person_name_raw IS NOT NULL
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS ix_problem_hypothesis_evidence_signal_unique
            ON problem_hypothesis_evidence (interpreted_signal_id)
        """))
        # V2 Briefing performance fix -- see governance.py's GovernanceSnapshot. Moves
        # evaluate_gtm_governance()'s expensive live sweep off the request path onto the hourly
        # scheduler; the API route reads the latest row here instead of recomputing.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS gtm_governance_snapshots (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                snapshot JSON NOT NULL,
                computed_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_gtm_governance_snapshots_tenant_computed ON gtm_governance_snapshots (tenant_id, computed_at DESC)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_gtm_intelligence_runs_tenant_started ON gtm_intelligence_runs (tenant_id, started_at)"))
        # Autonomous Sensing Phase S1 -- see app/gtm_os/intelligence/investigation_memory.py.
        # The structured InvestigationObjective is the durable memory; no query text is stored.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS investigation_objectives (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                icp_id VARCHAR NOT NULL,
                target_company_id INTEGER REFERENCES companies(id),
                claim TEXT NOT NULL,
                evidence_sought VARCHAR NOT NULL,
                reason TEXT NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'open',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempted_at TIMESTAMP,
                next_eligible_at TIMESTAMP,
                stopped_reason TEXT,
                source_attempted VARCHAR,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_investigation_objectives_tenant_icp_company ON investigation_objectives (tenant_id, icp_id, target_company_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_investigation_objectives_tenant_status ON investigation_objectives (tenant_id, status)"))
