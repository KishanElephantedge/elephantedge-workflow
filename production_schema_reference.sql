--
-- PostgreSQL database dump
--

\restrict UbbHFW6wHQIbHYQM103DhG8vcwSLL8VNhqwuFd2baO8bdIKHT8Ot1u4SYsI0j7x

-- Dumped from database version 18.6 (3484359)
-- Dumped by pg_dump version 18.6 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: autonomous_runs; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.autonomous_runs (
    id integer NOT NULL,
    run_date timestamp without time zone,
    batch_id integer,
    status character varying,
    companies_discovered integer,
    companies_selected integer,
    contacts_found integer,
    contacts_pushed integer,
    credits_spent_usd double precision,
    budget_stopped_early boolean,
    error_message text,
    started_at timestamp without time zone,
    completed_at timestamp without time zone,
    awaiting_approval_until timestamp without time zone,
    cancelled boolean DEFAULT false,
    duplicates_removed integer
);


ALTER TABLE public.autonomous_runs OWNER TO neondb_owner;

--
-- Name: autonomous_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.autonomous_runs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.autonomous_runs_id_seq OWNER TO neondb_owner;

--
-- Name: autonomous_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.autonomous_runs_id_seq OWNED BY public.autonomous_runs.id;


--
-- Name: batches; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.batches (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    name character varying NOT NULL,
    created_at timestamp without time zone,
    current_phase character varying,
    status character varying,
    source character varying DEFAULT 'deepline'::character varying NOT NULL
);


ALTER TABLE public.batches OWNER TO neondb_owner;

--
-- Name: batches_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.batches_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.batches_id_seq OWNER TO neondb_owner;

--
-- Name: batches_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.batches_id_seq OWNED BY public.batches.id;


--
-- Name: calendar_bookings; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.calendar_bookings (
    id integer NOT NULL,
    google_event_id character varying NOT NULL,
    booker_name character varying,
    booker_email character varying,
    start_time timestamp without time zone,
    end_time timestamp without time zone,
    status character varying,
    raw_payload json NOT NULL,
    synced_at timestamp without time zone,
    outcome_status character varying,
    outcome_company_id integer,
    outcome_offering_name character varying,
    outcome_amount_usd double precision,
    outcome_reason text,
    outcome_notes text,
    outcome_icp_snapshot json,
    outcome_recorded_at timestamp without time zone,
    outcome_recorded_by character varying,
    outcome_opportunity_id integer
);


ALTER TABLE public.calendar_bookings OWNER TO neondb_owner;

--
-- Name: calendar_bookings_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.calendar_bookings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.calendar_bookings_id_seq OWNER TO neondb_owner;

--
-- Name: calendar_bookings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.calendar_bookings_id_seq OWNED BY public.calendar_bookings.id;


--
-- Name: campaign_events; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.campaign_events (
    id integer NOT NULL,
    contact_id integer,
    event_type character varying,
    raw_payload json NOT NULL,
    received_at timestamp without time zone
);


ALTER TABLE public.campaign_events OWNER TO neondb_owner;

--
-- Name: campaign_events_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.campaign_events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.campaign_events_id_seq OWNER TO neondb_owner;

--
-- Name: campaign_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.campaign_events_id_seq OWNED BY public.campaign_events.id;


--
-- Name: campaign_pushes; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.campaign_pushes (
    id integer NOT NULL,
    contact_id integer NOT NULL,
    heyreach_campaign_id character varying,
    status character varying,
    error_message text,
    pushed_at timestamp without time zone
);


ALTER TABLE public.campaign_pushes OWNER TO neondb_owner;

--
-- Name: campaign_pushes_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.campaign_pushes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.campaign_pushes_id_seq OWNER TO neondb_owner;

--
-- Name: campaign_pushes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.campaign_pushes_id_seq OWNED BY public.campaign_pushes.id;


