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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
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
from app.gtm_os.decisions.revenue_plan import generate_revenue_plan
from app.gtm_os.icp.company_enrichment import run_company_enrichment_sweep
from app.gtm_os.orchestration.discovery_profiles import get_enabled_discovery_profiles
from app.gtm_os.icp.icp_matching import run_icp_matching_sweep
from app.gtm_os.icp.revenue_estimation import run_revenue_backfill_sweep
from app.gtm_os.intelligence.demand_detection import run_demand_hypothesis_sweep
from app.gtm_os.intelligence.interpretation import promote_concurrent_hiring_across_sweeps, run_interpretation_sweep
from app.gtm_os.intelligence.investigation_cycle import run_investigation_cycle
from app.db.session import SessionLocal
from app.gtm_os.intelligence.signal import GtmSignal
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
    # 2026-09-18, real fix: confirmed live that this exact commit can hit the same
    # stale-connection error every other long-lived use of this session risks -- losing the
    # FINAL status write after a real, successful, possibly hour-long sweep would be the worst
    # possible place for that bug to land (a genuinely completed run stuck reading "running"
    # forever). One retry after rollback, same discipline as every other fix today.
    try:
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        run.status = result.get("status", "completed")
        run.stage_results = result
        run.error_summary = "; ".join(failure_summaries) if failure_summaries else None
        run.completed_at = datetime.utcnow()
        db.commit()
    return run


def _run_stage_with_timeout(stage_fn, tenant_id: int, timeout_seconds: int) -> dict:
    """Real fix, 2026-09-17: confirmed live that a full sweep (run 128, triggered 08:18 UTC)
    finished sensing (58 GtmSignal rows) and discovery (9 companies) by ~08:27, then produced
    ZERO further writes to ANY downstream table for 36+ minutes while the run row still read
    "running" -- no timeout anywhere in this pipeline meant one slow/hung external call inside a
    single stage silently froze the entire sweep, indefinitely, with nothing surfacing until the
    120-minute stale-run recovery eventually marked it failed. investigation_cycle
    (app/gtm_os/intelligence/investigation_cycle.py) is the stage immediately after discovery and
    the most likely candidate (a real, previously-documented hang in this exact stage) -- this
    wrapper is applied there first, and is written generically so any other stage can get the
    same protection.

    Runs `stage_fn(db, tenant_id)` in its OWN thread with its OWN fresh db session (never the
    caller's -- a SQLAlchemy Session is not thread-safe, and the calling sweep must keep using
    its own session immediately after this returns, timeout or not). If the stage doesn't finish
    within timeout_seconds, this returns a real "timed_out" status and the sweep MOVES ON --
    the abandoned thread is a daemon and either finishes harmlessly in the background (its own
    session's writes still land for whichever objects it manages to reach) or dies when the
    process eventually restarts. Never blocks the rest of the sweep again."""
    import queue
    import threading

    from app.db.session import SessionLocal

    result_queue: queue.Queue = queue.Queue(maxsize=1)

    def _target():
        stage_db = SessionLocal()
        try:
            result_queue.put(("ok", stage_fn(stage_db, tenant_id)))
        except Exception as e:  # noqa: BLE001 -- report it through the queue, never crash the thread silently
            result_queue.put(("error", str(e)))
        finally:
            stage_db.close()

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    try:
        status, value = result_queue.get(timeout=timeout_seconds)
    except queue.Empty:
        return {"status": "timed_out", "reason": f"exceeded {timeout_seconds}s -- abandoned, sweep continuing"}
    if status == "error":
        return {"status": "failed", "error": value}
    # Real regression fix, 2026-09-18: confirmed live -- not every stage_fn's own return dict
    # sets "status" itself (company_enrichment does; opportunity/revenue_backfill/icp_matching
    # and others don't, they just return their raw metric dict). Every caller here checks
    # `.get("status") == "succeeded"` to decide success -- for a dict with no "status" key at
    # all, that's neither "succeeded" nor "timed_out", so _run_stage_with_retry's own check
    # (`first.get("status") in ("succeeded", "timed_out")`) fell through and ran the ENTIRE
    # stage a second time for no reason on every single one of these, then still logged
    # "failed -- None" at the end since the second attempt has the exact same shape. A real,
    # confirmed live-observed bug (opportunity/revenue_backfill/icp_matching all doubled and
    # misreported today) -- normalized here, once, for every caller, rather than requiring every
    # stage_fn to remember to self-report a status it was never designed to include.
    if isinstance(value, dict) and "status" not in value:
        value = {"status": "succeeded", **value}
    return value


