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
        conn.execute(text("ALTER TABLE companies ADD COLUMN IF NOT EXISTS hiring_signal_posting_count INTEGER"))
        conn.execute(text("ALTER TABLE companies ADD COLUMN IF NOT EXISTS tofu_keyword_found BOOLEAN"))
        conn.execute(text("ALTER TABLE companies ADD COLUMN IF NOT EXISTS hot_lead BOOLEAN"))
        conn.execute(text("ALTER TABLE companies ADD COLUMN IF NOT EXISTS hot_lead_reasoning TEXT"))
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
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_message_drafts_opportunity_strategy ON message_drafts (opportunity_id, gtm_strategy_id)"))
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
