"""The GTM-OS intelligence orchestrator -- Step 13A, extended by Batch 2 (Step 16 Batch 2 Part
A/B). Coordinates existing, unmodified sensing/interpretation/detection/content-intelligence
functions; contains zero intelligence logic of its own -- every stage below is a plain call into
an already-built, already-tested sweep function from elsewhere in app/gtm_os.

Two independent branches, both rooted in the same raw GtmSignal sensing stage, per Batch 2's own
architecture diagram -- neither branch reads the other's output:

    SOURCE SENSING (sense_linkedin_jobs / sense_linkedin_replies / sense_hackernews_stories /
                     sense_rss_articles / sense_linkedin_post_search -- independent per source,
                     own failure boundary each)
              │
              ├──▶ INTERPRETATION (run_interpretation_sweep, ALL_INTERPRETED_SOURCES)
              │        │
              │        ▼
              │    PROBLEM HYPOTHESES (run_problem_hypothesis_sweep)
              │        │
              │        ▼
              │    DEMAND HYPOTHESES (run_demand_hypothesis_sweep)
              │
              └──▶ CONFIGURED TOPIC LINKING (run_content_topic_linking_sweep)
                       │
                       ▼
                   CANDIDATE EXTRACTION (run_candidate_extraction_sweep -- candidate-worthy
                       │                  filter + LLM extraction, both already gated internally)
                       ▼
                   CANDIDATE NORMALIZATION (run_candidate_normalization_sweep -- deterministic +
                       │                     bounded LLM comparison, both already gated internally)
                       ▼
                   CANDIDATE PROMOTION (run_candidate_promotion_sweep)
                       │
                       ▼
                   TREND INTELLIGENCE (run_trend_intelligence_sweep -- read-only, no LLM)

A third branch (ACCOUNT_STRATEGY_STAGES), independently failure-isolated from both branches above,
runs after Problem/Demand (it reads DemandHypothesis):

    OPPORTUNITY (run_opportunity_intelligence_sweep)
        │
        ▼
    ICP MATCHING (run_icp_matching_sweep -- persists ICPMatch; previously had ZERO callers
        │          anywhere in this app until this wiring pass, see its own inline comment)
        ▼
    GTM STRATEGY (run_gtm_strategy_sweep -- internally calls match_offerings() per opportunity,
        │          so offering-fit is already evaluated as part of this stage, not a separate one)
        ▼
    MESSAGE GENERATION (run_message_generation_sweep -- previously had ZERO callers anywhere;
        │                the ONE stage here that spends real LLM $, bounded by its own small
        │                limit. Produces MessageDraft rows in draft/ready_for_review/
        │                insufficient_context state ONLY -- NEVER approved/sent/executed by
        │                this sweep or anything it calls. approve_message_draft() is a separate,
        │                human-only action (POST /gtm-os/messages/{id}/review). This is the
        │                autonomous cycle's human-approval boundary.)
        ▼
    SALES READINESS (run_sales_agent_sweep -- read-only reporting, writes nothing)
        │
        ▼
    OUTCOME DETECTION (run_outcome_detection_sweep)

GTM Motion recommendation, Account Brief, and Governance evaluation are DELIBERATELY NOT separate
scheduled stages here -- each is a pure, cheap, read-only computation with NO persistence table of
its own (recommend_gtm_motion()/build_account_brief()/evaluate_gtm_governance() all write nothing,
by design, per their own module docstrings). There is nothing for a batch stage to persist; they
are correctly computed fresh whenever a human opens the relevant dashboard page (governance.py's
own callers) rather than duplicated into a stale snapshot table. This is an intentional,
already-made architecture decision, not a gap -- see the accompanying audit report for the
reasoning in full.

Content Intelligence stages never consult InterpretedSignal/ProblemHypothesis/DemandHypothesis,
and Problem/Demand stages never consult ContentTopic/TopicCandidate -- both branches only ever
read GtmSignal (or their own branch's prior stage output), by construction, matching Batch 2's
explicit "do not make either branch depend on the other" requirement.

sense_linkedin_jobs()/sense_linkedin_posts() are deliberately NOT in this sweep -- neither
has an approved/configured source for its required input yet (search criteria; a production
profile-URL list) and inventing one here would silently make the sweep do nothing useful for
those two sources rather than being honest that they need a real decision first. sense_web_search()/
sense_company_website() are single-item/on-demand tools, not sweep sources at all (Step 8's own
conclusion) -- they were never candidates for inclusion.

**Real-money note (Batch 2)**: `candidate_extraction` calls a real LLM (app.llm_client, Gemini-
first/Claude-fallback) for every candidate-worthy unmatched signal. Wiring this sweep into a
recurring scheduled job (see app/main.py) means that cost now recurs automatically, not just on
manual invocation -- worth knowing before tightening the schedule interval.

**Explicit, current interpreted-source list, used for every sweep call below -- deliberately NOT
relying on run_interpretation_sweep()/run_problem_hypothesis_sweep()/run_demand_hypothesis_sweep()'s
own default `sources` arguments.** Direct inspection (Step 13 design doc §1) found both detection
sweeps' defaults are stale: run_problem_hypothesis_sweep defaults to only
["linkedin_job", "theirstack_job"], and run_demand_hypothesis_sweep defaults to
["linkedin_post", "linkedin_job", "theirstack_job"] -- neither includes "linkedin_reply" (added
Step 11B). Those functions are NOT modified here (out of scope); this orchestrator simply never
relies on either stale default.

**Failure isolation**: each sensing source has its own try/except -- one source failing (or being
unconfigured) never prevents another source, or any downstream stage, from running. Each of the
three sweep stages (interpretation/problem/demand) also has its own try/except -- a failure in an
earlier stage does not skip a later one, since every stage only ever operates on already-durably-
committed rows from the stage before it (nothing is lost by continuing). This is deliberately NOT
one blanket try/except around the whole function (see the module docstring's own reasoning, and
Step 13 design doc §6, for why that shape -- correct for run_daily_autonomous_cycle's sequential,
dependent phases -- is the wrong shape here, where sources and stages are independent).

Exceptions are caught broadly (`except Exception`) per source/stage, matching the existing
precedent already used elsewhere in this codebase for the same reason (see
autonomous_orchestrator.py's `_generate_messages_for_batch`: "one contact's failure must never
block the rest"). This includes infrastructure-level failures (e.g. a lost DB connection) --
deliberately not distinguished from an ordinary API error, since doing so would require importing
and special-casing SQLAlchemy's own exception hierarchy for uncertain benefit. Nothing is
silently swallowed either way: every failure is captured in the returned result dict AND logged
at ERROR level, so a fatal infrastructure issue is fully visible to any caller, just represented
as structured failure data rather than a raised exception out of this function."""

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Column, DateTime, Integer, JSON, String, func
from sqlalchemy.orm import Session