--
-- Name: chat_conversations; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.chat_conversations (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    title character varying,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


ALTER TABLE public.chat_conversations OWNER TO neondb_owner;

--
-- Name: chat_conversations_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.chat_conversations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.chat_conversations_id_seq OWNER TO neondb_owner;

--
-- Name: chat_conversations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.chat_conversations_id_seq OWNED BY public.chat_conversations.id;


--
-- Name: chat_messages; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.chat_messages (
    id integer NOT NULL,
    conversation_id integer NOT NULL,
    role character varying NOT NULL,
    content text NOT NULL,
    tools_used json,
    created_at timestamp without time zone
);


ALTER TABLE public.chat_messages OWNER TO neondb_owner;

--
-- Name: chat_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.chat_messages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.chat_messages_id_seq OWNER TO neondb_owner;

--
-- Name: chat_messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.chat_messages_id_seq OWNED BY public.chat_messages.id;


--
-- Name: companies; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.companies (
    id integer NOT NULL,
    batch_id integer NOT NULL,
    name character varying NOT NULL,
    domain character varying,
    industry character varying,
    employee_count integer,
    location character varying,
    created_at timestamp without time zone,
    decision_maker_searched_at timestamp without time zone,
    estimated_revenue_lower_usd integer,
    estimated_revenue_higher_usd integer,
    last_funding_round_type character varying,
    last_funding_date timestamp without time zone,
    crunchbase_total_investment_usd double precision,
    headcount_growth_12m_percent double precision,
    source character varying,
    active_head_of_sales_posting boolean,
    buying_signal_checked_at timestamp without time zone,
    sales_headcount_percent double precision,
    marketing_headcount_percent double precision,
    geography_tier character varying,
    industry_classification character varying,
    active_job_title character varying,
    hiring_signal_role character varying,
    hiring_signal_hire_type character varying,
    hiring_signal_strength character varying,
    hiring_signal_reasoning text,
    detected_tech_stack json,
    has_outbound_tooling boolean,
    has_ai_sdr_tool boolean,
    tech_stack_checked_at timestamp without time zone,
    product_fit_jd_categories json,
    team_fit_tier character varying,
    team_fit_reasoning text,
    linkedin_url character varying,
    hiring_signal_posting_count integer,
    tofu_keyword_found boolean,
    hot_lead boolean,
    hot_lead_reasoning text
);


ALTER TABLE public.companies OWNER TO neondb_owner;

--
-- Name: companies_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.companies_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.companies_id_seq OWNER TO neondb_owner;

--
-- Name: companies_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.companies_id_seq OWNED BY public.companies.id;


--
-- Name: confirmed_patterns; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.confirmed_patterns (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    category character varying NOT NULL,
    trigger_description text NOT NULL,
    pattern_description text NOT NULL,
    status character varying DEFAULT 'candidate'::character varying NOT NULL,
    source_event_refs json NOT NULL,
    confirmed_by character varying,
    confirmed_at timestamp without time zone,
    created_at timestamp without time zone
);


ALTER TABLE public.confirmed_patterns OWNER TO neondb_owner;

--
-- Name: confirmed_patterns_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.confirmed_patterns_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.confirmed_patterns_id_seq OWNER TO neondb_owner;

--
-- Name: confirmed_patterns_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.confirmed_patterns_id_seq OWNED BY public.confirmed_patterns.id;


--
-- Name: contacts; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.contacts (
    id integer NOT NULL,
    company_id integer NOT NULL,
    first_name character varying,
    last_name character varying,
    title character varying,
    linkedin_url character varying,
    thread_role character varying,
    matched_title_reasoning text,
    created_at timestamp without time zone,
    hubspot_synced_at timestamp without time zone,
    personalization_hook text,
    excluded_from_push boolean DEFAULT false,
    email character varying,
    email_source character varying
);


ALTER TABLE public.contacts OWNER TO neondb_owner;

--
-- Name: contacts_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.contacts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.contacts_id_seq OWNER TO neondb_owner;

--
-- Name: contacts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.contacts_id_seq OWNED BY public.contacts.id;


--
-- Name: content_topic_evidence; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.content_topic_evidence (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    content_topic_id integer NOT NULL,
    gtm_signal_id integer NOT NULL,
    matched_term character varying,
    match_method character varying NOT NULL,
    added_at timestamp without time zone
);


ALTER TABLE public.content_topic_evidence OWNER TO neondb_owner;

--
-- Name: content_topic_evidence_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.content_topic_evidence_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.content_topic_evidence_id_seq OWNER TO neondb_owner;

--
-- Name: content_topic_evidence_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.content_topic_evidence_id_seq OWNED BY public.content_topic_evidence.id;


--
-- Name: content_topics; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.content_topics (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    canonical_name character varying NOT NULL,
    aliases json,
    origin character varying NOT NULL,
    first_seen_at timestamp without time zone,
    last_seen_at timestamp without time zone,
    created_at timestamp without time zone
);


ALTER TABLE public.content_topics OWNER TO neondb_owner;

--
-- Name: content_topics_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.content_topics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.content_topics_id_seq OWNER TO neondb_owner;

--
-- Name: content_topics_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.content_topics_id_seq OWNED BY public.content_topics.id;


--
-- Name: credentials; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.credentials (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    name character varying NOT NULL,
    value character varying NOT NULL,
    updated_at timestamp without time zone
);


ALTER TABLE public.credentials OWNER TO neondb_owner;

--
-- Name: credentials_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.credentials_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.credentials_id_seq OWNER TO neondb_owner;

--
-- Name: credentials_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.credentials_id_seq OWNED BY public.credentials.id;


--
-- Name: daily_reviews; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.daily_reviews (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    review_date character varying NOT NULL,
    status character varying DEFAULT 'pending'::character varying,
    updated_at timestamp without time zone
);


ALTER TABLE public.daily_reviews OWNER TO neondb_owner;

--
-- Name: daily_reviews_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.daily_reviews_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.daily_reviews_id_seq OWNER TO neondb_owner;

--
-- Name: daily_reviews_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.daily_reviews_id_seq OWNED BY public.daily_reviews.id;


--
-- Name: demand_hypotheses; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.demand_hypotheses (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    company_id integer,
    company_name_raw character varying,
    problem_hypothesis_id integer NOT NULL,
    affected_function character varying NOT NULL,
    demand_statement text NOT NULL,
    reasoning_note text,
    confidence json,
    first_observed_at timestamp without time zone,
    last_updated_at timestamp without time zone,
    created_at timestamp without time zone
);


ALTER TABLE public.demand_hypotheses OWNER TO neondb_owner;

--
-- Name: demand_hypotheses_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.demand_hypotheses_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.demand_hypotheses_id_seq OWNER TO neondb_owner;

--
-- Name: demand_hypotheses_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.demand_hypotheses_id_seq OWNED BY public.demand_hypotheses.id;


--
-- Name: demand_hypothesis_evidence; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.demand_hypothesis_evidence (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    demand_hypothesis_id integer NOT NULL,
    interpreted_signal_id integer NOT NULL,
    role character varying NOT NULL,
    evidence_tier character varying NOT NULL,
    note text,
    added_at timestamp without time zone
);


ALTER TABLE public.demand_hypothesis_evidence OWNER TO neondb_owner;

--
-- Name: demand_hypothesis_evidence_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.demand_hypothesis_evidence_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.demand_hypothesis_evidence_id_seq OWNER TO neondb_owner;

--
-- Name: demand_hypothesis_evidence_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.demand_hypothesis_evidence_id_seq OWNED BY public.demand_hypothesis_evidence.id;


--
-- Name: efficiency_activity_events; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.efficiency_activity_events (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    activity_type character varying NOT NULL,
    activity_date date NOT NULL,
    volume integer NOT NULL,
    source character varying NOT NULL,
    source_run_id integer,
    metadata json,
    created_at timestamp without time zone
);


ALTER TABLE public.efficiency_activity_events OWNER TO neondb_owner;

--
-- Name: efficiency_activity_events_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.efficiency_activity_events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.efficiency_activity_events_id_seq OWNER TO neondb_owner;

--
-- Name: efficiency_activity_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.efficiency_activity_events_id_seq OWNED BY public.efficiency_activity_events.id;


--
-- Name: enrichments; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.enrichments (
    id integer NOT NULL,
    contact_id integer NOT NULL,
    verified_email character varying,
    mobile_phone character varying,
    company_hq_address character varying,
    twitter_x character varying,
    blog_or_substack character varying,
    podcast_appearances json,
    upcoming_conference_slots json,
    channel_plan character varying,
    subject_line character varying,
    email_body text,
    linkedin_connection_note text,
    created_at timestamp without time zone
);


ALTER TABLE public.enrichments OWNER TO neondb_owner;

--
-- Name: enrichments_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.enrichments_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.enrichments_id_seq OWNER TO neondb_owner;

--
-- Name: enrichments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.enrichments_id_seq OWNED BY public.enrichments.id;


--
-- Name: gtm_governance_snapshots; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.gtm_governance_snapshots (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    snapshot json NOT NULL,
    computed_at timestamp without time zone
);


ALTER TABLE public.gtm_governance_snapshots OWNER TO neondb_owner;

--
-- Name: gtm_governance_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.gtm_governance_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.gtm_governance_snapshots_id_seq OWNER TO neondb_owner;

--
-- Name: gtm_governance_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.gtm_governance_snapshots_id_seq OWNED BY public.gtm_governance_snapshots.id;


--
-- Name: gtm_intelligence_runs; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.gtm_intelligence_runs (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    status character varying DEFAULT 'running'::character varying NOT NULL,
    stage_results json,
    error_summary character varying,
    started_at timestamp without time zone,
    completed_at timestamp without time zone
);


ALTER TABLE public.gtm_intelligence_runs OWNER TO neondb_owner;

--
-- Name: gtm_intelligence_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.gtm_intelligence_runs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.gtm_intelligence_runs_id_seq OWNER TO neondb_owner;

--
-- Name: gtm_intelligence_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.gtm_intelligence_runs_id_seq OWNED BY public.gtm_intelligence_runs.id;


--
-- Name: gtm_signals; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.gtm_signals (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    source character varying NOT NULL,
    source_ref character varying NOT NULL,
    signal_type character varying NOT NULL,
    observed_at timestamp without time zone,
    captured_at timestamp without time zone,
    company_id integer,
    company_name_raw character varying,
    contact_id integer,
    person_name_raw character varying,
    raw_evidence json,
    extracted_info json,
    dedup_key character varying NOT NULL,
    created_at timestamp without time zone
);


ALTER TABLE public.gtm_signals OWNER TO neondb_owner;

--
-- Name: gtm_signals_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.gtm_signals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.gtm_signals_id_seq OWNER TO neondb_owner;

--
-- Name: gtm_signals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.gtm_signals_id_seq OWNED BY public.gtm_signals.id;


--
-- Name: gtm_strategies; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.gtm_strategies (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    opportunity_id integer NOT NULL,
    strategy_type character varying NOT NULL,
    recommended_approach text,
    target_function character varying,
    positioning_angle text,
    offering_fit_status character varying,
    matched_offering_name character varying,
    evidence_basis json,
    constraints json,
    missing_information json,
    action_plan json,
    recommended_next_step character varying,
    reasoning_note text,
    created_at timestamp without time zone,
    last_updated_at timestamp without time zone
);


ALTER TABLE public.gtm_strategies OWNER TO neondb_owner;

--
-- Name: gtm_strategies_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.gtm_strategies_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.gtm_strategies_id_seq OWNER TO neondb_owner;

--
-- Name: gtm_strategies_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.gtm_strategies_id_seq OWNED BY public.gtm_strategies.id;


--
-- Name: human_knowledge; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.human_knowledge (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    original_text text NOT NULL,
    source character varying DEFAULT 'human_input'::character varying NOT NULL,
    interpretation text,
    status character varying DEFAULT 'pending_review'::character varying NOT NULL,
    created_by character varying,
    created_at timestamp without time zone,
    confirmed_by character varying,
    confirmed_at timestamp without time zone
);


ALTER TABLE public.human_knowledge OWNER TO neondb_owner;

--
-- Name: human_knowledge_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.human_knowledge_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.human_knowledge_id_seq OWNER TO neondb_owner;

--
-- Name: human_knowledge_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.human_knowledge_id_seq OWNED BY public.human_knowledge.id;


--
-- Name: icp_matches; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.icp_matches (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    company_id integer NOT NULL,
    icp_id character varying NOT NULL,
    reasons json NOT NULL,
    trigger_evidence json NOT NULL,
    evaluated_at timestamp without time zone,
    created_at timestamp without time zone
);


ALTER TABLE public.icp_matches OWNER TO neondb_owner;

--
-- Name: icp_matches_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.icp_matches_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.icp_matches_id_seq OWNER TO neondb_owner;

--
-- Name: icp_matches_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.icp_matches_id_seq OWNED BY public.icp_matches.id;


--
-- Name: interpreted_signals; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.interpreted_signals (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    source_signal_id integer NOT NULL,
    event_type character varying NOT NULL,
    affected_function character varying,
    business_change text NOT NULL,
    evidence_excerpt text,
    extraction_method character varying NOT NULL,
    extraction_confidence character varying,
    company_id integer,
    company_name_raw character varying,
    contact_id integer,
    person_name_raw character varying,
    observed_at timestamp without time zone,
    interpreted_at timestamp without time zone,
    created_at timestamp without time zone
);


ALTER TABLE public.interpreted_signals OWNER TO neondb_owner;

--
-- Name: interpreted_signals_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.interpreted_signals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.interpreted_signals_id_seq OWNER TO neondb_owner;

--
-- Name: interpreted_signals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.interpreted_signals_id_seq OWNED BY public.interpreted_signals.id;


--
-- Name: linkedin_monitor_profiles; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.linkedin_monitor_profiles (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    name character varying,
    linkedin_url character varying NOT NULL,
    company character varying,
    active boolean DEFAULT true,
    last_checked_at timestamp without time zone,
    created_at timestamp without time zone,
    industry character varying,
    sells_to character varying,
    classification_status character varying,
    classification_confidence character varying,
    classification_reasoning text,
    classification_evidence_excerpt text,
    classified_at timestamp without time zone,
    title character varying,
    employee_count integer,
    location character varying,
    company_website character varying,
    source character varying DEFAULT 'linkedin'::character varying,
    team_size character varying,
    gtm_university_data json,
    gtm_university_synced_at timestamp without time zone,
    slack_user_id character varying
);


ALTER TABLE public.linkedin_monitor_profiles OWNER TO neondb_owner;

--
-- Name: linkedin_monitor_profiles_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.linkedin_monitor_profiles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.linkedin_monitor_profiles_id_seq OWNER TO neondb_owner;

--
-- Name: linkedin_monitor_profiles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.linkedin_monitor_profiles_id_seq OWNED BY public.linkedin_monitor_profiles.id;


--
-- Name: linkedin_monitor_signals; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.linkedin_monitor_signals (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    profile_id integer NOT NULL,
    post_urn character varying NOT NULL,
    post_url character varying,
    post_text text,
    author_name character varying,
    posted_at timestamp without time zone,
    matched_keywords json,
    tier character varying,
    alerted_at timestamp without time zone,
    created_at timestamp without time zone,
    relevance_score integer,
    recommended_action character varying,
    classifier_reason text
);


ALTER TABLE public.linkedin_monitor_signals OWNER TO neondb_owner;

--
-- Name: linkedin_monitor_signals_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.linkedin_monitor_signals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.linkedin_monitor_signals_id_seq OWNER TO neondb_owner;

--
-- Name: linkedin_monitor_signals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.linkedin_monitor_signals_id_seq OWNED BY public.linkedin_monitor_signals.id;


--
-- Name: message_drafts; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.message_drafts (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    opportunity_id integer NOT NULL,
    gtm_strategy_id integer NOT NULL,
    contact_id integer,
    channel character varying,
    objective text,
    target_role character varying,
    positioning_angle text,
    evidence_basis json,
    personalization_inputs json,
    message_text text,
    generation_method character varying NOT NULL,
    missing_information json,
    status character varying DEFAULT 'insufficient_context'::character varying NOT NULL,
    quality_gate_reasons json,
    approved_at timestamp without time zone,
    approved_by character varying,
    created_at timestamp without time zone,
    last_updated_at timestamp without time zone,
    reviewed_at timestamp without time zone,
    reviewed_by character varying,
    review_note text
);


ALTER TABLE public.message_drafts OWNER TO neondb_owner;

--
-- Name: message_drafts_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.message_drafts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.message_drafts_id_seq OWNER TO neondb_owner;

--
-- Name: message_drafts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.message_drafts_id_seq OWNED BY public.message_drafts.id;


--
-- Name: notifications; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.notifications (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    type character varying NOT NULL,
    severity character varying DEFAULT 'info'::character varying,
    title character varying NOT NULL,
    message text,
    batch_id integer,
    run_id integer,
    read_at timestamp without time zone,
    created_at timestamp without time zone
);


ALTER TABLE public.notifications OWNER TO neondb_owner;

--
-- Name: notifications_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.notifications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.notifications_id_seq OWNER TO neondb_owner;

--
-- Name: notifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.notifications_id_seq OWNED BY public.notifications.id;


--
-- Name: opportunities; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.opportunities (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    company_id integer,
    company_name_raw character varying,
    demand_hypothesis_id integer NOT NULL,
    problem_hypothesis_id integer NOT NULL,
    affected_function character varying NOT NULL,
    opportunity_statement text NOT NULL,
    reasoning_note text,
    status character varying DEFAULT 'candidate'::character varying NOT NULL,
    confidence json,
    first_observed_at timestamp without time zone,
    last_updated_at timestamp without time zone,
    created_at timestamp without time zone
);


ALTER TABLE public.opportunities OWNER TO neondb_owner;

--
-- Name: opportunities_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.opportunities_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.opportunities_id_seq OWNER TO neondb_owner;

--
-- Name: opportunities_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.opportunities_id_seq OWNED BY public.opportunities.id;


--
-- Name: parameters; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.parameters (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    key character varying NOT NULL,
    value json NOT NULL,
    description text,
    updated_at timestamp without time zone
);


ALTER TABLE public.parameters OWNER TO neondb_owner;

--
-- Name: parameters_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.parameters_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.parameters_id_seq OWNER TO neondb_owner;

--
-- Name: parameters_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.parameters_id_seq OWNED BY public.parameters.id;


--
-- Name: partner_company_recommendations; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.partner_company_recommendations (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    profile_id integer NOT NULL,
    company_id integer NOT NULL,
    match_reasoning text,
    match_confidence character varying,
    status character varying DEFAULT 'proposed'::character varying,
    created_at timestamp without time zone,
    reviewed_at timestamp without time zone
);


ALTER TABLE public.partner_company_recommendations OWNER TO neondb_owner;

--
-- Name: partner_company_recommendations_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.partner_company_recommendations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.partner_company_recommendations_id_seq OWNER TO neondb_owner;

--
-- Name: partner_company_recommendations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.partner_company_recommendations_id_seq OWNED BY public.partner_company_recommendations.id;


--
-- Name: partner_recommendation_messages; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.partner_recommendation_messages (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    profile_id integer NOT NULL,
    recommendation_ids json NOT NULL,
    generated_message text,
    status character varying DEFAULT 'draft'::character varying,
    generated_at timestamp without time zone,
    reviewed_at timestamp without time zone,
    sent_at timestamp without time zone,
    send_channel character varying
);


ALTER TABLE public.partner_recommendation_messages OWNER TO neondb_owner;

--
-- Name: partner_recommendation_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.partner_recommendation_messages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.partner_recommendation_messages_id_seq OWNER TO neondb_owner;

--
-- Name: partner_recommendation_messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.partner_recommendation_messages_id_seq OWNED BY public.partner_recommendation_messages.id;


--
-- Name: personalized_messages; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.personalized_messages (
    id integer NOT NULL,
    contact_id integer NOT NULL,
    company_research json,
    contact_research json,
    fit_analysis json,
    generated_message text,
    status character varying DEFAULT 'draft'::character varying,
    error_message text,
    generated_at timestamp without time zone,
    email_subject character varying,
    email_body text
);


ALTER TABLE public.personalized_messages OWNER TO neondb_owner;

--
-- Name: personalized_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.personalized_messages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.personalized_messages_id_seq OWNER TO neondb_owner;

--
-- Name: personalized_messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.personalized_messages_id_seq OWNED BY public.personalized_messages.id;


--
-- Name: problem_hypotheses; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.problem_hypotheses (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    company_id integer,
    company_name_raw character varying,
    affected_function character varying NOT NULL,
    problem_statement text NOT NULL,
    reasoning_note text,
    confidence json,
    first_observed_at timestamp without time zone,
    last_updated_at timestamp without time zone,
    created_at timestamp without time zone,
    person_name_raw character varying
);


ALTER TABLE public.problem_hypotheses OWNER TO neondb_owner;

--
-- Name: problem_hypotheses_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.problem_hypotheses_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.problem_hypotheses_id_seq OWNER TO neondb_owner;

--
-- Name: problem_hypotheses_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.problem_hypotheses_id_seq OWNED BY public.problem_hypotheses.id;


--
-- Name: problem_hypothesis_evidence; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.problem_hypothesis_evidence (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    problem_hypothesis_id integer NOT NULL,
    interpreted_signal_id integer NOT NULL,
    role character varying NOT NULL,
    evidence_tier character varying NOT NULL,
    note text,
    added_at timestamp without time zone
);


ALTER TABLE public.problem_hypothesis_evidence OWNER TO neondb_owner;

--
-- Name: problem_hypothesis_evidence_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.problem_hypothesis_evidence_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.problem_hypothesis_evidence_id_seq OWNER TO neondb_owner;

--
-- Name: problem_hypothesis_evidence_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.problem_hypothesis_evidence_id_seq OWNED BY public.problem_hypothesis_evidence.id;


--
-- Name: reverse_discovery_candidates; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.reverse_discovery_candidates (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    post_urn character varying NOT NULL,
    post_url character varying,
    post_text text,
    matched_keyword character varying,
    author_name character varying,
    author_profile_url character varying,
    author_occupation character varying,
    guessed_company_name character varying,
    relevance_score integer,
    recommended_action character varying,
    classifier_reason text,
    icp_status character varying,
    icp_reasoning text,
    posted_at timestamp without time zone,
    created_at timestamp without time zone
);


ALTER TABLE public.reverse_discovery_candidates OWNER TO neondb_owner;

--
-- Name: reverse_discovery_candidates_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.reverse_discovery_candidates_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.reverse_discovery_candidates_id_seq OWNER TO neondb_owner;

--
-- Name: reverse_discovery_candidates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.reverse_discovery_candidates_id_seq OWNED BY public.reverse_discovery_candidates.id;


--
-- Name: review_comments; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.review_comments (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    review_date character varying NOT NULL,
    comment text NOT NULL,
    created_at timestamp without time zone
);


ALTER TABLE public.review_comments OWNER TO neondb_owner;

--
-- Name: review_comments_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.review_comments_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.review_comments_id_seq OWNER TO neondb_owner;

--
-- Name: review_comments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.review_comments_id_seq OWNED BY public.review_comments.id;


--
-- Name: sales_outcomes; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.sales_outcomes (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    interpreted_signal_id integer NOT NULL,
    gtm_signal_id integer NOT NULL,
    opportunity_id integer,
    contact_id integer,
    outcome_category character varying NOT NULL,
    source_event_type character varying NOT NULL,
    reasoning_note text,
    observed_at timestamp without time zone,
    created_at timestamp without time zone
);


ALTER TABLE public.sales_outcomes OWNER TO neondb_owner;

--
-- Name: sales_outcomes_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.sales_outcomes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.sales_outcomes_id_seq OWNER TO neondb_owner;

--
-- Name: sales_outcomes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.sales_outcomes_id_seq OWNED BY public.sales_outcomes.id;


--
-- Name: scores; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.scores (
    id integer NOT NULL,
    company_id integer NOT NULL,
    signal_strength double precision,
    icp_fit double precision,
    financial_growth double precision,
    compliance_complexity double precision,
    greenfield_legacy double precision,
    stacking_bonus double precision,
    total_score double precision,
    tier character varying,
    passed_industry_gate boolean,
    computed_at timestamp without time zone,
    breakdown json,
    decision_maker_match double precision DEFAULT 0
);


ALTER TABLE public.scores OWNER TO neondb_owner;

--
-- Name: scores_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.scores_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.scores_id_seq OWNER TO neondb_owner;

--
-- Name: scores_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.scores_id_seq OWNED BY public.scores.id;


--
-- Name: signals; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.signals (
    id integer NOT NULL,
    company_id integer NOT NULL,
    category character varying NOT NULL,
    signal_type character varying NOT NULL,
    detail text,
    fired_at timestamp without time zone,
    source character varying
);


ALTER TABLE public.signals OWNER TO neondb_owner;

--
-- Name: signals_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.signals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.signals_id_seq OWNER TO neondb_owner;

--
-- Name: signals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.signals_id_seq OWNED BY public.signals.id;


--
-- Name: tenants; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.tenants (
    id integer NOT NULL,
    name character varying NOT NULL,
    slug character varying NOT NULL,
    backend_url character varying,
    created_at timestamp without time zone
);


ALTER TABLE public.tenants OWNER TO neondb_owner;

--
-- Name: tenants_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.tenants_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tenants_id_seq OWNER TO neondb_owner;

--
-- Name: tenants_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.tenants_id_seq OWNED BY public.tenants.id;


--
-- Name: topic_candidates; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.topic_candidates (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    candidate_name character varying NOT NULL,
    normalized_name character varying NOT NULL,
    gtm_signal_id integer NOT NULL,
    evidence_excerpt text,
    extraction_method character varying NOT NULL,
    confidence character varying,
    created_at timestamp without time zone,
    cluster_key character varying,
    normalization_method character varying,
    normalization_reason text
);


ALTER TABLE public.topic_candidates OWNER TO neondb_owner;

--
-- Name: topic_candidates_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.topic_candidates_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.topic_candidates_id_seq OWNER TO neondb_owner;

--
-- Name: topic_candidates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.topic_candidates_id_seq OWNED BY public.topic_candidates.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: neondb_owner
--

CREATE TABLE public.users (
    id integer NOT NULL,
    email character varying NOT NULL,
    password_hash character varying NOT NULL,
    name character varying,
    created_at timestamp without time zone
);


ALTER TABLE public.users OWNER TO neondb_owner;

--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: neondb_owner
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.users_id_seq OWNER TO neondb_owner;

--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: neondb_owner
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: autonomous_runs id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.autonomous_runs ALTER COLUMN id SET DEFAULT nextval('public.autonomous_runs_id_seq'::regclass);


--
-- Name: batches id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.batches ALTER COLUMN id SET DEFAULT nextval('public.batches_id_seq'::regclass);


--
-- Name: calendar_bookings id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.calendar_bookings ALTER COLUMN id SET DEFAULT nextval('public.calendar_bookings_id_seq'::regclass);


--
-- Name: campaign_events id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.campaign_events ALTER COLUMN id SET DEFAULT nextval('public.campaign_events_id_seq'::regclass);


--
-- Name: campaign_pushes id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.campaign_pushes ALTER COLUMN id SET DEFAULT nextval('public.campaign_pushes_id_seq'::regclass);


--
-- Name: chat_conversations id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.chat_conversations ALTER COLUMN id SET DEFAULT nextval('public.chat_conversations_id_seq'::regclass);


--
-- Name: chat_messages id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.chat_messages ALTER COLUMN id SET DEFAULT nextval('public.chat_messages_id_seq'::regclass);


--
-- Name: companies id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.companies ALTER COLUMN id SET DEFAULT nextval('public.companies_id_seq'::regclass);


--
-- Name: confirmed_patterns id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.confirmed_patterns ALTER COLUMN id SET DEFAULT nextval('public.confirmed_patterns_id_seq'::regclass);


--
-- Name: contacts id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.contacts ALTER COLUMN id SET DEFAULT nextval('public.contacts_id_seq'::regclass);


--
-- Name: content_topic_evidence id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.content_topic_evidence ALTER COLUMN id SET DEFAULT nextval('public.content_topic_evidence_id_seq'::regclass);


--
-- Name: content_topics id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.content_topics ALTER COLUMN id SET DEFAULT nextval('public.content_topics_id_seq'::regclass);


--
-- Name: credentials id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.credentials ALTER COLUMN id SET DEFAULT nextval('public.credentials_id_seq'::regclass);


--
-- Name: daily_reviews id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.daily_reviews ALTER COLUMN id SET DEFAULT nextval('public.daily_reviews_id_seq'::regclass);


--
-- Name: demand_hypotheses id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.demand_hypotheses ALTER COLUMN id SET DEFAULT nextval('public.demand_hypotheses_id_seq'::regclass);


--
-- Name: demand_hypothesis_evidence id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.demand_hypothesis_evidence ALTER COLUMN id SET DEFAULT nextval('public.demand_hypothesis_evidence_id_seq'::regclass);


--
-- Name: efficiency_activity_events id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.efficiency_activity_events ALTER COLUMN id SET DEFAULT nextval('public.efficiency_activity_events_id_seq'::regclass);


--
-- Name: enrichments id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.enrichments ALTER COLUMN id SET DEFAULT nextval('public.enrichments_id_seq'::regclass);


--
-- Name: gtm_governance_snapshots id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.gtm_governance_snapshots ALTER COLUMN id SET DEFAULT nextval('public.gtm_governance_snapshots_id_seq'::regclass);


--
-- Name: gtm_intelligence_runs id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.gtm_intelligence_runs ALTER COLUMN id SET DEFAULT nextval('public.gtm_intelligence_runs_id_seq'::regclass);


--
-- Name: gtm_signals id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.gtm_signals ALTER COLUMN id SET DEFAULT nextval('public.gtm_signals_id_seq'::regclass);


--
-- Name: gtm_strategies id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.gtm_strategies ALTER COLUMN id SET DEFAULT nextval('public.gtm_strategies_id_seq'::regclass);


--
-- Name: human_knowledge id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.human_knowledge ALTER COLUMN id SET DEFAULT nextval('public.human_knowledge_id_seq'::regclass);


--
-- Name: icp_matches id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.icp_matches ALTER COLUMN id SET DEFAULT nextval('public.icp_matches_id_seq'::regclass);


--
-- Name: interpreted_signals id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.interpreted_signals ALTER COLUMN id SET DEFAULT nextval('public.interpreted_signals_id_seq'::regclass);


--
-- Name: linkedin_monitor_profiles id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.linkedin_monitor_profiles ALTER COLUMN id SET DEFAULT nextval('public.linkedin_monitor_profiles_id_seq'::regclass);


--
-- Name: linkedin_monitor_signals id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.linkedin_monitor_signals ALTER COLUMN id SET DEFAULT nextval('public.linkedin_monitor_signals_id_seq'::regclass);


--
-- Name: message_drafts id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.message_drafts ALTER COLUMN id SET DEFAULT nextval('public.message_drafts_id_seq'::regclass);


--
-- Name: notifications id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.notifications ALTER COLUMN id SET DEFAULT nextval('public.notifications_id_seq'::regclass);


--
-- Name: opportunities id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.opportunities ALTER COLUMN id SET DEFAULT nextval('public.opportunities_id_seq'::regclass);


--
-- Name: parameters id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.parameters ALTER COLUMN id SET DEFAULT nextval('public.parameters_id_seq'::regclass);


--
-- Name: partner_company_recommendations id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.partner_company_recommendations ALTER COLUMN id SET DEFAULT nextval('public.partner_company_recommendations_id_seq'::regclass);


--
-- Name: partner_recommendation_messages id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.partner_recommendation_messages ALTER COLUMN id SET DEFAULT nextval('public.partner_recommendation_messages_id_seq'::regclass);


--
-- Name: personalized_messages id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.personalized_messages ALTER COLUMN id SET DEFAULT nextval('public.personalized_messages_id_seq'::regclass);


--
-- Name: problem_hypotheses id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.problem_hypotheses ALTER COLUMN id SET DEFAULT nextval('public.problem_hypotheses_id_seq'::regclass);


--
-- Name: problem_hypothesis_evidence id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.problem_hypothesis_evidence ALTER COLUMN id SET DEFAULT nextval('public.problem_hypothesis_evidence_id_seq'::regclass);


--
-- Name: reverse_discovery_candidates id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.reverse_discovery_candidates ALTER COLUMN id SET DEFAULT nextval('public.reverse_discovery_candidates_id_seq'::regclass);


--
-- Name: review_comments id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.review_comments ALTER COLUMN id SET DEFAULT nextval('public.review_comments_id_seq'::regclass);


--
-- Name: sales_outcomes id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.sales_outcomes ALTER COLUMN id SET DEFAULT nextval('public.sales_outcomes_id_seq'::regclass);


--
-- Name: scores id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.scores ALTER COLUMN id SET DEFAULT nextval('public.scores_id_seq'::regclass);


--
-- Name: signals id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.signals ALTER COLUMN id SET DEFAULT nextval('public.signals_id_seq'::regclass);


--
-- Name: tenants id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.tenants ALTER COLUMN id SET DEFAULT nextval('public.tenants_id_seq'::regclass);


--
-- Name: topic_candidates id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.topic_candidates ALTER COLUMN id SET DEFAULT nextval('public.topic_candidates_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: autonomous_runs autonomous_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.autonomous_runs
    ADD CONSTRAINT autonomous_runs_pkey PRIMARY KEY (id);


--
-- Name: batches batches_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.batches
    ADD CONSTRAINT batches_pkey PRIMARY KEY (id);


--
-- Name: calendar_bookings calendar_bookings_google_event_id_key; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.calendar_bookings
    ADD CONSTRAINT calendar_bookings_google_event_id_key UNIQUE (google_event_id);


--
-- Name: calendar_bookings calendar_bookings_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.calendar_bookings
    ADD CONSTRAINT calendar_bookings_pkey PRIMARY KEY (id);


--
-- Name: campaign_events campaign_events_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.campaign_events
    ADD CONSTRAINT campaign_events_pkey PRIMARY KEY (id);


--
-- Name: campaign_pushes campaign_pushes_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.campaign_pushes
    ADD CONSTRAINT campaign_pushes_pkey PRIMARY KEY (id);


--
-- Name: chat_conversations chat_conversations_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.chat_conversations
    ADD CONSTRAINT chat_conversations_pkey PRIMARY KEY (id);


--
-- Name: chat_messages chat_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_pkey PRIMARY KEY (id);


--
-- Name: companies companies_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.companies
    ADD CONSTRAINT companies_pkey PRIMARY KEY (id);


--
-- Name: confirmed_patterns confirmed_patterns_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.confirmed_patterns
    ADD CONSTRAINT confirmed_patterns_pkey PRIMARY KEY (id);


--
-- Name: contacts contacts_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT contacts_pkey PRIMARY KEY (id);


--
-- Name: content_topic_evidence content_topic_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.content_topic_evidence
    ADD CONSTRAINT content_topic_evidence_pkey PRIMARY KEY (id);


--
-- Name: content_topics content_topics_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.content_topics
    ADD CONSTRAINT content_topics_pkey PRIMARY KEY (id);


--
-- Name: credentials credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.credentials
    ADD CONSTRAINT credentials_pkey PRIMARY KEY (id);


--
-- Name: daily_reviews daily_reviews_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.daily_reviews
    ADD CONSTRAINT daily_reviews_pkey PRIMARY KEY (id);


--
-- Name: demand_hypotheses demand_hypotheses_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.demand_hypotheses
    ADD CONSTRAINT demand_hypotheses_pkey PRIMARY KEY (id);


--
-- Name: demand_hypothesis_evidence demand_hypothesis_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.demand_hypothesis_evidence
    ADD CONSTRAINT demand_hypothesis_evidence_pkey PRIMARY KEY (id);


--
-- Name: efficiency_activity_events efficiency_activity_events_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.efficiency_activity_events
    ADD CONSTRAINT efficiency_activity_events_pkey PRIMARY KEY (id);


--
-- Name: enrichments enrichments_contact_id_key; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.enrichments
    ADD CONSTRAINT enrichments_contact_id_key UNIQUE (contact_id);


--
-- Name: enrichments enrichments_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.enrichments
    ADD CONSTRAINT enrichments_pkey PRIMARY KEY (id);


--
-- Name: gtm_governance_snapshots gtm_governance_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.gtm_governance_snapshots
    ADD CONSTRAINT gtm_governance_snapshots_pkey PRIMARY KEY (id);


--
-- Name: gtm_intelligence_runs gtm_intelligence_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.gtm_intelligence_runs
    ADD CONSTRAINT gtm_intelligence_runs_pkey PRIMARY KEY (id);


--
-- Name: gtm_signals gtm_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.gtm_signals
    ADD CONSTRAINT gtm_signals_pkey PRIMARY KEY (id);


--
-- Name: gtm_strategies gtm_strategies_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.gtm_strategies
    ADD CONSTRAINT gtm_strategies_pkey PRIMARY KEY (id);


--
-- Name: human_knowledge human_knowledge_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.human_knowledge
    ADD CONSTRAINT human_knowledge_pkey PRIMARY KEY (id);


--
-- Name: icp_matches icp_matches_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.icp_matches
    ADD CONSTRAINT icp_matches_pkey PRIMARY KEY (id);


--
-- Name: interpreted_signals interpreted_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.interpreted_signals
    ADD CONSTRAINT interpreted_signals_pkey PRIMARY KEY (id);


--
-- Name: linkedin_monitor_profiles linkedin_monitor_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.linkedin_monitor_profiles
    ADD CONSTRAINT linkedin_monitor_profiles_pkey PRIMARY KEY (id);


--
-- Name: linkedin_monitor_signals linkedin_monitor_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.linkedin_monitor_signals
    ADD CONSTRAINT linkedin_monitor_signals_pkey PRIMARY KEY (id);


--
-- Name: message_drafts message_drafts_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.message_drafts
    ADD CONSTRAINT message_drafts_pkey PRIMARY KEY (id);


--
-- Name: notifications notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);


--
-- Name: opportunities opportunities_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.opportunities
    ADD CONSTRAINT opportunities_pkey PRIMARY KEY (id);


--
-- Name: parameters parameters_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.parameters
    ADD CONSTRAINT parameters_pkey PRIMARY KEY (id);


--
-- Name: partner_company_recommendations partner_company_recommendations_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.partner_company_recommendations
    ADD CONSTRAINT partner_company_recommendations_pkey PRIMARY KEY (id);


--
-- Name: partner_recommendation_messages partner_recommendation_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.partner_recommendation_messages
    ADD CONSTRAINT partner_recommendation_messages_pkey PRIMARY KEY (id);


--
-- Name: personalized_messages personalized_messages_contact_id_key; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.personalized_messages
    ADD CONSTRAINT personalized_messages_contact_id_key UNIQUE (contact_id);


--
-- Name: personalized_messages personalized_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.personalized_messages
    ADD CONSTRAINT personalized_messages_pkey PRIMARY KEY (id);


--
-- Name: problem_hypotheses problem_hypotheses_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.problem_hypotheses
    ADD CONSTRAINT problem_hypotheses_pkey PRIMARY KEY (id);


--
-- Name: problem_hypothesis_evidence problem_hypothesis_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.problem_hypothesis_evidence
    ADD CONSTRAINT problem_hypothesis_evidence_pkey PRIMARY KEY (id);


--
-- Name: reverse_discovery_candidates reverse_discovery_candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.reverse_discovery_candidates
    ADD CONSTRAINT reverse_discovery_candidates_pkey PRIMARY KEY (id);


--
-- Name: review_comments review_comments_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.review_comments
    ADD CONSTRAINT review_comments_pkey PRIMARY KEY (id);


--
-- Name: sales_outcomes sales_outcomes_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.sales_outcomes
    ADD CONSTRAINT sales_outcomes_pkey PRIMARY KEY (id);


--
-- Name: scores scores_company_id_key; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.scores
    ADD CONSTRAINT scores_company_id_key UNIQUE (company_id);


--
-- Name: scores scores_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.scores
    ADD CONSTRAINT scores_pkey PRIMARY KEY (id);


--
-- Name: signals signals_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.signals
    ADD CONSTRAINT signals_pkey PRIMARY KEY (id);


--
-- Name: tenants tenants_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);