def _run_stage_with_retry(runner, db: Session, tenant_id: int, timeout_seconds: int = 120) -> dict:
    """Real fix, 2026-09-17 (two rounds, same day): round 1 added a bare try/except+retry here
    for the "SSL connection has been closed unexpectedly" staleness bug (confirmed live in run
    131) -- but that retry ran the stage directly against the sweep's own shared `db` session,
    with no timeout at all. Confirmed live in run 136 (started 17:50, still "running" 13+ hours
    later, zero new signals/companies/anything written since 18:35): a stage using this wrapper
    hung -- blocked forever on some real network call that never returned an exception, which
    this wrapper had no way to detect or recover from, unlike _run_stage_with_timeout's
    thread+queue.get(timeout=...) approach used elsewhere in this file.

    Round 2 (this fix): every attempt now runs through _run_stage_with_timeout -- its own fresh
    session (never the sweep's, so the staleness bug round 1 fixed is structurally impossible
    here too, not just retried around) plus a real hard wall-clock bound. A "failed" result
    (a real exception, e.g. that same staleness class if it ever recurs) gets one retry, same as
    round 1. A "timed_out" result does NOT get retried -- if a stage is still hanging after
    timeout_seconds, retrying it immediately would just hang again; it's reported as-is and the
    sweep moves on, same discipline every other _run_stage_with_timeout call site in this module
    already follows."""
    first = _run_stage_with_timeout(runner, tenant_id, timeout_seconds)
    if first.get("status") == "succeeded" or first.get("status") == "timed_out":
        return first
    second = _run_stage_with_timeout(runner, tenant_id, timeout_seconds)
    if second.get("status") == "failed":
        second["first_attempt_error"] = first.get("error")
    return second


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
    # Runs BEFORE revenue_backfill/icp_matching: both read firmographics this stage fills in.
    # Confirmed live 2026-08-31 -- without it only 17 of 776 companies had the fields ICP
    # matching needs, so 1,486 of 1,500 checks returned "insufficient_information".
    ("company_enrichment", lambda db, tenant_id: run_company_enrichment_sweep(db, tenant_id)),
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
    # The decision layer runs LAST, after every measuring stage in this tick has updated its own
    # evidence -- a plan built on this morning's numbers would be advising on a funnel that has
    # already moved. Deterministic diagnosis + a verified plan; see decisions/revenue_plan.py.
    ("revenue_plan", lambda db, tenant_id: generate_revenue_plan(db, tenant_id)),
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


class SourceNotDue(Exception):
    """Raised by a source wrapper when that source runs on its OWN slower cadence and isn't due
    this tick -- caught and reported as "skipped", same family as SourceBudgetBlocked: an
    intentional schedule working as designed, never an error.

    Why this exists (2026-08-31): web_search_trend + competitor_content cost $0.275 of every
    sweep -- 36% of run cost -- and feed Content Intelligence's topic/trend layer, NOT the sales
    pipeline. Confirmed against real data: 228 competitor_content + 89 web_search_trend signals
    produced 0 sales opportunities, because every Opportunity in this system traces back to a
    job-posting signal. Daily cadence for them buys content freshness nobody reads daily, while
    consuming budget the 10-opportunities/day target needs. Weekly keeps the content layer fed
    at 1/7th the cost."""


def _source_cadence_due(db: Session, tenant_id: int, source: str, cadence_days: int) -> tuple[bool, str]:
    """True when this source has no signal newer than cadence_days. Uses the source's own real
    last-signal timestamp rather than a separate schedule table -- no new state to keep in sync,
    and it self-corrects after an outage (a missed week simply runs on the next tick)."""
    latest = (
        db.query(func.max(GtmSignal.created_at))
        .filter(GtmSignal.tenant_id == tenant_id, GtmSignal.source == source)
        .scalar()
    )
    if latest is None:
        return True, "never sensed"
    elapsed_days = (datetime.utcnow() - latest).total_seconds() / 86400.0
    if elapsed_days < cadence_days:
        return False, f"last sensed {elapsed_days:.1f}d ago, cadence is {cadence_days}d"
    return True, f"last sensed {elapsed_days:.1f}d ago"


CONTENT_SENSING_CADENCE_DAYS = 7
CONTENT_CADENCE_SOURCES = ("web_search_trend", "competitor_content")


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
    from app.phases.apify_discovery import (
        APIFY_DEFAULT_LOCATION_SEARCH, APIFY_EMPLOYEE_MAX, APIFY_EMPLOYEE_MIN, APIFY_INDUSTRY_FILTER,
        APIFY_TITLE_SEARCH,
    )

    # One search PER OFFERING PROFILE (2026-08-31), not a single hardcoded V1 filter set. The
    # old call searched only "25-50 person software company hiring a salesperson" -- Execution's
    # buyer -- so the other five offerings had no discovery at all. See discovery_profiles.py.
    limit = 25
    profiles = get_enabled_discovery_profiles(db, tenant_id)
    if not profiles:
        raise MissingSourceConfiguration("no enabled V2 discovery profiles configured")

    signals = []
    blocked_reasons = []
    for profile in profiles:
        # Budget-checked per profile, so an exhausted budget skips the remaining profiles
        # instead of the whole source failing -- and whatever ran before it still counts.
        budget_result = check_apify_budget(db, tenant_id, estimate_cost_usd(limit))
        if budget_result["status"] != STATUS_ALLOWED:
            blocked_reasons.append(f"{profile['id']}: {budget_result['reason']}")
            break
        signals.extend(sense_linkedin_jobs(
            db, tenant_id,
            title_search=profile["title_search"],
            location_search=profile.get("location_search") or APIFY_DEFAULT_LOCATION_SEARCH,
            organization_employees_gte=profile["employee_min"],
            organization_employees_lte=profile["employee_max"],
            industry_filter=profile.get("industry_filter") or APIFY_INDUSTRY_FILTER,
            time_range=profile.get("time_range") or "7d",
            limit=limit,
        ))

    # Only a total block is a skip. If any profile ran, this is a real (partial) success -- the
    # signals it produced are just as real as a full pass, and reporting it as "skipped" would
    # discard them from the run record.
    if not signals and blocked_reasons:
        raise SourceBudgetBlocked("; ".join(blocked_reasons))
    return signals


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