from app.db.models import Base, Parameter
from app.gtm_os.content.candidate_extraction import run_candidate_extraction_sweep
from app.gtm_os.content.candidate_normalization import run_candidate_normalization_sweep
from app.gtm_os.content.content_opportunity import run_content_opportunity_generation_sweep
from app.gtm_os.content.promotion import run_candidate_promotion_sweep
from app.gtm_os.content.topic_linking import run_content_topic_linking_sweep
from app.gtm_os.content.trend_intelligence import run_trend_intelligence_sweep
from app.gtm_os.icp.icp_matching import run_icp_matching_sweep
from app.gtm_os.icp.revenue_estimation import run_revenue_backfill_sweep
from app.gtm_os.intelligence.demand_detection import run_demand_hypothesis_sweep
from app.gtm_os.intelligence.interpretation import run_interpretation_sweep
from app.gtm_os.intelligence.investigation_cycle import run_investigation_cycle
from app.gtm_os.intelligence.problem_detection import run_problem_hypothesis_sweep
from app.gtm_os.intelligence.sensing import (
    sense_competitor_content,
    sense_hackernews_stories,
    sense_linkedin_jobs,
    sense_linkedin_post_search,
    sense_linkedin_replies,
    sense_rss_articles,
    sense_web_search_trends,
    sense_website_visitors,
)
from app.gtm_os.learning.message_draft import MessageDraft, run_message_generation_sweep
from app.gtm_os.learning.outcome import run_outcome_detection_sweep
from app.gtm_os.opportunity.opportunity import run_opportunity_intelligence_sweep
from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run, get_control_config
from app.gtm_os.orchestration.discovery import run_v2_discovery_if_due
from app.gtm_os.sales.contact_discovery import run_v2_contact_discovery_sweep
from app.gtm_os.sales.outreach_sequencing import run_v2_outreach_sequencing_sweep
from app.gtm_os.sales.sales_agent import run_sales_agent_sweep
from app.gtm_os.send.send import run_v2_send_sweep
from app.gtm_os.strategy.strategy import run_gtm_strategy_sweep

logger = logging.getLogger(__name__)


class GtmIntelligenceRun(Base):
    """One row per run_gtm_intelligence_sweep() invocation -- the durable run-state V1's
    AutonomousRun already provides for its own daily cycle, mirrored here rather than left as
    log-only output (the sweep previously only logged its result dict, with nothing queryable
    afterward -- no dashboard-visible run history, no way to answer "did last night's run
    actually complete," no stale-run detection). Deliberately its OWN table, not a reuse of
    AutonomousRun itself: AutonomousRun's columns (companies_discovered, contacts_found, budget
    fields, awaiting_approval_until, ...) are shaped for V1's sequential discovery/decision-maker/
    outreach phases and don't correspond to this sweep's independent-branch stage list -- reusing
    it would mean bolting on unrelated columns or leaving most of them permanently null. Same
    "own table when the shape genuinely differs" precedent as GtmStrategy vs. the Score pipeline's
    own tables."""
    __tablename__ = "gtm_intelligence_runs"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    status = Column(String, nullable=False, default="running")  # "running" | "completed" | "partial" | "failed"
    stage_results = Column(JSON, nullable=True)  # the full result dict run_gtm_intelligence_sweep() returns
    error_summary = Column(String, nullable=True)  # short, human-readable summary when status != "completed"

    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


def start_gtm_intelligence_run(db: Session, tenant_id: int) -> GtmIntelligenceRun:
    run = GtmIntelligenceRun(tenant_id=tenant_id, status="running")
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _concise_error(error: str, max_len: int = 160) -> str:
    """Best-effort SHORT excerpt of a raw error string, for a run's error_summary -- never
    raises, always falls back to plain truncation. Real provider errors (e.g. Deepline's
    theirstack_job_search failure) are often a multi-KB JSON blob; a human-readable "message"
    field, when present, is far more useful here than the raw blob's own opening text."""
    match = re.search(r'"message":\s*"((?:[^"\\]|\\.)*)"', error)
    text = match.group(1) if match else error
    text = " ".join(text.split())  # collapse embedded newlines/whitespace
    return text if len(text) <= max_len else text[:max_len].rstrip() + "..."


def _collect_failure_summaries(result: dict) -> list[str]:
    """Real, human-readable one-liners for every genuine "failed" condition in a sweep result --
    both top-level stage failures (result[stage].status == "failed", the original, unchanged
    detection) AND nested per-source failures (result["sources"][name].status == "failed") --
    e.g. sources.theirstack_job failing on a real Deepline credit error, which the original
    top-level-only scan never saw at all.

    Deliberately narrow: only ever looks at status == "failed". Every other real, non-error
    state a stage or source can be in today -- "skipped", "blocked_by_budget",
    "configuration_required", or a "succeeded" result with a zero/no-data count -- is left
    completely alone; none of those are errors, and this never reclassifies them as one."""
    summaries = []
    for key, value in result.items():
        if key == "sources" or not isinstance(value, dict):
            continue
        if value.get("status") == "failed":
            label = key.replace("_", " ").capitalize()
            error = value.get("error")
            summaries.append(f"{label} failed: {_concise_error(error)}" if error else f"{label} failed")

    sources = result.get("sources")
    if isinstance(sources, dict):
        for name, value in sources.items():
            if isinstance(value, dict) and value.get("status") == "failed":
                label = name.replace("_", " ").capitalize()
                error = value.get("error")
                summaries.append(f"{label} failed: {_concise_error(error)}" if error else f"{label} failed")

    return summaries


def finish_gtm_intelligence_run(db: Session, run: GtmIntelligenceRun, result: dict) -> GtmIntelligenceRun:
    failure_summaries = _collect_failure_summaries(result)
    run.status = result.get("status", "completed")
    run.stage_results = result
    run.error_summary = "; ".join(failure_summaries) if failure_summaries else None
    run.completed_at = datetime.utcnow()
    db.commit()
    return run


def recover_stale_gtm_intelligence_runs(db: Session, tenant_id: int, stale_after_minutes: int = 120) -> int:
    """Mirrors V1's _clear_stale_running_flags() concurrency-safety pattern (autonomous_orchestrator.py):
    a run that's been "running" for longer than any real sweep could plausibly take (a crashed
    process, an unhandled exception before finish_gtm_intelligence_run() could be called) must not
    stay "running" forever and must not silently block a real concurrency check from ever
    proceeding again. Marked "failed", never silently deleted -- the record itself is real
    evidence something went wrong."""
    from datetime import timedelta

    cutoff = datetime.utcnow() - timedelta(minutes=stale_after_minutes)
    stale = (
        db.query(GtmIntelligenceRun)
        .filter(GtmIntelligenceRun.tenant_id == tenant_id, GtmIntelligenceRun.status == "running", GtmIntelligenceRun.started_at < cutoff)
        .all()
    )
    for run in stale:
        run.status = "failed"
        run.error_summary = f"run exceeded {stale_after_minutes} minutes without completing -- marked failed by stale-run recovery"
        run.completed_at = datetime.utcnow()
    if stale:
        db.commit()
    return len(stale)


def _last_outbound_cycle_at(db: Session, tenant_id: int) -> datetime | None:
    """Anchor for the outbound cadence gate (Phase S7) -- derived from existing
    GtmIntelligenceRun.stage_results rather than a new table/column/timestamp, per the "reuse the
    existing run entity, do not invent another run-history mechanism" instruction. gtm_strategy's
    own runner never itself returns status=="skipped" (see its own docstring: "safe to run every
    cycle even with near-zero real data" -- it always succeeds/fails, never self-skips), so a
    "skipped" gtm_strategy entry in a past run can only mean THIS cadence gate produced it --
    making "the most recent run where gtm_strategy is present and not 'skipped'" an exact,
    non-invented anchor for "the last time outbound genuinely ran"."""
    runs = (
        db.query(GtmIntelligenceRun)
        .filter(GtmIntelligenceRun.tenant_id == tenant_id)
        .order_by(GtmIntelligenceRun.started_at.desc())
        .limit(50)
        .all()
    )
    for run in runs:
        stage_result = (run.stage_results or {}).get("gtm_strategy") or {}
        if stage_result.get("status") not in (None, "skipped"):
            return run.started_at
    return None