--
-- Name: tenants tenants_slug_key; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_slug_key UNIQUE (slug);


--
-- Name: topic_candidates topic_candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.topic_candidates
    ADD CONSTRAINT topic_candidates_pkey PRIMARY KEY (id);


--
-- Name: credentials uq_credential_tenant_name; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.credentials
    ADD CONSTRAINT uq_credential_tenant_name UNIQUE (tenant_id, name);


--
-- Name: parameters uq_parameter_tenant_key; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.parameters
    ADD CONSTRAINT uq_parameter_tenant_key UNIQUE (tenant_id, key);


--
-- Name: partner_company_recommendations uq_partner_company_reco; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.partner_company_recommendations
    ADD CONSTRAINT uq_partner_company_reco UNIQUE (profile_id, company_id);


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: ix_autonomous_runs_batch_id; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_autonomous_runs_batch_id ON public.autonomous_runs USING btree (batch_id);


--
-- Name: ix_batches_tenant_id; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_batches_tenant_id ON public.batches USING btree (tenant_id);


--
-- Name: ix_campaign_events_contact_id; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_campaign_events_contact_id ON public.campaign_events USING btree (contact_id);


--
-- Name: ix_campaign_pushes_contact_id; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_campaign_pushes_contact_id ON public.campaign_pushes USING btree (contact_id);


