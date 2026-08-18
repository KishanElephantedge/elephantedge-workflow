"""The GTM-OS intelligence orchestrator -- Step 13A, extended by Batch 2 (Step 16 Batch 2 Part
A/B). Coordinates existing, unmodified sensing/interpretation/detection/content-intelligence
functions; contains zero intelligence logic of its own -- every stage below is a plain call into
an already-built, already-tested sweep function from elsewhere in app/gtm_os.

Two independent branches, both rooted in the same raw GtmSignal sensing stage, per Batch 2's own
architecture diagram -- neither branch reads the other's output:

    SOURCE SENSING (sense_theirstack_jobs / sense_linkedin_replies / sense_hackernews_stories /
                     sense_rss_articles -- independent per source, own failure boundary each)
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
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, JSON, String
from sqlalchemy.orm import Session

from app.db.models import Base, Parameter
from app.gtm_os.content.candidate_extraction import run_candidate_extraction_sweep
from app.gtm_os.content.candidate_normalization import run_candidate_normalization_sweep
from app.gtm_os.content.promotion import run_candidate_promotion_sweep
from app.gtm_os.content.topic_linking import run_content_topic_linking_sweep
from app.gtm_os.content.trend_intelligence import run_trend_intelligence_sweep
from app.gtm_os.icp.icp_matching import run_icp_matching_sweep
from app.gtm_os.intelligence.demand_detection import run_demand_hypothesis_sweep
from app.gtm_os.intelligence.interpretation import run_interpretation_sweep
from app.gtm_os.intelligence.problem_detection import run_problem_hypothesis_sweep
from app.gtm_os.intelligence.sensing import (
    sense_hackernews_stories,
    sense_linkedin_replies,
    sense_rss_articles,
    sense_theirstack_jobs,
)
from app.gtm_os.learning.message_draft import run_message_generation_sweep
from app.gtm_os.learning.outcome import run_outcome_detection_sweep
from app.gtm_os.opportunity.opportunity import run_opportunity_intelligence_sweep
from app.gtm_os.sales.sales_agent import run_sales_agent_sweep
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


def finish_gtm_intelligence_run(db: Session, run: GtmIntelligenceRun, result: dict) -> GtmIntelligenceRun:
    failed_stages = [k for k, v in result.items() if isinstance(v, dict) and v.get("status") == "failed"]
    run.status = result.get("status", "completed")
    run.stage_results = result
    run.error_summary = f"{len(failed_stages)} stage(s) failed: {', '.join(failed_stages)}" if failed_stages else None
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

# Content Intelligence sweep stages, run in this exact order after sensing -- each stage only
# ever consumes the previous content-branch stage's output (or raw GtmSignal for the first one),
# never InterpretedSignal/ProblemHypothesis/DemandHypothesis. (stage_key, runner(db, tenant_id) -> dict).
CONTENT_INTELLIGENCE_STAGES: list[tuple[str, callable]] = [
    ("topic_linking", lambda db, tenant_id: run_content_topic_linking_sweep(db, tenant_id)),
    ("candidate_extraction", lambda db, tenant_id: run_candidate_extraction_sweep(db, tenant_id)),
    ("candidate_normalization", lambda db, tenant_id: run_candidate_normalization_sweep(db, tenant_id)),
    ("candidate_promotion", lambda db, tenant_id: run_candidate_promotion_sweep(db, tenant_id)),
    ("trend_intelligence", lambda db, tenant_id: run_trend_intelligence_sweep(db, tenant_id)),
]

# Account/Strategy/Sales branch (Batch 6 Part L) -- runs AFTER problem_detection/demand_detection
# above (it reads DemandHypothesis, which that stage produces), but is still independently
# failure-isolated: a failure here never touches the Problem/Demand or Content Intelligence
# branches, and vice versa. Every stage here is a pure/idempotent read-or-additive-insert sweep
# (see opportunity.py/strategy.py/sales_agent.py's own docstrings) -- zero LLM calls, zero
# external API calls, zero CRM/outbound writes, safe to run on every cycle even with near-zero
# real data (each stage safely returns all-zero counts rather than fabricating output).
ACCOUNT_STRATEGY_STAGES: list[tuple[str, callable]] = [
    ("opportunity", lambda db, tenant_id: run_opportunity_intelligence_sweep(db, tenant_id)),
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
    # Message generation (message_draft.py) -- makes a real LLM call per eligible Opportunity, so
    # placed after strategy/before the cheap read-only stages below and bounded by its own small
    # `limit` (see message_draft.py's own cost-containment note) rather than left permanently
    # uncalled. Every draft this produces stops at status="draft"/"ready_for_review" --
    # approve_message_draft() is a SEPARATE, human-only action (app/routes/api.py's
    # POST /gtm-os/messages/{id}/review), never invoked by this sweep or anything it calls. This
    # is the human-approval boundary the whole autonomous cycle stops at.
    ("message_generation", lambda db, tenant_id: run_message_generation_sweep(db, tenant_id, limit=20)),
    ("sales_readiness", lambda db, tenant_id: run_sales_agent_sweep(db, tenant_id)),
    # Batch 7 -- outcome detection reuses existing linkedin_reply InterpretedSignal rows only
    # (zero LLM/external calls, see outcome.py).
    ("outcome_detection", lambda db, tenant_id: run_outcome_detection_sweep(db, tenant_id)),
]

# The complete, CURRENT set of sources the interpretation/detection layers know how to handle
# (interpretation.py::_INTERPRETERS, kept in sync manually -- see module docstring for why this
# exists instead of trusting either sweep function's own default).
ALL_INTERPRETED_SOURCES = ["linkedin_job", "theirstack_job", "linkedin_post", "linkedin_reply"]


class MissingSourceConfiguration(Exception):
    """Raised by a source wrapper (never by the sweep loop itself) when required tenant
    configuration isn't set -- caught and reported as a "skipped" source, never a crash."""


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