def is_outbound_cycle_due(db: Session, tenant_id: int, now: datetime | None = None) -> tuple[bool, str]:
    """Returns (due, reason). Never due while outbound.cadence_hours is unconfigured -- same
    "None never means unlimited/immediate" discipline as discovery.is_discovery_due(). Gates only
    the Strategy/Contact-discovery/Message-generation/Send/outreach-sequencing stages -- Opportunity
    and ICP matching are deliberately NOT gated by this (see control.py's own comment on why)."""
    config = get_control_config(db, tenant_id)
    cadence_hours = (config.get("outbound") or {}).get("cadence_hours")
    if not cadence_hours:
        return False, "outbound.cadence_hours not configured"

    last_at = _last_outbound_cycle_at(db, tenant_id)
    if last_at is None:
        return True, "no prior outbound cycle"

    now = now or datetime.utcnow()
    elapsed_hours = (now - last_at).total_seconds() / 3600
    if elapsed_hours < cadence_hours:
        return False, f"last outbound cycle {elapsed_hours:.1f}h ago, cadence is {cadence_hours}h"
    return True, f"last outbound cycle {elapsed_hours:.1f}h ago, cadence is {cadence_hours}h -- due"

# Content Intelligence sweep stages, run in this exact order after sensing -- each stage only
# ever consumes the previous content-branch stage's output (or raw GtmSignal for the first one),
# never InterpretedSignal/ProblemHypothesis/DemandHypothesis. (stage_key, runner(db, tenant_id) -> dict).
CONTENT_INTELLIGENCE_STAGES: list[tuple[str, callable]] = [
    ("topic_linking", lambda db, tenant_id: run_content_topic_linking_sweep(db, tenant_id)),
    ("candidate_extraction", lambda db, tenant_id: run_candidate_extraction_sweep(db, tenant_id)),
    ("candidate_normalization", lambda db, tenant_id: run_candidate_normalization_sweep(db, tenant_id)),
    ("candidate_promotion", lambda db, tenant_id: run_candidate_promotion_sweep(db, tenant_id)),
    ("trend_intelligence", lambda db, tenant_id: run_trend_intelligence_sweep(db, tenant_id)),
    ("content_opportunity_generation", lambda db, tenant_id: run_content_opportunity_generation_sweep(db, tenant_id)),
]

# Account/Strategy/Sales branch (Batch 6 Part L) -- runs AFTER problem_detection/demand_detection
# above (it reads DemandHypothesis, which that stage produces), but is still independently
# failure-isolated: a failure here never touches the Problem/Demand or Content Intelligence
# branches, and vice versa. Every stage here is a pure/idempotent read-or-additive-insert sweep
# (see opportunity.py/strategy.py/sales_agent.py's own docstrings) -- zero LLM calls, zero
# external API calls, zero CRM/outbound writes, safe to run on every cycle even with near-zero
# real data (each stage safely returns all-zero counts rather than fabricating output).
ACCOUNT_STRATEGY_STAGES_PRE_CONTACT: list[tuple[str, callable]] = [
    ("opportunity", lambda db, tenant_id: run_opportunity_intelligence_sweep(db, tenant_id)),
    # 2026-08-26, real fix -- confirmed live: 1,461 of 1,500 real ICP checks in one run came back
    # "insufficient_information", overwhelmingly because revenue is null on the company, not
    # because it was genuinely evaluated and disqualified. Runs BEFORE icp_matching, in the SAME
    # cycle, so a company this backfills gets a real chance at a real ICP verdict immediately
    # rather than waiting for a future run. See revenue_estimation.py's own docstring for the
    # bounded/budget-gated real cost model.
    ("revenue_backfill", lambda db, tenant_id: run_revenue_backfill_sweep(db, tenant_id, limit=30)),
    # ICP matching (icp_matching.py) -- until this GTM-OS wiring pass, run_icp_matching_sweep()
    # had ZERO callers anywhere in the app (confirmed by full-repo grep): not the scheduler, not
    # any API route, not this orchestrator. The real ICPMatch table (read by the Demand Grid,
    # Account 360, icp_candidates, offering_recommendation, and governance) was never populated by
    # anything automatic. Placed here (after opportunity, before strategy) matching the spec's own
    # dependency diagram; ICPMatch itself only depends on Company, not Opportunity, so this stage's
    # own correctness doesn't depend on ordering, but the reading order matches intent. Reuses the
    # sweep verbatim, unmodified -- no second ICP engine.
    ("icp_matching", lambda db, tenant_id: run_icp_matching_sweep(db, tenant_id, limit=500)),
    ("gtm_strategy", lambda db, tenant_id: run_gtm_strategy_sweep(db, tenant_id)),
]

# V2 Phase 3/4 contact discovery (app/gtm_os/sales/contact_discovery.py) runs BETWEEN these two
# groups -- deliberately NOT folded into the generic ACCOUNT_STRATEGY_STAGES loop below like its
# neighbors, same reasoning as V2 discovery (Phase 1): it needs its own explicit
# succeeded/skipped/failed handling (a "skipped" contact-discovery tick -- paused, unconfigured
# budget, nothing eligible -- must NOT count toward any_succeeded the way every other stage's
# mere non-exception completion does). Must run AFTER gtm_strategy (eligibility depends on a
# real GtmStrategy existing for the Opportunity) and BEFORE message_generation (so a contact
# found this same tick is immediately available to be drafted for).
ACCOUNT_STRATEGY_STAGES_CONTACT_TO_MESSAGE: list[tuple[str, callable]] = [
    # Message generation (message_draft.py) -- makes a real LLM call per eligible Opportunity, so
    # placed after strategy/before the cheap read-only stages below and bounded by its own small
    # `limit` (see message_draft.py's own cost-containment note) rather than left permanently
    # uncalled. Every draft this produces stops at status="draft"/"ready_for_review" --
    # approve_message_draft() is a SEPARATE, human-only action (app/routes/api.py's
    # POST /gtm-os/messages/{id}/review), never invoked by this sweep or anything it calls. This
    # is the human-approval boundary the whole autonomous cycle stops at.
    ("message_generation", lambda db, tenant_id: run_message_generation_sweep(db, tenant_id, limit=20)),
]

# V2 Phase 7 send (app/gtm_os/send/send.py) runs BETWEEN message_generation and the two stages
# below -- same reasoning as V2 discovery/contact_discovery: needs its own explicit
# succeeded/skipped/failed handling (a "skipped" send tick -- paused, missing safety config,
# outside business hours -- must not count as any_succeeded the way every other stage's mere
# non-exception completion does), so it sits outside the generic ACCOUNT_STRATEGY_STAGES loop.
# Must run AFTER message_generation (nothing to send before a draft exists) and BEFORE
# outcome_detection (which reads reply signals that only matter once something was actually sent).
ACCOUNT_STRATEGY_STAGES_POST_SEND: list[tuple[str, callable]] = [
    ("sales_readiness", lambda db, tenant_id: run_sales_agent_sweep(db, tenant_id)),
    # Batch 7 -- outcome detection reuses existing linkedin_reply InterpretedSignal rows only
    # (zero LLM/external calls, see outcome.py).
    ("outcome_detection", lambda db, tenant_id: run_outcome_detection_sweep(db, tenant_id)),
]

# Kept as a single flat list too -- dry_run's own preview loop and any other code that wants
# "every account/strategy stage key" iterates this, rather than remembering to combine every
# segment everywhere.
ACCOUNT_STRATEGY_STAGES: list[tuple[str, callable]] = (
    ACCOUNT_STRATEGY_STAGES_PRE_CONTACT + ACCOUNT_STRATEGY_STAGES_CONTACT_TO_MESSAGE + ACCOUNT_STRATEGY_STAGES_POST_SEND
)