# Concentrate the engagement-harvest budget on the FEW real posts most likely to have real
# comments, not spread thin across every post a phrase search returns -- see the real
# "engagement prioritization" comment below for why. Small and explicit, same convention as
# every other per-tick cap in this codebase.
MAX_POSTS_FOR_ENGAGEMENT_HARVEST = 5


def _run_linkedin_post_search(db: Session, tenant_id: int, budget_tenant_id: int | None = None):
    # No MissingSourceConfiguration needed -- linkedin_search_config.py always has a derived
    # default (computed from live ICP/offering/business-context config) even with zero saved
    # overrides, same "empty config is a valid state" reasoning as HN/RSS above. Rate-limited
    # internally (select_due_phrases) -- may legitimately return [] on a tick where every
    # configured phrase was searched too recently to search again.
    #
    # BUDGET GATE ADDED 2026-09-19. This is a real paid Apify path (Google organic search per
    # phrase, then supreme_coder/linkedin-post per author) and it was the one paid sensing
    # source with NO budget check at all, while _run_linkedin_jobs, _run_web_search_trends and
    # _run_competitor_content all had one. It therefore spent against the shared Apify account
    # on every tick while every other source believed the daily cap was intact -- which is
    # exactly the accounting hole that makes spend unpredictable.
    #
    # Worst case is bounded by the same config the sensing call uses, so the estimate matches
    # what will actually be bought rather than being a guess.
    #
    # budget_tenant_id ADDED 2026-09-22, real gap found testing the "majji" partner tenant: this
    # source is now called for partner tenants too (see main.py's partner_daily_run_tick), and a
    # partner tenant has no gtm_os_control_config of its own -- check_apify_budget would read
    # None/None and correctly, but silently, block forever (fails closed, "None is never
    # unlimited" -- never an accidental spend, just a permanent no-op nobody would notice).
    # "Our key, our cost, not the partner's tenant": the SAME rule run_apify_discovery's own
    # budget_tenant_id parameter and partner_pipeline.py's enrichment_tenant_id already follow
    # for every other partner-facing paid call in this codebase. The API KEY lookup
    # (_get_apify_api_key, inside sense_linkedin_post_search) already falls back to Elephant
    # Edge's own credential automatically -- only the SPEND CAP check needed this fix.
    from app.apify_budget_guard import STATUS_ALLOWED, check_apify_budget
    from app.apify_client import GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD, LINKEDIN_POST_COST_PER_POST_USD
    from app.gtm_os.intelligence.linkedin_search_config import get_linkedin_search_config

    search_config = get_linkedin_search_config(db, tenant_id)
    max_phrases = int(search_config.get("max_phrases_per_cycle") or 8)
    posts_per_phrase = int(search_config.get("posts_per_phrase") or 10)
    worst_case_usd = (
        max_phrases * GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD
        + max_phrases * posts_per_phrase * LINKEDIN_POST_COST_PER_POST_USD
    )

    budget_result = check_apify_budget(db, budget_tenant_id or tenant_id, worst_case_usd, operation="linkedin_post_search")
    if budget_result["status"] != STATUS_ALLOWED:
        raise SourceBudgetBlocked(budget_result["reason"])

    signals = sense_linkedin_post_search(db, tenant_id)

    # ENGAGEMENT MINING ADDED 2026-09-21, off by default -- new, unvalidated (see
    # engagement_intent.py's own honesty note), so it must not silently start spending for a
    # tenant that never opted in. Chains off THESE SAME posts rather than searching again --
    # see sense_linkedin_post_engagement's own docstring for why a second search would double-
    # pay Apify for scraping the same real-world posts. A blocked/skipped engagement step never
    # fails the whole source: the post-search half already succeeded and its signals are real
    # regardless of whether the extra engagement harvest could also run today.
    if search_config.get("engagement_mining_enabled"):
        from app.apify_client import LINKEDIN_ENGAGEMENT_COST_PER_ENGAGER_USD
        from app.gtm_os.intelligence.sensing import DEFAULT_ENGAGERS_PER_SEARCH_TICK, sense_linkedin_post_engagement

        # ENGAGEMENT PRIORITIZATION ADDED 2026-09-22, real user feedback after the first live
        # test: a post found by phrase search can have near-zero real engagement (confirmed --
        # the actual post majji's first test harvested had numComments=1, numLikes=0, so there
        # was almost nothing there TO find). The actor bills maxResults as a SHARED cap across
        # every post_url passed in, so spreading it evenly across many low-engagement posts
        # wastes most of the budget on posts with nothing to harvest. Concentrating the same
        # budget on the posts that actually have comments/likes/shares is strictly better yield
        # per dollar, and the data is already free -- numLikes/numComments/numShares are already
        # present in raw_evidence, a byproduct of the post-search call already paid for; nothing
        # new is bought to compute this.
        # INTERNAL-HIRING FILTER + ENGAGEMENT RANKING moved into select_relevant_post_urls()
        # (engagement_intent.py, 2026-09-22) so majji's own controlled daily engagement-mining
        # run (partner_daily_run.py) can reuse the exact same real filtering instead of a second,
        # drifting copy -- see that function's own docstring.
        from app.gtm_os.intelligence.engagement_intent import select_relevant_post_urls

        post_urls = select_relevant_post_urls(signals, MAX_POSTS_FOR_ENGAGEMENT_HARVEST)
        if post_urls:
            engagement_budget = check_apify_budget(
                db, budget_tenant_id or tenant_id, DEFAULT_ENGAGERS_PER_SEARCH_TICK * LINKEDIN_ENGAGEMENT_COST_PER_ENGAGER_USD,
                operation="linkedin_engagement",
            )
            if engagement_budget["status"] == STATUS_ALLOWED:
                signals = signals + sense_linkedin_post_engagement(db, tenant_id, post_urls, budget_tenant_id=budget_tenant_id)

    return signals


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

    due, cadence_reason = _source_cadence_due(db, tenant_id, "web_search_trend", CONTENT_SENSING_CADENCE_DAYS)
    if not due:
        raise SourceNotDue(cadence_reason)

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

    due, cadence_reason = _source_cadence_due(db, tenant_id, "competitor_content", CONTENT_SENSING_CADENCE_DAYS)
    if not due:
        raise SourceNotDue(cadence_reason)

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