--
-- Name: ix_chat_conversations_tenant_updated; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_chat_conversations_tenant_updated ON public.chat_conversations USING btree (tenant_id, updated_at DESC);


--
-- Name: ix_chat_messages_conversation_created; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_chat_messages_conversation_created ON public.chat_messages USING btree (conversation_id, created_at);


--
-- Name: ix_companies_batch_id; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_companies_batch_id ON public.companies USING btree (batch_id);


--
-- Name: ix_confirmed_patterns_tenant_category; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_confirmed_patterns_tenant_category ON public.confirmed_patterns USING btree (tenant_id, category);


--
-- Name: ix_contacts_company_id; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_contacts_company_id ON public.contacts USING btree (company_id);


--
-- Name: ix_content_topic_evidence_signal; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_content_topic_evidence_signal ON public.content_topic_evidence USING btree (gtm_signal_id);


--
-- Name: ix_content_topic_evidence_topic_signal; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE UNIQUE INDEX ix_content_topic_evidence_topic_signal ON public.content_topic_evidence USING btree (content_topic_id, gtm_signal_id);


--
-- Name: ix_content_topics_tenant; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_content_topics_tenant ON public.content_topics USING btree (tenant_id);


--
-- Name: ix_daily_reviews_tenant_date; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE UNIQUE INDEX ix_daily_reviews_tenant_date ON public.daily_reviews USING btree (tenant_id, review_date);