# The complete, CURRENT set of sources the interpretation/detection layers know how to handle
# (interpretation.py::_INTERPRETERS, kept in sync manually -- see module docstring for why this
# exists instead of trusting either sweep function's own default).
ALL_INTERPRETED_SOURCES = ["linkedin_job", "theirstack_job", "linkedin_post", "linkedin_reply", "website_visitor"]


class MissingSourceConfiguration(Exception):
    """Raised by a source wrapper (never by the sweep loop itself) when required tenant
    configuration isn't set -- caught and reported as a "skipped" source, never a crash."""


class SourceBudgetBlocked(Exception):
    """Raised by a source wrapper (never by the sweep loop itself) when a real, configured
    budget guard blocks the call -- caught and reported as a "skipped" source, same as
    MissingSourceConfiguration, never a "failed" source. A budget block is an intentional safety
    gate working as designed, not an error -- conflating the two would misreport GtmIntelligenceRun
    as "partial" (mixed success/failure) for a tick where nothing actually went wrong."""


def _get_salesrobot_config(db: Session, tenant_id: int) -> tuple[str, list[str]] | None:
    """Mirrors app/routes/api.py's _get_salesrobot_linkedin_account_uuid()/_get_our_campaign_uuids()
    exactly -- same Parameter keys, same fallback from salesrobot_our_campaign_uuids to the
    single salesrobot_campaign_uuid -- but returns None instead of raising HTTPException. This
    layer must never import from app/routes/* (the same rule already established for
    linkedin_utils.py in Step 11A), so the small amount of Parameter-reading logic is duplicated
    here rather than imported, exactly as Step 11A did for normalize_linkedin_url in the other
    direction."""
    account_param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id, Parameter.key == "salesrobot_linkedin_account_uuid")
        .first()
    )
    if not account_param or not account_param.value:
        return None
    account_uuid = account_param.value.get("value") if isinstance(account_param.value, dict) else account_param.value
    if not account_uuid:
        return None

    uuids_param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id, Parameter.key == "salesrobot_our_campaign_uuids")
        .first()
    )
    campaign_uuids = None
    if uuids_param and uuids_param.value and isinstance(uuids_param.value, dict) and uuids_param.value.get("uuids"):
        campaign_uuids = uuids_param.value["uuids"]
    else:
        single_param = (
            db.query(Parameter)
            .filter(Parameter.tenant_id == tenant_id, Parameter.key == "salesrobot_campaign_uuid")
            .first()
        )
        if single_param and single_param.value:
            val = single_param.value.get("value") if isinstance(single_param.value, dict) else single_param.value
            campaign_uuids = [val] if val else None

    if not campaign_uuids:
        return None
    return account_uuid, campaign_uuids


def _run_linkedin_jobs(db: Session, tenant_id: int):
    """Replaces theirstack_job (Deepline) as this sweep's job-posting sensing source, 2026-08-23
    -- theirstack_job_search was failing every tick on a real, persistent Deepline credit
    exhaustion (workspace balance -0.96), with no fallback. sense_linkedin_jobs (Apify) already
    existed, already fully wired through interpretation/problem_detection/demand_detection
    (ALL_INTERPRETED_SOURCES already lists "linkedin_job" first), and was simply never
    registered in SWEEPABLE_SOURCES -- this is that wiring, not new capability.

    Reuses V1's own real, validated filter constants from app/phases/apify_discovery.py
    (APIFY_TITLE_SEARCH/APIFY_INDUSTRY_FILTER/APIFY_EMPLOYEE_MIN/MAX) rather than inventing a
    second set. limit=25 mirrors sense_theirstack_jobs' own prior default (worst-case
    25*$0.005+$0.01 = $0.135/tick) -- deliberately smaller than V1's discovery-run sizing
    (target*20, up to 150), since this runs once a DAY now (see control.py's
    get_intelligence_schedule_utc) as an incremental "what's new" signal, not a bulk discovery
    sweep.

    Unlike sense_theirstack_jobs, this source makes a REAL PAID Apify call with no budget check
    of its own -- so, unlike the old wrapper, this one checks apify_budget_guard itself before
    calling, the same real guard S7 investigation already uses. A block is reported as
    "skipped" (SourceBudgetBlocked), never "failed" -- deliberately blocked by budget is not the
    same thing as broken."""
    from app.apify_client import estimate_cost_usd
    from app.apify_budget_guard import STATUS_ALLOWED, check_apify_budget
    from app.phases.apify_discovery import APIFY_EMPLOYEE_MAX, APIFY_EMPLOYEE_MIN, APIFY_INDUSTRY_FILTER, APIFY_TITLE_SEARCH

    limit = 25
    budget_result = check_apify_budget(db, tenant_id, estimate_cost_usd(limit))
    if budget_result["status"] != STATUS_ALLOWED:
        raise SourceBudgetBlocked(budget_result["reason"])

    return sense_linkedin_jobs(
        db, tenant_id,
        title_search=APIFY_TITLE_SEARCH,
        location_search=["United States"],
        organization_employees_gte=APIFY_EMPLOYEE_MIN,
        organization_employees_lte=APIFY_EMPLOYEE_MAX,
        industry_filter=APIFY_INDUSTRY_FILTER,
        time_range="24h",
        limit=limit,
    )


def _run_linkedin_replies(db: Session, tenant_id: int):
    config = _get_salesrobot_config(db, tenant_id)
    if config is None:
        raise MissingSourceConfiguration(
            "salesrobot_linkedin_account_uuid and/or salesrobot_our_campaign_uuids/"
            "salesrobot_campaign_uuid not configured for this tenant"
        )
    account_uuid, campaign_uuids = config
    return sense_linkedin_replies(db, tenant_id, account_uuid, campaign_uuids)


def _run_hackernews(db: Session, tenant_id: int):
    # No new topic configuration invented -- uses sense_hackernews_stories' own existing
    # defaults; the function itself already senses nothing if zero enabled content topics are
    # configured for this tenant (Step 16C's own documented behavior), so no MissingSourceConfiguration
    # is needed here -- an empty topic list is a valid, non-error tenant state, not a misconfiguration.
    return sense_hackernews_stories(db, tenant_id)


def _run_rss(db: Session, tenant_id: int):
    # Same reasoning as _run_hackernews -- sense_rss_articles already senses nothing if zero
    # feeds are configured (Step 16D), which is a valid state, not an error.
    return sense_rss_articles(db, tenant_id)


def _run_linkedin_post_search(db: Session, tenant_id: int):
    # No MissingSourceConfiguration needed -- linkedin_search_config.py always has a derived
    # default (computed from live ICP/offering/business-context config) even with zero saved
    # overrides, same "empty config is a valid state" reasoning as HN/RSS above. Rate-limited
    # internally (select_due_phrases) -- may legitimately return [] on a tick where every
    # configured phrase was searched too recently to search again.
    return sense_linkedin_post_search(db, tenant_id)


def _run_website_visitors(db: Session, tenant_id: int):
    # No MissingSourceConfiguration needed -- sense_website_visitors() already senses nothing if
    # zero resolved WebsiteVisitor rows exist for this tenant yet, a valid state (the tracking
    # snippet may simply not have captured a resolved visit since the last sweep), not an error.
    # Free (no external paid call -- WebsiteVisitor rows are already resolved at write time by
    # app/website_visitor_tracking.py, this only reads them).
    return sense_website_visitors(db, tenant_id)