def _run_theirstack(db: Session, tenant_id: int):
    # No new search criteria invented -- uses sense_theirstack_jobs' own existing defaults
    # exactly, per the Step 13 spec's explicit instruction.
    return sense_theirstack_jobs(db, tenant_id)


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


# Source registration -- (name, runner(db, tenant_id) -> list[GtmSignal]). A future source
# (Reddit/X/YouTube/etc., none added in this step) means adding one entry here; the sweep loop
# below never branches on source name (Step 13 design doc §14). hackernews_story/rss_article
# added in Batch 2 -- both were built (Step 16C/16D) and tested but never wired into any
# recurring sweep until now; Content Intelligence's own stages (CONTENT_INTELLIGENCE_STAGES)
# have nothing real to process without them.
SWEEPABLE_SOURCES: list[tuple[str, callable]] = [
    ("theirstack_job", _run_theirstack),
    ("linkedin_reply", _run_linkedin_replies),
    ("hackernews_story", _run_hackernews),
    ("rss_article", _run_rss),
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
        "sources": {},
        "interpretation": {},
        "problem_detection": {},
        "demand_detection": {},
        "topic_linking": {},
        "candidate_extraction": {},
        "candidate_normalization": {},
        "candidate_promotion": {},
        "trend_intelligence": {},
        "opportunity": {},
        "icp_matching": {},
        "gtm_strategy": {},
        "message_generation": {},
        "sales_readiness": {},
        "outcome_detection": {},
    }

    if dry_run:
        for name, _runner in SWEEPABLE_SOURCES:
            if name not in selected:
                result["sources"][name] = {"status": "skipped", "reason": "not selected"}
                continue
            result["sources"][name] = _dry_run_source_status(db, tenant_id, name)
        result["interpretation"] = {"status": "would_run", "sources": ALL_INTERPRETED_SOURCES}
        result["problem_detection"] = {"status": "would_run", "sources": ALL_INTERPRETED_SOURCES}
        result["demand_detection"] = {"status": "would_run", "sources": ALL_INTERPRETED_SOURCES}
        for stage_key, _runner in CONTENT_INTELLIGENCE_STAGES:
            result[stage_key] = {"status": "would_run"}
        for stage_key, _runner in ACCOUNT_STRATEGY_STAGES:
            result[stage_key] = {"status": "would_run"}
        return result

    any_succeeded = False
    any_failed = False

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
        except Exception as e:  # noqa: BLE001 -- one source's failure must never block the others; see module docstring
            result["sources"][name] = {"status": "failed", "error": str(e)}
            any_failed = True
            logger.error("gtm_intelligence_sweep: sensing %s failed -- %s", name, e)

    try:
        interpreted = run_interpretation_sweep(db, tenant_id, sources=ALL_INTERPRETED_SOURCES)
        result["interpretation"] = {"status": "succeeded", "created": len(interpreted)}
        any_succeeded = True
        logger.info("gtm_intelligence_sweep: interpretation succeeded (%d created)", len(interpreted))
    except Exception as e:  # noqa: BLE001 -- see module docstring
        result["interpretation"] = {"status": "failed", "error": str(e)}
        any_failed = True
        logger.error("gtm_intelligence_sweep: interpretation failed -- %s", e)

    try:
        problems = run_problem_hypothesis_sweep(db, tenant_id, sources=ALL_INTERPRETED_SOURCES)
        result["problem_detection"] = {"status": "succeeded", "hypotheses_touched": len(problems)}
        any_succeeded = True
        logger.info("gtm_intelligence_sweep: problem detection succeeded (%d hypotheses touched)", len(problems))
    except Exception as e:  # noqa: BLE001 -- see module docstring
        result["problem_detection"] = {"status": "failed", "error": str(e)}
        any_failed = True
        logger.error("gtm_intelligence_sweep: problem detection failed -- %s", e)

    try:
        demands = run_demand_hypothesis_sweep(db, tenant_id, sources=ALL_INTERPRETED_SOURCES)
        result["demand_detection"] = {"status": "succeeded", "hypotheses_touched": len(demands)}
        any_succeeded = True
        logger.info("gtm_intelligence_sweep: demand detection succeeded (%d hypotheses touched)", len(demands))
    except Exception as e:  # noqa: BLE001 -- see module docstring
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
            result[stage_key] = {"status": "failed", "error": str(e)}
            any_failed = True
            logger.error("gtm_intelligence_sweep: %s failed -- %s", stage_key, e)

    # Account/Strategy/Sales branch (Batch 6) -- reads DemandHypothesis (produced above), but its
    # own failure never touches Problem/Demand or Content Intelligence, and vice versa. Each
    # stage is a pure/idempotent read-or-additive-insert sweep with zero LLM/external/CRM calls
    # (see opportunity.py/strategy.py/sales_agent.py docstrings) -- safe to run every cycle even
    # with near-zero real data.
    for stage_key, runner in ACCOUNT_STRATEGY_STAGES:
        try:
            stage_result = runner(db, tenant_id)
            result[stage_key] = {"status": "succeeded", **stage_result}
            any_succeeded = True
            logger.info("gtm_intelligence_sweep: %s succeeded -- %s", stage_key, stage_result)
        except Exception as e:  # noqa: BLE001 -- one stage's failure must never block the others; see module docstring
            result[stage_key] = {"status": "failed", "error": str(e)}
            any_failed = True
            logger.error("gtm_intelligence_sweep: %s failed -- %s", stage_key, e)

    if any_failed and any_succeeded:
        result["status"] = "partial"
    elif any_failed:
        result["status"] = "failed"
    else:
        result["status"] = "completed"
    return result