--
-- Name: ix_demand_hypotheses_company; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_demand_hypotheses_company ON public.demand_hypotheses USING btree (tenant_id, company_id);


--
-- Name: ix_demand_hypotheses_problem_hypothesis; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_demand_hypotheses_problem_hypothesis ON public.demand_hypotheses USING btree (problem_hypothesis_id);


--
-- Name: ix_demand_hypothesis_evidence_hypothesis; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_demand_hypothesis_evidence_hypothesis ON public.demand_hypothesis_evidence USING btree (demand_hypothesis_id);


--
-- Name: ix_demand_hypothesis_evidence_interpreted_signal; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_demand_hypothesis_evidence_interpreted_signal ON public.demand_hypothesis_evidence USING btree (interpreted_signal_id);


--
-- Name: ix_efficiency_activity_tenant_type_date; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_efficiency_activity_tenant_type_date ON public.efficiency_activity_events USING btree (tenant_id, activity_type, activity_date);


--
-- Name: ix_gtm_governance_snapshots_tenant_computed; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_gtm_governance_snapshots_tenant_computed ON public.gtm_governance_snapshots USING btree (tenant_id, computed_at DESC);


--
-- Name: ix_gtm_intelligence_runs_tenant_started; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_gtm_intelligence_runs_tenant_started ON public.gtm_intelligence_runs USING btree (tenant_id, started_at);