def _run_web_search_trends(db: Session, tenant_id: int):
    """Content Intelligence trend leg (2026-08-28) -- real paid Apify calls (Google Search), so,
    same as _run_linkedin_jobs, this checks apify_budget_guard itself before calling. No
    MissingSourceConfiguration needed -- sense_web_search_trends() already senses nothing if zero
    enabled content topics are configured, a valid state, not a misconfiguration."""
    from app.apify_client import GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD
    from app.apify_budget_guard import STATUS_ALLOWED, check_apify_budget

    limit = 20
    budget_result = check_apify_budget(db, tenant_id, limit * GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD)
    if budget_result["status"] != STATUS_ALLOWED:
        raise SourceBudgetBlocked(budget_result["reason"])

    return sense_web_search_trends(db, tenant_id, limit=limit)


def _run_competitor_content(db: Session, tenant_id: int):
    """Content Intelligence competitor leg (2026-08-28) -- same real-paid-call budget check as
    _run_web_search_trends above. No MissingSourceConfiguration needed -- sense_competitor_content()
    already senses nothing if zero enabled topics or zero enabled competitors are configured."""
    from app.apify_client import GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD
    from app.apify_budget_guard import STATUS_ALLOWED, check_apify_budget

    limit = 30
    budget_result = check_apify_budget(db, tenant_id, limit * GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD)
    if budget_result["status"] != STATUS_ALLOWED:
        raise SourceBudgetBlocked(budget_result["reason"])

    return sense_competitor_content(db, tenant_id, limit=limit)


# Source registration -- (name, runner(db, tenant_id) -> list[GtmSignal]). A future source
# (Reddit/X/YouTube/etc., none added in this step) means adding one entry here; the sweep loop
# below never branches on source name (Step 13 design doc §14). hackernews_story/rss_article
# added in Batch 2 -- both were built (Step 16C/16D) and tested but never wired into any
# recurring sweep until now; Content Intelligence's own stages (CONTENT_INTELLIGENCE_STAGES)
# have nothing real to process without them. linkedin_post_search added in the GTM-OS end-to-end
# wiring pass -- the one source capable of OPENING a new ProblemHypothesis (see
# problem_detection.py's own tier map); explicitly NOT the Network/LinkedIn-monitor watch-list,
# see sense_linkedin_post_search()'s own docstring. website_visitor added in Channels Intelligence
# step 4 -- deliberately contextual-tier only (see sense_website_visitors()'s own docstring for
# why it can never open a hypothesis alone). web_search_trend/competitor_content added for
# Content Intelligence's 60/40 trend/competitor content mix (2026-08-28) -- neither is meant to
# open an outbound Problem/Demand hypothesis (deliberately absent from ALL_INTERPRETED_SOURCES
# below), only to feed CONTENT_INTELLIGENCE_STAGES' already source-agnostic topic-linking
# pipeline (see sense_web_search_trends()'s own docstring).
SWEEPABLE_SOURCES: list[tuple[str, callable]] = [
    ("linkedin_job", _run_linkedin_jobs),
    ("linkedin_reply", _run_linkedin_replies),
    ("hackernews_story", _run_hackernews),
    ("rss_article", _run_rss),
    ("linkedin_post_search", _run_linkedin_post_search),
    ("website_visitor", _run_website_visitors),
    ("web_search_trend", _run_web_search_trends),
    ("competitor_content", _run_competitor_content),
]


def _dry_run_source_status(db: Session, tenant_id: int, name: str) -> dict:
    if name == "linkedin_reply":
        if _get_salesrobot_config(db, tenant_id) is None:
            return {"status": "would_skip", "reason": "missing SalesRobot configuration for this tenant"}
        return {"status": "would_run"}
    return {"status": "would_run"}