# Every sensing source runs INDEPENDENTLY and CONCURRENTLY (2026-08-31).
#
# The old loop ran sources one after another on a single shared session, so a source that hung
# rather than failed froze every source behind it. Confirmed live in run 116: linkedin_job
# finished at 08:28:51, linkedin_reply (SalesRobot) then hung, and linkedin_post_search --
# which sits after it in the list -- never ran at all. Jobs and posts are two independent ways
# of finding accounts, each with its own logic, and each is supposed to contribute to the daily
# target on its own. Neither may depend on the other completing, or even succeeding.
#
# Each source gets its OWN database session because SQLAlchemy sessions are not thread-safe, and
# a hard timeout so a hanging provider can cost at most that long instead of the whole run.
SOURCE_TIMEOUT_SECONDS = 180
SOURCE_MAX_WORKERS = 6


def _run_one_source(name: str, runner, tenant_id: int) -> dict:
    """Runs a single source on its own session and returns a plain, session-free result dict.

    Returns counts rather than ORM objects on purpose: the objects belong to a session this
    function closes, so handing them back would hand back rows that raise on attribute access.
    """
    db = SessionLocal()
    try:
        signals = runner(db, tenant_id)
        return {"name": name, "status": "succeeded", "signals_created": len(signals)}
    except MissingSourceConfiguration as e:
        return {"name": name, "status": "skipped", "reason": str(e)}
    except SourceBudgetBlocked as e:
        return {"name": name, "status": "skipped", "reason": str(e), "budget": True}
    except SourceNotDue as e:
        return {"name": name, "status": "skipped", "reason": f"not due: {e}"}
    except Exception as e:  # noqa: BLE001 -- one source's failure must never affect another
        db.rollback()
        return {"name": name, "status": "failed", "error": str(e)}
    finally:
        db.close()


def _dry_run_source_status(db: Session, tenant_id: int, name: str) -> dict:
    if name == "linkedin_reply":
        if _get_salesrobot_config(db, tenant_id) is None:
            return {"status": "would_skip", "reason": "missing SalesRobot configuration for this tenant"}
        return {"status": "would_run"}
    # The weekly content channels are skipped by cadence in a real run -- this preview has to say
    # so, or dry_run reports "would_run" for sources that will not actually run (2026-08-31).
    if name in CONTENT_CADENCE_SOURCES:
        due, reason = _source_cadence_due(db, tenant_id, name, CONTENT_SENSING_CADENCE_DAYS)
        if not due:
            return {"status": "would_skip", "reason": f"not due: {reason}"}
    return {"status": "would_run"}


def _report_progress(db: Session, run, result: dict, current_stage: str) -> None:
    """Real fix, 2026-09-18: GtmIntelligenceRun.stage_results was only ever written once, at the
    very end of the whole (potentially 60-100+ minute) sweep -- so a live GET on the run while it
    was still in progress showed nothing but "running", with no way to see which stage it was
    actually on. Confirmed live: this was the direct reason a stalled/slow stage looked identical
    to a genuinely hung one from the outside, and the only way to tell them apart was ad-hoc
    manual DB queries against unrelated tables. `run` is optional (None for any caller that
    doesn't have a persisted run row, e.g. a future direct unit-test call) so this is purely
    additive -- never required, never changes what the function returns."""
    if run is None:
        return
    snapshot = dict(result)
    snapshot["current_stage"] = current_stage
    run.stage_results = snapshot
    db.add(run)
    try:
        db.commit()
    except Exception:  # noqa: BLE001 -- confirmed live: the sweep's own long-lived `db` session
        # can go stale on Neon after sitting idle through a stage's real API calls, the exact
        # same class of bug already fixed for every OTHER call site that touches this session --
        # this one was missed because it's brand new. A progress-reporting commit failing must
        # never crash the whole sweep -- rollback and retry once; if it fails twice, silently
        # skip this one snapshot rather than losing the run entirely over a visibility feature.
        db.rollback()
        try:
            run.stage_results = snapshot
            db.add(run)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()