--
-- Name: ix_gtm_signals_dedup_key; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_gtm_signals_dedup_key ON public.gtm_signals USING btree (dedup_key);


--
-- Name: ix_gtm_signals_tenant_source; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_gtm_signals_tenant_source ON public.gtm_signals USING btree (tenant_id, source);


--
-- Name: ix_gtm_strategies_tenant_opportunity; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_gtm_strategies_tenant_opportunity ON public.gtm_strategies USING btree (tenant_id, opportunity_id);


--
-- Name: ix_human_knowledge_tenant_status; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_human_knowledge_tenant_status ON public.human_knowledge USING btree (tenant_id, status);


--
-- Name: ix_icp_matches_tenant_company_icp; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE UNIQUE INDEX ix_icp_matches_tenant_company_icp ON public.icp_matches USING btree (tenant_id, company_id, icp_id);


--
-- Name: ix_icp_matches_tenant_icp; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_icp_matches_tenant_icp ON public.icp_matches USING btree (tenant_id, icp_id);


--
-- Name: ix_interpreted_signals_source_signal_id; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_interpreted_signals_source_signal_id ON public.interpreted_signals USING btree (source_signal_id);


--
-- Name: ix_interpreted_signals_tenant; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_interpreted_signals_tenant ON public.interpreted_signals USING btree (tenant_id);