def run_gtm_intelligence_sweep(
    db: Session,
    tenant_id: int,
    sources: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Runs the first GTM Intelligence Sweep for one tenant: sensing (the sources currently
    registered in SWEEPABLE_SOURCES, optionally narrowed via `sources`), then interpretation,
    then Problem detection, then Demand detection -- each stage using the complete, explicit
    current source list (ALL_INTERPRETED_SOURCES), never either sweep function's own stale
    default (see module docstring).

    `sources`: optional subset of sweepable source names to run (e.g. ["linkedin_reply"]).
    Defaults to every currently-registered sweepable source.

    `dry_run`: reports which sources WOULD run and whether their configuration exists, and which
    interpretation/detection/content-intelligence stages WOULD run -- makes zero external/paid
    calls (including zero LLM calls -- candidate_extraction/candidate_normalization are both
    LLM-calling stages, see module docstring's real-money note) and writes nothing. Does not
    simulate sensing in any other way (no fake signal counts) -- it only answers "would this be
    attempted," per the Step 13 spec's own instruction not to pretend a dry run performed
    sensing, extended unchanged to every stage added in Batch 2.

    Never raises for an individual source/stage failure -- see module docstring for why, and for
    why this is a deliberate choice, not an oversight. `tenant_id` is threaded, unchanged, into
    every downstream call -- this function never resolves or iterates tenants itself."""
    selected = sources if sources is not None else [name for name, _ in SWEEPABLE_SOURCES]
    result: dict = {
        "status": "completed",
        "discovery": {},
        "sources": {},
        "investigation_cycle": {},
        "interpretation": {},
        "problem_detection": {},
        "demand_detection": {},
        "topic_linking": {},
        "candidate_extraction": {},
        "candidate_normalization": {},
        "candidate_promotion": {},
        "trend_intelligence": {},
        "content_opportunity_generation": {},
        "opportunity": {},
        "revenue_backfill": {},
        "icp_matching": {},
        "gtm_strategy": {},
        "contact_discovery": {},
        "send": {},
        "outreach_sequencing": {},
        "message_generation": {},
        "sales_readiness": {},
        "outcome_detection": {},
    }

    if dry_run:
        # Free (read-only, no provider call) -- reports the real current due/not-due state,
        # matching this whole branch's own "reports what WOULD happen" contract, rather than a
        # placeholder like the stages below (those genuinely can't preview without a paid call;
        # this one can for free).
        from app.gtm_os.orchestration.discovery import is_discovery_due
        due, reason = is_discovery_due(db, tenant_id)
        result["discovery"] = {"status": "would_run" if due else "would_skip", "reason": reason}
        for name, _runner in SWEEPABLE_SOURCES:
            if name not in selected:
                result["sources"][name] = {"status": "skipped", "reason": "not selected"}
                continue
            result["sources"][name] = _dry_run_source_status(db, tenant_id, name)

        # Autonomous Sensing Phase S7 -- free, read-only preview of the same real gates
        # run_investigation_cycle() itself checks, no S3-S6 side effects.
        investigation_config = get_control_config(db, tenant_id)
        max_per_tick = investigation_config.get("investigation", {}).get("max_objectives_per_tick")
        if max_per_tick:
            result["investigation_cycle"] = {"status": "would_run", "max_objectives_per_tick": max_per_tick}
        else:
            result["investigation_cycle"] = {"status": "configuration_required", "reason": "investigation.max_objectives_per_tick is not configured"}

        result["interpretation"] = {"status": "would_run", "sources": ALL_INTERPRETED_SOURCES}
        result["problem_detection"] = {"status": "would_run", "sources": ALL_INTERPRETED_SOURCES}
        result["demand_detection"] = {"status": "would_run", "sources": ALL_INTERPRETED_SOURCES}
        for stage_key, _runner in CONTENT_INTELLIGENCE_STAGES:
            result[stage_key] = {"status": "would_run"}

        outbound_due, outbound_reason = is_outbound_cycle_due(db, tenant_id)
        for stage_key, _runner in ACCOUNT_STRATEGY_STAGES:
            if stage_key in ("gtm_strategy", "message_generation"):
                result[stage_key] = {"status": "would_run" if outbound_due else "would_skip", "reason": outbound_reason}
            else:
                result[stage_key] = {"status": "would_run"}
        result["contact_discovery"] = {"status": "would_run" if outbound_due else "would_skip", "reason": outbound_reason}
        result["send"] = {"status": "would_run" if outbound_due else "would_skip", "reason": outbound_reason}
        result["outreach_sequencing"] = {"status": "would_run" if outbound_due else "would_skip", "reason": outbound_reason}
        return result

    any_succeeded = False
    any_failed = False

    # V2-owned discovery (Phase 1, app/gtm_os/orchestration/discovery.py) -- runs first, before
    # sensing, since a company it finds this tick is what makes opportunity/icp_matching below
    # have anything new to work with. Never raises (see that module's own docstring) and
    # internally no-ops (real, logged skip -- never silently invisible) unless the control
    # plane is running AND cadence/daily_target are configured AND enough time has passed since
    # the last V2 discovery run -- this is what keeps every hourly tick from spending discovery
    # budget by default (see control.py's DEFAULT_GTM_OS_CONTROL_CONFIG: both are None/
    # unconfigured until an operator sets them).
    discovery_result = run_v2_discovery_if_due(db, tenant_id)
    result["discovery"] = discovery_result
    if discovery_result.get("status") == "succeeded":
        any_succeeded = True
    elif discovery_result.get("status") == "failed":
        any_failed = True
        logger.error("gtm_intelligence_sweep: discovery failed -- %s", discovery_result.get("error"))

    for name, runner in SWEEPABLE_SOURCES:
        if name not in selected:
            result["sources"][name] = {"status": "skipped", "reason": "not selected"}
            continue
        logger.info("gtm_intelligence_sweep: sensing %s started (tenant_id=%s)", name, tenant_id)
        try:
            signals = runner(db, tenant_id)
            result["sources"][name] = {"status": "succeeded", "signals_created": len(signals)}
            any_succeeded = True
            logger.info("gtm_intelligence_sweep: sensing %s succeeded (%d signals)", name, len(signals))
        except MissingSourceConfiguration as e:
            result["sources"][name] = {"status": "skipped", "reason": str(e)}
            logger.warning("gtm_intelligence_sweep: sensing %s skipped -- %s", name, e)
        except SourceBudgetBlocked as e:
            result["sources"][name] = {"status": "skipped", "reason": str(e)}
            logger.info("gtm_intelligence_sweep: sensing %s skipped (budget) -- %s", name, e)
        except Exception as e:  # noqa: BLE001 -- one source's failure must never block the others; see module docstring
            db.rollback()  # 2026-08-26, real fix -- see the try/except above this function's ACCOUNT_STRATEGY_STAGES loop for the full explanation
            result["sources"][name] = {"status": "failed", "error": str(e)}
            any_failed = True
            logger.error("gtm_intelligence_sweep: sensing %s failed -- %s", name, e)

    # Autonomous Sensing Phase S7 (app/gtm_os/intelligence/investigation_cycle.py) -- runs BEFORE
    # interpretation/problem/demand below so any GtmSignal rows S5 execution created this same
    # tick get picked up by the SAME existing interpretation pass, not a following one. Bounded
    # per control.investigation.max_objectives_per_tick; S2 gap-identification itself always runs
    # (free/local), S3-S6 are skipped with an explicit configuration_required status while that
    # cap is unconfigured. Never raises -- same per-stage error isolation as every other stage here.
    try:
        investigation_result = run_investigation_cycle(db, tenant_id)
        result["investigation_cycle"] = investigation_result
        if investigation_result.get("status") in ("succeeded", "partial"):
            any_succeeded = True
        if investigation_result.get("status") == "partial":
            any_failed = True
        logger.info("gtm_intelligence_sweep: investigation_cycle %s", investigation_result.get("status"))
    except Exception as e:  # noqa: BLE001 -- see module docstring
        db.rollback()  # 2026-08-26, real fix -- see the ACCOUNT_STRATEGY_STAGES loop's own comment for the full explanation
        result["investigation_cycle"] = {"status": "failed", "error": str(e)}
        any_failed = True
        logger.error("gtm_intelligence_sweep: investigation_cycle failed -- %s", e)

    try:
        interpreted = run_interpretation_sweep(db, tenant_id, sources=ALL_INTERPRETED_SOURCES)
        result["interpretation"] = {"status": "succeeded", "created": len(interpreted)}
        any_succeeded = True
        logger.info("gtm_intelligence_sweep: interpretation succeeded (%d created)", len(interpreted))
    except Exception as e:  # noqa: BLE001 -- see module docstring
        db.rollback()  # 2026-08-26, real fix -- see the ACCOUNT_STRATEGY_STAGES loop's own comment for the full explanation
        result["interpretation"] = {"status": "failed", "error": str(e)}
        any_failed = True
        logger.error("gtm_intelligence_sweep: interpretation failed -- %s", e)

    try:
        problems = run_problem_hypothesis_sweep(db, tenant_id, sources=ALL_INTERPRETED_SOURCES)
        result["problem_detection"] = {"status": "succeeded", "hypotheses_touched": len(problems)}
        any_succeeded = True
        logger.info("gtm_intelligence_sweep: problem detection succeeded (%d hypotheses touched)", len(problems))
    except Exception as e:  # noqa: BLE001 -- see module docstring
        db.rollback()  # 2026-08-26, real fix -- see the ACCOUNT_STRATEGY_STAGES loop's own comment for the full explanation
        result["problem_detection"] = {"status": "failed", "error": str(e)}
        any_failed = True
        logger.error("gtm_intelligence_sweep: problem detection failed -- %s", e)

    try:
        demands = run_demand_hypothesis_sweep(db, tenant_id, sources=ALL_INTERPRETED_SOURCES)
        result["demand_detection"] = {"status": "succeeded", "hypotheses_touched": len(demands)}
        any_succeeded = True
        logger.info("gtm_intelligence_sweep: demand detection succeeded (%d hypotheses touched)", len(demands))
    except Exception as e:  # noqa: BLE001 -- see module docstring
        db.rollback()  # 2026-08-26, real fix -- see the ACCOUNT_STRATEGY_STAGES loop's own comment for the full explanation
        result["demand_detection"] = {"status": "failed", "error": str(e)}
        any_failed = True
        logger.error("gtm_intelligence_sweep: demand detection failed -- %s", e)

    # Content Intelligence branch -- runs regardless of whether interpretation/problem/demand
    # above succeeded or failed (own GtmSignal-rooted branch, see module docstring). Stages run
    # in order (each depends only on its own prior stage's output), but each has its own failure
    # boundary -- a failure in one (e.g. candidate_extraction) does not skip a later one whose
    # prerequisites are still valid (e.g. candidate_promotion simply finds no new eligible
    # clusters that run produced, not an error) -- same reasoning gpt.txt's own examples give.
    for stage_key, runner in CONTENT_INTELLIGENCE_STAGES:
        try:
            stage_result = runner(db, tenant_id)
            result[stage_key] = {"status": "succeeded", **stage_result}
            any_succeeded = True
            logger.info("gtm_intelligence_sweep: %s succeeded -- %s", stage_key, stage_result)
        except Exception as e:  # noqa: BLE001 -- one stage's failure must never block the others; see module docstring
            # 2026-08-26, real fix (same class of bug as the contact_discovery.py crash fixed
            # earlier -- confirmed live): an uncaught DB-level exception here leaves the shared
            # session's transaction invalid for every stage still to come in this same sweep.
            db.rollback()
            result[stage_key] = {"status": "failed", "error": str(e)}
            any_failed = True
            logger.error("gtm_intelligence_sweep: %s failed -- %s", stage_key, e)

    # Account/Strategy/Sales branch (Batch 6) -- reads DemandHypothesis (produced above), but its
    # own failure never touches Problem/Demand or Content Intelligence, and vice versa. Each
    # stage is a pure/idempotent read-or-additive-insert sweep with zero LLM/external/CRM calls
    # (see opportunity.py/strategy.py/sales_agent.py docstrings) -- safe to run every cycle even
    # with near-zero real data.
    #
    # Autonomous Sensing Phase S7 -- approved hybrid-cadence architecture: "opportunity"/
    # "icp_matching" are pure evidence-evaluation stages, not outbound ACTIONS, so they stay
    # UNGATED and run every hourly tick same as always. "gtm_strategy" onward through
    # "outreach_sequencing" (contact_discovery, message_generation, send) ARE the
    # downstream/outbound-adjacent stages named in the approved design -- gated to the slower
    # is_outbound_cycle_due() cadence, never executed autonomously every hour. sales_readiness/
    # outcome_detection remain ungated too -- both are read-only reporting/detection, not actions.
    outbound_due, outbound_reason = is_outbound_cycle_due(db, tenant_id)
    for stage_key, runner in ACCOUNT_STRATEGY_STAGES_PRE_CONTACT:
        if stage_key == "gtm_strategy" and not outbound_due:
            result[stage_key] = {"status": "skipped", "reason": outbound_reason}
            continue
        try:
            stage_result = runner(db, tenant_id)
            result[stage_key] = {"status": "succeeded", **stage_result}
            any_succeeded = True
            logger.info("gtm_intelligence_sweep: %s succeeded -- %s", stage_key, stage_result)
        except Exception as e:  # noqa: BLE001 -- one stage's failure must never block the others; see module docstring
            # 2026-08-26, real fix (same class of bug as the contact_discovery.py crash fixed
            # earlier -- confirmed live): an uncaught DB-level exception here leaves the shared
            # session's transaction invalid for every stage still to come in this same sweep.
            db.rollback()
            result[stage_key] = {"status": "failed", "error": str(e)}
            any_failed = True
            logger.error("gtm_intelligence_sweep: %s failed -- %s", stage_key, e)

    # V2-owned contact discovery (Phase 3/4, app/gtm_os/sales/contact_discovery.py) -- see
    # ACCOUNT_STRATEGY_STAGES_POST_CONTACT's own comment above for why this sits here, outside
    # the generic loop, with its own explicit succeeded/skipped/failed handling. Outbound-gated
    # (S7) -- see comment above the gtm_strategy loop.
    if not outbound_due:
        contact_discovery_result = {"status": "skipped", "reason": outbound_reason}
    else:
        try:
            contact_discovery_result = run_v2_contact_discovery_sweep(db, tenant_id, limit=50)
        except Exception as e:  # noqa: BLE001 -- real bug fix (2026-08-26, confirmed live): this
            # was the ONE stage call in this whole function missing the try/except every other
            # stage here has -- confirmed live, a real sqlalchemy.exc.PendingRollbackError
            # (itself caused by a separate now-fixed bug in contact_discovery.py) propagated all
            # the way out of run_gtm_intelligence_sweep and killed the ENTIRE remaining sweep
            # (message_generation, send, outreach_sequencing never even attempted), directly
            # violating this module's own "never raises for an individual source/stage failure"
            # docstring promise. rollback() first since an uncaught DB-level exception can leave
            # this shared session invalid for every stage still to come.
            db.rollback()
            contact_discovery_result = {"status": "failed", "error": str(e)}
            logger.error("gtm_intelligence_sweep: contact_discovery raised unexpectedly -- %s", e)
    result["contact_discovery"] = contact_discovery_result
    if contact_discovery_result.get("status") == "succeeded":
        any_succeeded = True
    elif contact_discovery_result.get("status") == "failed":
        any_failed = True
        logger.error("gtm_intelligence_sweep: contact_discovery failed -- %s", contact_discovery_result.get("error"))

    for stage_key, runner in ACCOUNT_STRATEGY_STAGES_CONTACT_TO_MESSAGE:
        if not outbound_due:
            result[stage_key] = {"status": "skipped", "reason": outbound_reason}
            continue
        try:
            stage_result = runner(db, tenant_id)
            result[stage_key] = {"status": "succeeded", **stage_result}
            any_succeeded = True
            logger.info("gtm_intelligence_sweep: %s succeeded -- %s", stage_key, stage_result)
        except Exception as e:  # noqa: BLE001 -- one stage's failure must never block the others; see module docstring
            # 2026-08-26, real fix (same class of bug as the contact_discovery.py crash fixed
            # earlier -- confirmed live): an uncaught DB-level exception here leaves the shared
            # session's transaction invalid for every stage still to come in this same sweep.
            db.rollback()
            result[stage_key] = {"status": "failed", "error": str(e)}
            any_failed = True
            logger.error("gtm_intelligence_sweep: %s failed -- %s", stage_key, e)

    # V2-owned send (Phase 7, app/gtm_os/send/send.py) -- see ACCOUNT_STRATEGY_STAGES_POST_SEND's
    # own comment above for why this sits here, outside the generic loop, with its own explicit
    # succeeded/skipped/failed handling. Outbound-gated (S7).
    if not outbound_due:
        send_result = {"status": "skipped", "reason": outbound_reason}
    else:
        send_result = run_v2_send_sweep(db, tenant_id, limit=50)
    result["send"] = send_result
    if send_result.get("status") == "succeeded":
        any_succeeded = True
    elif send_result.get("status") == "failed":
        any_failed = True
        logger.error("gtm_intelligence_sweep: send failed -- %s", send_result.get("error"))

    for stage_key, runner in ACCOUNT_STRATEGY_STAGES_POST_SEND:
        try:
            stage_result = runner(db, tenant_id)
            result[stage_key] = {"status": "succeeded", **stage_result}
            any_succeeded = True
            logger.info("gtm_intelligence_sweep: %s succeeded -- %s", stage_key, stage_result)
        except Exception as e:  # noqa: BLE001 -- one stage's failure must never block the others; see module docstring
            # 2026-08-26, real fix (same class of bug as the contact_discovery.py crash fixed
            # earlier -- confirmed live): an uncaught DB-level exception here leaves the shared
            # session's transaction invalid for every stage still to come in this same sweep.
            db.rollback()
            result[stage_key] = {"status": "failed", "error": str(e)}
            any_failed = True
            logger.error("gtm_intelligence_sweep: %s failed -- %s", stage_key, e)

    # V2-owned multi-contact outreach sequencing (Phase 8, app/gtm_os/sales/outreach_sequencing.py)
    # -- runs LAST, after outcome_detection, so it sees this same tick's freshest SalesOutcome
    # data before deciding whether to advance any opportunity to a fallback contact. Same
    # explicit succeeded/skipped/failed handling as discovery/contact_discovery/send, outside
    # the generic loop. Outbound-gated (S7).
    if not outbound_due:
        outreach_sequencing_result = {"status": "skipped", "reason": outbound_reason}
    else:
        outreach_sequencing_result = run_v2_outreach_sequencing_sweep(db, tenant_id, limit=50)
    result["outreach_sequencing"] = outreach_sequencing_result
    if outreach_sequencing_result.get("status") == "succeeded":
        any_succeeded = True
    elif outreach_sequencing_result.get("status") == "failed":
        any_failed = True
        logger.error("gtm_intelligence_sweep: outreach_sequencing failed -- %s", outreach_sequencing_result.get("error"))

    if any_failed and any_succeeded:
        result["status"] = "partial"
    elif any_failed:
        result["status"] = "failed"
    else:
        result["status"] = "completed"
    return result


def _flow_window_start(db: Session, tenant_id: int, now: datetime | None = None) -> datetime:
    """Midnight of the current day, in the tenant's own configured business_hours.timezone (the
    one real, already-existing tenant-timezone field in this codebase -- see control.py's
    DEFAULT_GTM_OS_CONTROL_CONFIG) -- reused rather than introducing a second timezone concept.
    Falls back to UTC on a missing/invalid zone string, same fail-safe pattern
    is_within_business_hours() already uses. Returned as a naive UTC datetime (matching every
    other timestamp column in this codebase, e.g. MessageDraft.created_at), for direct comparison
    against DB-stored values."""
    now = now or datetime.utcnow()
    tz_name = (get_control_config(db, tenant_id).get("business_hours") or {}).get("timezone") or "UTC"
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    local_now = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def count_completed_flows_today(db: Session, tenant_id: int, now: datetime | None = None) -> int:
    """A completed 'flow' = one distinct Opportunity that has at least one real, usable
    MessageDraft (status 'ready_for_review' or 'approved' -- NOT 'draft', which failed the
    quality gate, and NOT 'insufficient_context'), created since local midnight today. Reuses the
    existing MessageDraft/Opportunity lineage exactly as verified before implementation:
    Opportunity.demand_hypothesis_id carries a real DB-level UNIQUE index
    (ix_opportunities_demand_hypothesis, app/db/session.py) guaranteeing one Opportunity per
    independent Problem/Demand lineage, so COUNT(DISTINCT opportunity_id) here is exactly
    COUNT(DISTINCT flow) -- no new Flow table, no new counter column. Multiple MessageDrafts for
    the same Opportunity (one per contact, per V2 Phase 8) correctly count once."""
    window_start = _flow_window_start(db, tenant_id, now)
    return (
        db.query(func.count(func.distinct(MessageDraft.opportunity_id)))
        .filter(
            MessageDraft.tenant_id == tenant_id,
            MessageDraft.status.in_(("ready_for_review", "approved")),
            MessageDraft.created_at >= window_start,
        )
        .scalar()
        or 0
    )


def _no_eligible_work_remaining(result: dict) -> bool:
    """True only when literally nothing new could have entered ANY flow's lineage this
    iteration -- checked at the origin of the pipeline (sensing + investigation + interpretation),
    never inferred from "zero new completed flows" (per the explicit zero-progress rule: a flow
    can legitimately take several iterations to mature from Signal through to MessageDraft).

    Reasoning: every downstream stage (opportunity/icp_matching/gtm_strategy/contact_discovery/
    message_generation) is already a pure, idempotent full re-scan of EXISTING rows (see each
    stage's own docstring -- "safe to run every cycle even with near-zero real data", never
    duplicates on an unchanged rerun). Re-running them again against the exact same upstream data
    they already saw cannot produce a new flow -- a new flow can only originate from either a
    genuinely new sensed signal, a new investigation attempt, or a newly-created interpretation.
    So checking only these three origin-level counts (not every individual downstream counter) is
    sufficient and correct, without needing any of the six existing stage functions to be
    modified or given a new rejection vocabulary."""
    total_new_signals = sum(
        source.get("signals_created", 0)
        for source in (result.get("sources") or {}).values()
        if isinstance(source, dict) and source.get("status") == "succeeded"
    )
    objectives_processed = (result.get("investigation_cycle") or {}).get("objectives_processed", 0)
    interpretation_created = (result.get("interpretation") or {}).get("created", 0)
    return total_new_signals == 0 and objectives_processed == 0 and interpretation_created == 0


def run_gtm_daily_flow_cycle(db: Session, tenant_id: int) -> dict:
    """Wraps the existing, UNMODIFIED run_gtm_intelligence_sweep() in an outer target-seeking
    loop -- per the approved 2026-08-24 design, adds daily_flow_target/max_iterations_per_run
    WITHOUT rewriting any of the five independent batch stages into a serial per-company/per-flow
    loop. Each iteration is one full, ordinary call to run_gtm_intelligence_sweep(); the
    controller only measures outcomes between iterations, it never selects candidates itself.

    BACKWARD COMPATIBLE BY CONSTRUCTION: when flow_target.daily_flow_target or
    flow_target.max_iterations_per_run is unconfigured (both None by default -- see control.py),
    this runs run_gtm_intelligence_sweep() exactly ONCE and returns its result completely
    unchanged, identical to calling it directly. Nothing changes for any tenant until both are
    explicitly set -- same "None never means unlimited, and never changes default behavior"
    discipline as every other cap in this config.

    Every existing safety control composes unchanged: control-plane state is re-checked every
    iteration (not just once at the start -- a pause mid-run stops the NEXT iteration from
    starting), and every provider/spend budget (apify/discovery/contact-discovery) is enforced
    exactly as it already is inside each call to run_gtm_intelligence_sweep()."""
    config = get_control_config(db, tenant_id)
    flow_target_config = config.get("flow_target") or {}
    daily_flow_target = flow_target_config.get("daily_flow_target")
    max_iterations_per_run = flow_target_config.get("max_iterations_per_run")

    if not daily_flow_target or not max_iterations_per_run:
        return run_gtm_intelligence_sweep(db, tenant_id)

    now = datetime.utcnow()
    count_at_run_start = count_completed_flows_today(db, tenant_id, now)

    result: dict = {}
    iterations_run = 0
    stop_reason = None
    # Real observability gap fix (2026-08-24, confirmed live -- run 96): result was overwritten
    # each loop, so only the LAST iteration's investigation_cycle/opportunity/etc. counts were
    # ever visible afterward -- earlier iterations' outcomes (e.g. what a since-exhausted
    # objective's attempt actually returned) were unrecoverable without an ad-hoc reproduction.
    # Keeps only the compact, diagnostically-useful subset per iteration, not the full result
    # dict (which repeats large near-static blocks like icp_matching's 500-company scan).
    iteration_history: list = []

    while True:
        try:
            check_can_run(db, tenant_id)
        except ControlPlaneHalted as e:
            stop_reason = "control_plane_halted"
            if iterations_run == 0:
                result = {"status": "skipped", "reason": str(e)}
            break

        result = run_gtm_intelligence_sweep(db, tenant_id)
        iterations_run += 1
        iteration_history.append({
            "iteration": iterations_run,
            "investigation_cycle": result.get("investigation_cycle"),
            "opportunity": result.get("opportunity"),
            "gtm_strategy": result.get("gtm_strategy"),
            "contact_discovery": result.get("contact_discovery"),
            "message_generation": result.get("message_generation"),
        })

        # daily_flow_target is a DAY-cumulative total (count_at_run_start already includes any
        # flows completed earlier today, e.g. from an earlier manual "Run Now") -- not "10 new
        # flows produced by this particular invocation."
        current_count = count_completed_flows_today(db, tenant_id)

        if current_count >= daily_flow_target:
            stop_reason = "target_reached"
            break
        if iterations_run >= max_iterations_per_run:
            stop_reason = "iteration_ceiling_reached"
            break
        if _no_eligible_work_remaining(result):
            stop_reason = "no_eligible_work_remaining"
            break

    final_count = count_completed_flows_today(db, tenant_id, now)
    result["flow_target"] = {
        "daily_flow_target": daily_flow_target,
        "count_at_run_start": count_at_run_start,
        "completed_flow_count_now": final_count,
        "new_flows_this_run": final_count - count_at_run_start,
        "iterations_run": iterations_run,
        "stop_reason": stop_reason,
        "iteration_history": iteration_history,
    }
    return result