def run_gtm_intelligence_sweep(
    db: Session,
    tenant_id: int,
    sources: list[str] | None = None,
    dry_run: bool = False,
    run=None,
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

    runnable = [(name, runner) for name, runner in SWEEPABLE_SOURCES if name in selected]
    for name, _runner in SWEEPABLE_SOURCES:
        if name not in selected:
            result["sources"][name] = {"status": "skipped", "reason": "not selected"}

    # NOT a `with` block, deliberately: ThreadPoolExecutor's context manager calls
    # shutdown(wait=True) on exit, which blocks until every worker finishes -- including the very
    # hanging source this timeout exists to survive. Caught by testing a deliberately hanging
    # source: the timeout fired correctly and the sweep then froze anyway on pool exit.
    # shutdown(wait=False) lets the sweep continue; an orphaned worker holds only its own session
    # and dies with the process.
    pool = ThreadPoolExecutor(max_workers=SOURCE_MAX_WORKERS)
    try:
        futures = {pool.submit(_run_one_source, name, runner, tenant_id): name for name, runner in runnable}
        logger.info("gtm_intelligence_sweep: sensing %d sources concurrently (tenant_id=%s)", len(futures), tenant_id)
        for future in as_completed(futures, timeout=SOURCE_TIMEOUT_SECONDS * 2):
            name = futures[future]
            try:
                outcome = future.result(timeout=SOURCE_TIMEOUT_SECONDS)
            except FuturesTimeout:
                # A hanging provider is reported as a real failure for THAT source only. Every
                # other source has already run independently -- this is exactly the case that
                # used to freeze the whole sweep.
                result["sources"][name] = {"status": "failed", "error": f"timed out after {SOURCE_TIMEOUT_SECONDS}s"}
                any_failed = True
                logger.error("gtm_intelligence_sweep: sensing %s timed out after %ss", name, SOURCE_TIMEOUT_SECONDS)
                continue
            status = outcome["status"]
            entry = {k: v for k, v in outcome.items() if k not in ("name", "budget")}
            result["sources"][name] = entry
            if status == "succeeded":
                any_succeeded = True
                logger.info("gtm_intelligence_sweep: sensing %s succeeded (%d signals)", name, outcome.get("signals_created", 0))
            elif status == "failed":
                any_failed = True
                logger.error("gtm_intelligence_sweep: sensing %s failed -- %s", name, outcome.get("error"))
            else:
                logger.info("gtm_intelligence_sweep: sensing %s skipped -- %s", name, outcome.get("reason"))
    except FuturesTimeout:
        # as_completed's own overall deadline -- whatever has not reported by now is recorded as
        # timed out, rather than leaving those sources silently absent from the run record.
        for pending_name in {n for f, n in futures.items() if not f.done()}:
            result["sources"][pending_name] = {"status": "failed", "error": f"timed out after {SOURCE_TIMEOUT_SECONDS}s"}
            any_failed = True
            logger.error("gtm_intelligence_sweep: sensing %s timed out (overall deadline)", pending_name)
    finally:
        pool.shutdown(wait=False)
    _report_progress(db, run, result, "sensing")

    # Autonomous Sensing Phase S7 (app/gtm_os/intelligence/investigation_cycle.py) -- runs BEFORE
    # interpretation/problem/demand below so any GtmSignal rows S5 execution created this same
    # tick get picked up by the SAME existing interpretation pass, not a following one. Bounded
    # per control.investigation.max_objectives_per_tick; S2 gap-identification itself always runs
    # (free/local), S3-S6 are skipped with an explicit configuration_required status while that
    # cap is unconfigured. Never raises -- same per-stage error isolation as every other stage here.
    try:
        # Real, bounded timeout (2026-09-17), recalibrated 2026-09-18: originally sized for
        # max_objectives_per_tick=2 (2 objectives x ~60s Apify calls each). That cap was raised
        # to 10 the same day to fix a real queue-starvation bug (budget-blocked objectives never
        # aged, permanently jamming the front of the queue) -- but this timeout was never
        # recalculated against the new cap, so a genuinely busy tick (10 real Apify calls) could
        # need up to 600s, not fit in 300s. Confirmed live today's total sweep latency stacks
        # every stage's timeout sequentially, so a larger max_objectives_per_tick multiplies
        # straight into total run time. Rebalanced together: cap lowered to 5 (still 2.5x the
        # original crippling 2) so this 300s budget is proportionate again, instead of raising
        # the timeout further and making total run time even longer.
        investigation_result = _run_stage_with_timeout(run_investigation_cycle, tenant_id, timeout_seconds=300)
        result["investigation_cycle"] = investigation_result
        _report_progress(db, run, result, "investigation_cycle")
        if investigation_result.get("status") in ("succeeded", "partial"):
            any_succeeded = True
        if investigation_result.get("status") in ("partial", "timed_out"):
            any_failed = True
        logger.info("gtm_intelligence_sweep: investigation_cycle %s", investigation_result.get("status"))
    except Exception as e:  # noqa: BLE001 -- see module docstring
        db.rollback()  # 2026-08-26, real fix -- see the ACCOUNT_STRATEGY_STAGES loop's own comment for the full explanation
        result["investigation_cycle"] = {"status": "failed", "error": str(e)}
        any_failed = True
        logger.error("gtm_intelligence_sweep: investigation_cycle failed -- %s", e)

    try:
        # Real, confirmed live bug (2026-09-17, run 129): "SSL connection has been closed
        # unexpectedly" -- by the time this stage runs, the sweep's own long-lived `db` session
        # has usually already been idle through sensing's real network calls plus
        # investigation_cycle's own up-to-300s window, long enough for Neon's pooled endpoint to
        # drop it mid-query. pool_pre_ping only re-validates a connection at CHECKOUT, not while
        # a query is actively running against one that goes stale in between -- so it does not
        # catch this. Same fix as investigation_cycle: give this stage its own FRESH session via
        # _run_stage_with_timeout (a new session is never stale), instead of reusing the sweep's.
        def _run_interpretation_stage(stage_db, stage_tenant_id):
            interpreted = run_interpretation_sweep(stage_db, stage_tenant_id, sources=ALL_INTERPRETED_SOURCES)
            # Cross-sweep concurrent-hiring promotion (2026-08-31): interpretation's own promotion
            # only ever counted postings within one batch, so a company whose concurrent postings
            # arrived on different days stayed at dead-end "hiring_activity". Measured against real
            # data this recovered 14 companies / 32 signals on first run. Runs in the same stage --
            # it is part of interpreting job signals, not a separate one.
            promotion = promote_concurrent_hiring_across_sweeps(stage_db, stage_tenant_id)
            return {"status": "succeeded", "created": len(interpreted), "concurrent_hiring_promotion": promotion}

        # Real, confirmed live in run 130 (2026-09-17): this is not a hang -- there is a genuine
        # 237-signal backlog (linkedin_post/linkedin_job/linkedin_reply) that has been building up
        # because past runs kept getting cut off before finishing. Each signal costs one LLM call,
        # so 180s wasn't enough to clear even one run's worth. Widened to 600s (10min) as a one-time
        # catch-up budget so a single run can actually drain the backlog instead of perpetually
        # timing out on the same signals every day. Safe to run this long: it's a fresh background
        # thread on its own session, not something blocking an HTTP connection.
        #
        # Recalibrated 2026-09-18: the 600s figure was sized when EVERY linkedin_job signal also
        # cost one unconditional LLM call (since fixed with a deterministic classifier -- see
        # linkedin_job_interpretation.py) and Gemini was quota-exhausted, forcing slow Claude
        # fallback calls. Neither is true anymore: job signals are now free/instant, and a live
        # test today confirmed Gemini responds in ~1-2s per call. Confirmed live that stacking
        # every stage's generous timeout is the actual reason full runs take 60-100+ minutes even
        # when nothing is actually stuck -- lowering this is a direct fix for that, not just a
        # guess; if 240s genuinely isn't enough some day, that's itself a signal something is
        # newly slow and worth investigating, not a reason to keep a blanket 600s "just in case."
        interpretation_result = _run_stage_with_timeout(_run_interpretation_stage, tenant_id, timeout_seconds=240)
        result["interpretation"] = interpretation_result
        _report_progress(db, run, result, "interpretation")
        if interpretation_result.get("status") == "succeeded":
            any_succeeded = True
        else:
            any_failed = True
        logger.info("gtm_intelligence_sweep: interpretation %s", interpretation_result.get("status"))
    except Exception as e:  # noqa: BLE001 -- see module docstring
        db.rollback()  # 2026-08-26, real fix -- see the ACCOUNT_STRATEGY_STAGES loop's own comment for the full explanation
        result["interpretation"] = {"status": "failed", "error": str(e)}
        any_failed = True
        logger.error("gtm_intelligence_sweep: interpretation failed -- %s", e)

    try:
        # Same fresh-session fix as interpretation above -- confirmed live in run 130: this exact
        # stage hit "SSL connection has been closed unexpectedly" on the sweep's long-lived `db`
        # session (stale after sensing + investigation_cycle + interpretation's own time window).
        def _run_problem_detection_stage(stage_db, stage_tenant_id):
            problems = run_problem_hypothesis_sweep(stage_db, stage_tenant_id, sources=ALL_INTERPRETED_SOURCES)
            return {"status": "succeeded", "hypotheses_touched": len(problems)}

        problem_result = _run_stage_with_timeout(_run_problem_detection_stage, tenant_id, timeout_seconds=180)
        result["problem_detection"] = problem_result
        _report_progress(db, run, result, "problem_detection")
        if problem_result.get("status") == "succeeded":
            any_succeeded = True
        else:
            any_failed = True
        logger.info("gtm_intelligence_sweep: problem detection %s", problem_result.get("status"))
    except Exception as e:  # noqa: BLE001 -- see module docstring
        db.rollback()  # 2026-08-26, real fix -- see the ACCOUNT_STRATEGY_STAGES loop's own comment for the full explanation
        result["problem_detection"] = {"status": "failed", "error": str(e)}
        any_failed = True
        logger.error("gtm_intelligence_sweep: problem detection failed -- %s", e)

    try:
        # Same fresh-session fix -- same staleness risk applies here by the time this stage runs.
        def _run_demand_detection_stage(stage_db, stage_tenant_id):
            demands = run_demand_hypothesis_sweep(stage_db, stage_tenant_id, sources=ALL_INTERPRETED_SOURCES)
            return {"status": "succeeded", "hypotheses_touched": len(demands)}

        demand_result = _run_stage_with_timeout(_run_demand_detection_stage, tenant_id, timeout_seconds=180)
        result["demand_detection"] = demand_result
        _report_progress(db, run, result, "demand_detection")
        if demand_result.get("status") == "succeeded":
            any_succeeded = True
        else:
            any_failed = True
        logger.info("gtm_intelligence_sweep: demand detection %s", demand_result.get("status"))
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
        # 2026-09-17: _run_stage_with_retry (rollback + one retry) replaces the old bare
        # try/except here -- confirmed live in run 131 that whichever stage runs right after the
        # sweep's shared session goes stale on a long idle gap eats an avoidable failure otherwise.
        stage_result = _run_stage_with_retry(runner, db, tenant_id)
        result[stage_key] = stage_result
        _report_progress(db, run, result, stage_key)
        if stage_result.get("status") == "succeeded":
            any_succeeded = True
            logger.info("gtm_intelligence_sweep: %s succeeded -- %s", stage_key, stage_result)
        else:
            any_failed = True
            logger.error("gtm_intelligence_sweep: %s failed -- %s", stage_key, stage_result.get("error"))

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
        stage_result = _run_stage_with_retry(runner, db, tenant_id)
        result[stage_key] = stage_result
        _report_progress(db, run, result, stage_key)
        if stage_result.get("status") == "succeeded":
            any_succeeded = True
            logger.info("gtm_intelligence_sweep: %s succeeded -- %s", stage_key, stage_result)
        else:
            any_failed = True
            logger.error("gtm_intelligence_sweep: %s failed -- %s", stage_key, stage_result.get("error"))

    # V2-owned contact discovery (Phase 3/4, app/gtm_os/sales/contact_discovery.py) -- see
    # ACCOUNT_STRATEGY_STAGES_POST_CONTACT's own comment above for why this sits here, outside
    # the generic loop, with its own explicit succeeded/skipped/failed handling. Outbound-gated
    # (S7) -- see comment above the gtm_strategy loop.
    if not outbound_due:
        contact_discovery_result = {"status": "skipped", "reason": outbound_reason}
    else:
        # 2026-09-17 fix: real bug (2026-08-26) was a raise here killing the whole remaining
        # sweep -- fixed with a bare try/except+retry, which itself hung forever in run 136 (no
        # timeout at all on a direct call against the shared session). Now goes through
        # _run_stage_with_retry -- fresh session + real hard timeout on every attempt, one retry
        # on a genuine failure, no retry on a timeout (see that function's own docstring).
        contact_discovery_result = _run_stage_with_retry(
            lambda stage_db, stage_tenant_id: run_v2_contact_discovery_sweep(stage_db, stage_tenant_id, limit=50),
            db, tenant_id,
        )
    result["contact_discovery"] = contact_discovery_result
    _report_progress(db, run, result, "contact_discovery")
    if contact_discovery_result.get("status") == "succeeded":
        any_succeeded = True
    elif contact_discovery_result.get("status") == "failed":
        any_failed = True
        logger.error("gtm_intelligence_sweep: contact_discovery failed -- %s", contact_discovery_result.get("error"))

    for stage_key, runner in ACCOUNT_STRATEGY_STAGES_CONTACT_TO_MESSAGE:
        if not outbound_due:
            result[stage_key] = {"status": "skipped", "reason": outbound_reason}
            continue
        stage_result = _run_stage_with_retry(runner, db, tenant_id)
        result[stage_key] = stage_result
        _report_progress(db, run, result, stage_key)
        if stage_result.get("status") == "succeeded":
            any_succeeded = True
            logger.info("gtm_intelligence_sweep: %s succeeded -- %s", stage_key, stage_result)
        else:
            any_failed = True
            logger.error("gtm_intelligence_sweep: %s failed -- %s", stage_key, stage_result.get("error"))

    # V2-owned send (Phase 7, app/gtm_os/send/send.py) -- see ACCOUNT_STRATEGY_STAGES_POST_SEND's
    # own comment above for why this sits here, outside the generic loop, with its own explicit
    # succeeded/skipped/failed handling. Outbound-gated (S7).
    if not outbound_due:
        send_result = {"status": "skipped", "reason": outbound_reason}
    else:
        # 2026-09-17 fix: same as contact_discovery above -- goes through _run_stage_with_retry
        # (fresh session + real hard timeout per attempt) instead of a bare try/except+retry with
        # no timeout at all, which is exactly the class of bug that froze run 136 for 13+ hours.
        send_result = _run_stage_with_retry(
            lambda stage_db, stage_tenant_id: run_v2_send_sweep(stage_db, stage_tenant_id, limit=50),
            db, tenant_id,
        )
    result["send"] = send_result
    _report_progress(db, run, result, "send")
    if send_result.get("status") == "succeeded":
        any_succeeded = True
    elif send_result.get("status") == "failed":
        any_failed = True
        logger.error("gtm_intelligence_sweep: send failed -- %s", send_result.get("error"))

    for stage_key, runner in ACCOUNT_STRATEGY_STAGES_POST_SEND:
        stage_result = _run_stage_with_retry(runner, db, tenant_id)
        result[stage_key] = stage_result
        _report_progress(db, run, result, stage_key)
        if stage_result.get("status") == "succeeded":
            any_succeeded = True
            logger.info("gtm_intelligence_sweep: %s succeeded -- %s", stage_key, stage_result)
        else:
            any_failed = True
            logger.error("gtm_intelligence_sweep: %s failed -- %s", stage_key, stage_result.get("error"))

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
    _report_progress(db, run, result, "outreach_sequencing")
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


def _budget_exhausted(result: dict) -> bool:
    """True when this iteration was stopped by a spend ceiling rather than by a lack of work.

    Distinguishing the two matters: "no work left" is a satisfied pipeline, "budget gone" is a
    stalled one, and they need different responses (stop for today vs. raise the cap or wait for
    the reset). Reported separately so a run's stop_reason names the real cause instead of every
    unproductive day looking identical.

    Deliberately broad about WHERE the block came from -- an investigation objective refused for
    budget, or a sensing source raising SourceBudgetBlocked -- because from the loop's point of
    view they mean the same thing: another iteration cannot buy anything."""
    cycle = result.get("investigation_cycle") or {}
    objectives = cycle.get("results") or []
    if objectives and all(
        str(r.get("exec_status") or "") == "blocked_by_budget" for r in objectives if r.get("exec_status")
    ):
        return True

    for source in (result.get("sources") or {}).values():
        if isinstance(source, dict) and "budget" in str(source.get("reason") or "").lower():
            if source.get("status") not in ("succeeded",):
                return True
    return False


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
    cycle = result.get("investigation_cycle") or {}
    # Real fix 2026-09-19: this used to read objectives_PROCESSED, which counts an objective that
    # was refused outright (budget gone, source disabled, credentials missing, control plane
    # halted) exactly the same as one that did real work. A fully budget-blocked tick therefore
    # reported "work happened", this returned False, and run_gtm_daily_flow_cycle started another
    # full iteration -- re-running every stage and re-paying for whatever was not blocked -- until
    # the iteration ceiling. That is the mechanism behind "we set a target, never reach it, and
    # keep spending". Confirmed live in run 150: its single objective came back blocked_by_budget
    # and the run still completed 2 iterations producing nothing.
    #
    # objectives_advanced is blocked-exclusive. .get() with a fallback to objectives_processed
    # keeps this correct against an older cycle result that predates the new key.
    objectives_advanced = cycle.get("objectives_advanced", cycle.get("objectives_processed", 0))
    interpretation_created = (result.get("interpretation") or {}).get("created", 0)
    return total_new_signals == 0 and objectives_advanced == 0 and interpretation_created == 0


def run_gtm_daily_flow_cycle(db: Session, tenant_id: int, run=None) -> dict:
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
        # Silent degradation is the problem here, not the fallback itself. "None never means
        # unlimited" is the right discipline, but the CONSEQUENCE was invisible: an unset
        # flow_target quietly turned a target-seeking daily run into a single pass, and an unset
        # investigation.max_objectives_per_tick separately skips S3-S6 entirely -- so a run could
        # complete "successfully" having done almost nothing, look identical to a healthy run,
        # and leave the target unexplained. Runs 147/149/150 all presented exactly that way.
        #
        # Behavior is unchanged (still one pass); it now says so in the result, so a run that did
        # not even try to reach a target is distinguishable from one that tried and failed.
        missing = [
            f"flow_target.{key}" for key, value in
            (("daily_flow_target", daily_flow_target), ("max_iterations_per_run", max_iterations_per_run))
            if not value
        ]
        logger.warning(
            "run_gtm_daily_flow_cycle: no target loop for tenant %s -- unconfigured: %s. "
            "Running a single sweep pass.", tenant_id, ", ".join(missing),
        )
        single = run_gtm_intelligence_sweep(db, tenant_id, run=run)
        single["flow_target"] = {
            "status": "not_configured",
            "unconfigured_keys": missing,
            "iterations_run": 1,
            "stop_reason": "flow_target_not_configured",
        }
        return single

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

        result = run_gtm_intelligence_sweep(db, tenant_id, run=run)
        iterations_run += 1
        if run is not None:
            # 2026-09-18: also surface which flow-cycle iteration this is, not just which stage
            # within it -- a live GET now shows both "iteration 3 of 5" and "currently on
            # interpretation" instead of only the latter. Same stale-connection retry as
            # _report_progress -- a visibility-only commit must never crash the whole cycle.
            snapshot = dict(result)
            snapshot["flow_cycle_iteration"] = iterations_run
            try:
                run.stage_results = snapshot
                db.add(run)
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                try:
                    run.stage_results = snapshot
                    db.add(run)
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()
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
        # `now` pinned, matching the count_at_run_start call above and the final count below.
        # Unpinned, this re-derived "today" on every iteration against the tenant's local
        # business-hours timezone: a run crossing local midnight would compare its progress
        # against a freshly reset window, so current_count collapsed to ~0, the target became
        # unreachable, and the loop ran to iteration_ceiling_reached instead of stopping. The
        # daily sweep fires at a fixed UTC hour and iterations can each run to ~89 minutes, so
        # crossing local midnight is an ordinary occurrence, not an edge case.
        current_count = count_completed_flows_today(db, tenant_id, now)

        if current_count >= daily_flow_target:
            stop_reason = "target_reached"
            break
        if iterations_run >= max_iterations_per_run:
            stop_reason = "iteration_ceiling_reached"
            break
        if _budget_exhausted(result):
            # Checked BEFORE _no_eligible_work_remaining so the stop reason names the real
            # cause. Another iteration under an exhausted provider budget cannot produce a flow
            # -- every paid step is refused -- but it still re-runs every unpaid stage and
            # re-pays for anything not covered by the exhausted budget. Stopping here is what
            # turns "kept burning to the iteration ceiling" into a single legible outcome.
            stop_reason = "budget_exhausted"
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