--
-- Name: ix_linkedin_monitor_profiles_tenant_url; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE UNIQUE INDEX ix_linkedin_monitor_profiles_tenant_url ON public.linkedin_monitor_profiles USING btree (tenant_id, linkedin_url);


--
-- Name: ix_linkedin_monitor_signals_profile_urn; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE UNIQUE INDEX ix_linkedin_monitor_signals_profile_urn ON public.linkedin_monitor_signals USING btree (profile_id, post_urn);


--
-- Name: ix_message_drafts_opportunity_strategy; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE UNIQUE INDEX ix_message_drafts_opportunity_strategy ON public.message_drafts USING btree (opportunity_id, gtm_strategy_id);


--
-- Name: ix_message_drafts_tenant_status; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_message_drafts_tenant_status ON public.message_drafts USING btree (tenant_id, status);


--
-- Name: ix_notifications_tenant_created; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_notifications_tenant_created ON public.notifications USING btree (tenant_id, created_at DESC);


--
-- Name: ix_opportunities_demand_hypothesis; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE UNIQUE INDEX ix_opportunities_demand_hypothesis ON public.opportunities USING btree (demand_hypothesis_id);


--
-- Name: ix_opportunities_tenant_company; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_opportunities_tenant_company ON public.opportunities USING btree (tenant_id, company_id);


--
-- Name: ix_opportunities_tenant_status; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_opportunities_tenant_status ON public.opportunities USING btree (tenant_id, status);


--
-- Name: ix_problem_hypotheses_company_function; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_problem_hypotheses_company_function ON public.problem_hypotheses USING btree (tenant_id, company_id, affected_function);


--
-- Name: ix_problem_hypotheses_company_unique; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE UNIQUE INDEX ix_problem_hypotheses_company_unique ON public.problem_hypotheses USING btree (tenant_id, affected_function, company_id) WHERE (company_id IS NOT NULL);


--
-- Name: ix_problem_hypotheses_person_unique; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE UNIQUE INDEX ix_problem_hypotheses_person_unique ON public.problem_hypotheses USING btree (tenant_id, affected_function, person_name_raw) WHERE ((company_id IS NULL) AND (person_name_raw IS NOT NULL));


--
-- Name: ix_problem_hypothesis_evidence_hypothesis; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_problem_hypothesis_evidence_hypothesis ON public.problem_hypothesis_evidence USING btree (problem_hypothesis_id);


--
-- Name: ix_problem_hypothesis_evidence_interpreted_signal; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_problem_hypothesis_evidence_interpreted_signal ON public.problem_hypothesis_evidence USING btree (interpreted_signal_id);


--
-- Name: ix_problem_hypothesis_evidence_signal_unique; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE UNIQUE INDEX ix_problem_hypothesis_evidence_signal_unique ON public.problem_hypothesis_evidence USING btree (interpreted_signal_id);


--
-- Name: ix_reverse_discovery_candidates_tenant_urn; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE UNIQUE INDEX ix_reverse_discovery_candidates_tenant_urn ON public.reverse_discovery_candidates USING btree (tenant_id, post_urn);


--
-- Name: ix_review_comments_tenant_date; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_review_comments_tenant_date ON public.review_comments USING btree (tenant_id, review_date);


--
-- Name: ix_sales_outcomes_interpreted_signal; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE UNIQUE INDEX ix_sales_outcomes_interpreted_signal ON public.sales_outcomes USING btree (interpreted_signal_id);


--
-- Name: ix_sales_outcomes_tenant_opportunity; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_sales_outcomes_tenant_opportunity ON public.sales_outcomes USING btree (tenant_id, opportunity_id);


--
-- Name: ix_signals_company_id; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_signals_company_id ON public.signals USING btree (company_id);


--
-- Name: ix_topic_candidates_signal; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_topic_candidates_signal ON public.topic_candidates USING btree (gtm_signal_id);


--
-- Name: ix_topic_candidates_tenant_cluster; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_topic_candidates_tenant_cluster ON public.topic_candidates USING btree (tenant_id, cluster_key);


--
-- Name: ix_topic_candidates_tenant_normalized; Type: INDEX; Schema: public; Owner: neondb_owner
--

CREATE INDEX ix_topic_candidates_tenant_normalized ON public.topic_candidates USING btree (tenant_id, normalized_name);


--
-- Name: autonomous_runs autonomous_runs_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.autonomous_runs
    ADD CONSTRAINT autonomous_runs_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.batches(id);


--
-- Name: batches batches_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.batches
    ADD CONSTRAINT batches_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: calendar_bookings calendar_bookings_outcome_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.calendar_bookings
    ADD CONSTRAINT calendar_bookings_outcome_company_id_fkey FOREIGN KEY (outcome_company_id) REFERENCES public.companies(id);


--
-- Name: calendar_bookings calendar_bookings_outcome_opportunity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.calendar_bookings
    ADD CONSTRAINT calendar_bookings_outcome_opportunity_id_fkey FOREIGN KEY (outcome_opportunity_id) REFERENCES public.opportunities(id);


--
-- Name: campaign_events campaign_events_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.campaign_events
    ADD CONSTRAINT campaign_events_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.contacts(id);


--
-- Name: campaign_pushes campaign_pushes_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.campaign_pushes
    ADD CONSTRAINT campaign_pushes_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.contacts(id);


--
-- Name: chat_messages chat_messages_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.chat_conversations(id);


--
-- Name: companies companies_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.companies
    ADD CONSTRAINT companies_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.batches(id);


--
-- Name: contacts contacts_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT contacts_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: content_topic_evidence content_topic_evidence_content_topic_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.content_topic_evidence
    ADD CONSTRAINT content_topic_evidence_content_topic_id_fkey FOREIGN KEY (content_topic_id) REFERENCES public.content_topics(id);


--
-- Name: content_topic_evidence content_topic_evidence_gtm_signal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.content_topic_evidence
    ADD CONSTRAINT content_topic_evidence_gtm_signal_id_fkey FOREIGN KEY (gtm_signal_id) REFERENCES public.gtm_signals(id);


--
-- Name: credentials credentials_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.credentials
    ADD CONSTRAINT credentials_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: demand_hypotheses demand_hypotheses_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.demand_hypotheses
    ADD CONSTRAINT demand_hypotheses_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: demand_hypotheses demand_hypotheses_problem_hypothesis_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.demand_hypotheses
    ADD CONSTRAINT demand_hypotheses_problem_hypothesis_id_fkey FOREIGN KEY (problem_hypothesis_id) REFERENCES public.problem_hypotheses(id);


--
-- Name: demand_hypothesis_evidence demand_hypothesis_evidence_demand_hypothesis_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.demand_hypothesis_evidence
    ADD CONSTRAINT demand_hypothesis_evidence_demand_hypothesis_id_fkey FOREIGN KEY (demand_hypothesis_id) REFERENCES public.demand_hypotheses(id);


--
-- Name: demand_hypothesis_evidence demand_hypothesis_evidence_interpreted_signal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.demand_hypothesis_evidence
    ADD CONSTRAINT demand_hypothesis_evidence_interpreted_signal_id_fkey FOREIGN KEY (interpreted_signal_id) REFERENCES public.interpreted_signals(id);


--
-- Name: efficiency_activity_events efficiency_activity_events_source_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.efficiency_activity_events
    ADD CONSTRAINT efficiency_activity_events_source_run_id_fkey FOREIGN KEY (source_run_id) REFERENCES public.autonomous_runs(id);


--
-- Name: enrichments enrichments_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.enrichments
    ADD CONSTRAINT enrichments_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.contacts(id);


--
-- Name: gtm_signals gtm_signals_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.gtm_signals
    ADD CONSTRAINT gtm_signals_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: gtm_signals gtm_signals_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.gtm_signals
    ADD CONSTRAINT gtm_signals_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.contacts(id);


--
-- Name: gtm_strategies gtm_strategies_opportunity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.gtm_strategies
    ADD CONSTRAINT gtm_strategies_opportunity_id_fkey FOREIGN KEY (opportunity_id) REFERENCES public.opportunities(id);


--
-- Name: icp_matches icp_matches_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.icp_matches
    ADD CONSTRAINT icp_matches_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: interpreted_signals interpreted_signals_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.interpreted_signals
    ADD CONSTRAINT interpreted_signals_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: interpreted_signals interpreted_signals_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.interpreted_signals
    ADD CONSTRAINT interpreted_signals_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.contacts(id);


--
-- Name: interpreted_signals interpreted_signals_source_signal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.interpreted_signals
    ADD CONSTRAINT interpreted_signals_source_signal_id_fkey FOREIGN KEY (source_signal_id) REFERENCES public.gtm_signals(id);


--
-- Name: linkedin_monitor_signals linkedin_monitor_signals_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.linkedin_monitor_signals
    ADD CONSTRAINT linkedin_monitor_signals_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.linkedin_monitor_profiles(id);


--
-- Name: message_drafts message_drafts_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.message_drafts
    ADD CONSTRAINT message_drafts_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.contacts(id);


--
-- Name: message_drafts message_drafts_gtm_strategy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.message_drafts
    ADD CONSTRAINT message_drafts_gtm_strategy_id_fkey FOREIGN KEY (gtm_strategy_id) REFERENCES public.gtm_strategies(id);


--
-- Name: message_drafts message_drafts_opportunity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.message_drafts
    ADD CONSTRAINT message_drafts_opportunity_id_fkey FOREIGN KEY (opportunity_id) REFERENCES public.opportunities(id);


--
-- Name: opportunities opportunities_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.opportunities
    ADD CONSTRAINT opportunities_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: opportunities opportunities_demand_hypothesis_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.opportunities
    ADD CONSTRAINT opportunities_demand_hypothesis_id_fkey FOREIGN KEY (demand_hypothesis_id) REFERENCES public.demand_hypotheses(id);


--
-- Name: opportunities opportunities_problem_hypothesis_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.opportunities
    ADD CONSTRAINT opportunities_problem_hypothesis_id_fkey FOREIGN KEY (problem_hypothesis_id) REFERENCES public.problem_hypotheses(id);


--
-- Name: parameters parameters_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.parameters
    ADD CONSTRAINT parameters_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: partner_company_recommendations partner_company_recommendations_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.partner_company_recommendations
    ADD CONSTRAINT partner_company_recommendations_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: partner_company_recommendations partner_company_recommendations_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.partner_company_recommendations
    ADD CONSTRAINT partner_company_recommendations_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.linkedin_monitor_profiles(id);


--
-- Name: partner_recommendation_messages partner_recommendation_messages_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.partner_recommendation_messages
    ADD CONSTRAINT partner_recommendation_messages_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.linkedin_monitor_profiles(id);


--
-- Name: personalized_messages personalized_messages_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.personalized_messages
    ADD CONSTRAINT personalized_messages_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.contacts(id);


--
-- Name: problem_hypotheses problem_hypotheses_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.problem_hypotheses
    ADD CONSTRAINT problem_hypotheses_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: problem_hypothesis_evidence problem_hypothesis_evidence_interpreted_signal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.problem_hypothesis_evidence
    ADD CONSTRAINT problem_hypothesis_evidence_interpreted_signal_id_fkey FOREIGN KEY (interpreted_signal_id) REFERENCES public.interpreted_signals(id);


--
-- Name: problem_hypothesis_evidence problem_hypothesis_evidence_problem_hypothesis_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.problem_hypothesis_evidence
    ADD CONSTRAINT problem_hypothesis_evidence_problem_hypothesis_id_fkey FOREIGN KEY (problem_hypothesis_id) REFERENCES public.problem_hypotheses(id);


--
-- Name: sales_outcomes sales_outcomes_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.sales_outcomes
    ADD CONSTRAINT sales_outcomes_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.contacts(id);


--
-- Name: sales_outcomes sales_outcomes_gtm_signal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.sales_outcomes
    ADD CONSTRAINT sales_outcomes_gtm_signal_id_fkey FOREIGN KEY (gtm_signal_id) REFERENCES public.gtm_signals(id);


--
-- Name: sales_outcomes sales_outcomes_interpreted_signal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.sales_outcomes
    ADD CONSTRAINT sales_outcomes_interpreted_signal_id_fkey FOREIGN KEY (interpreted_signal_id) REFERENCES public.interpreted_signals(id);


--
-- Name: sales_outcomes sales_outcomes_opportunity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.sales_outcomes
    ADD CONSTRAINT sales_outcomes_opportunity_id_fkey FOREIGN KEY (opportunity_id) REFERENCES public.opportunities(id);


--
-- Name: scores scores_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.scores
    ADD CONSTRAINT scores_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: signals signals_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.signals
    ADD CONSTRAINT signals_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: topic_candidates topic_candidates_gtm_signal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: neondb_owner
--

ALTER TABLE ONLY public.topic_candidates
    ADD CONSTRAINT topic_candidates_gtm_signal_id_fkey FOREIGN KEY (gtm_signal_id) REFERENCES public.gtm_signals(id);


--
-- PostgreSQL database dump complete
--

\unrestrict UbbHFW6wHQIbHYQM103DhG8vcwSLL8VNhqwuFd2baO8bdIKHT8Ot1u4SYsI0j7x

