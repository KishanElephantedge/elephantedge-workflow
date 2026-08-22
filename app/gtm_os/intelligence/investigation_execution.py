"""Controlled investigation execution -- Autonomous Sensing Phase S5.

The first phase in this chain that actually calls a real sensing adapter. Deliberately narrow:
executes exactly ONE already-generated (S4) InvestigationAction, through the exact existing
adapter function for its source -- no new provider, no parallel persistence path, no fallback,
no loop. NOT wired into run_gtm_intelligence_sweep() or any autonomous scheduler; callable only
as an explicit, controlled capability until proven (per the approved S5 scope).

SAFETY ORDER (every check below runs BEFORE any provider call, in this exact order):
    1. action.status == "ok"                          (S4 already succeeded)
    2. control plane running                          (control.check_can_run, reused)
    3. objective eligible for a new attempt            (is_eligible_for_attempt, reused)
    4. source recognized by the S3 capability registry (source_capabilities.SOURCE_CAPABILITIES)
    5. required credentials resolvable                 (Apify key / Deepline key, reused lookups)
    6. a REAL, CONFIGURED budget guard exists           (see _budget_check below)

BUDGET RULE, per source cost_category:
    - COST_FREE (company_website): nothing to guard, always proceeds.
    - COST_DEEPLINE_BUDGET_GUARDED (theirstack_job): requires a real, configured
      discovery.daily_budget_usd, checked via the existing app.budget_guard.BudgetGuard
      (Deepline's own live balance check) -- unchanged from before Phase S8.
    - COST_APIFY_PER_POST/COST_APIFY_PER_QUERY (linkedin_post_search/web_search): Phase S8 --
      now checked via the REAL app.apify_budget_guard.check_apify_budget(), using the real
      per-call cost already documented in apify_client.py and Apify's own real (if slightly
      lagged) usage-so-far figures -- never a locally-accumulated estimate, never treating an
      unconfigured apify.daily_budget_usd/monthly_budget_usd as unlimited. Before Phase S8, both
      sources were unconditionally blocked here regardless of any configured number, because no
      real guard existed at all -- that gap is now closed.

RESULT RECORDING: reuses record_investigation_attempt() (S1) unchanged -- no second retry/logging
system. Only genuine attempts (the provider was actually called) get recorded; a pre-flight block
(control/eligibility/budget/unsupported/missing-credentials) does NOT increment attempts, exactly
like a blocked S3/S4 call never touched InvestigationObjective either.

EVIDENCE NEUTRALITY: this module NEVER judges whether a returned signal is a real problem/demand,
never assigns an evidence tier, never touches ProblemHypothesis/DemandHypothesis/Opportunity. A
successful execution that returns signals is recorded as S1's own `inconclusive` result -- "a
sensing attempt happened and produced N raw signals," not "evidence was found." That judgment
belongs entirely to the existing, untouched interpretation/evidence-tier pipeline downstream."""

from datetime import datetime
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.apify_budget_guard import STATUS_ALLOWED as APIFY_BUDGET_ALLOWED
from app.apify_budget_guard import check_apify_budget
from app.apify_client import GOOGLE_SEARCH_COST_PER_QUERY_USD, LINKEDIN_POST_COST_PER_POST_USD, ApifyError
from app.apify_client import _get_api_key as _get_apify_api_key
from app.budget_guard import BudgetExceededError, BudgetGuard
from app.gtm_os.intelligence.investigation_memory import (
    RESULT_ERROR, RESULT_INCONCLUSIVE, STATUS_STOPPED, InvestigationObjective,
    is_eligible_for_attempt, record_investigation_attempt,
)
from app.gtm_os.intelligence.sensing import sense_company_website, sense_linkedin_posts, sense_theirstack_jobs, sense_web_search
from app.gtm_os.intelligence.source_capabilities import COST_DEEPLINE_BUDGET_GUARDED, COST_FREE, SOURCE_CAPABILITIES
from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run, get_control_config

# Real, bounded call shape S5 actually executes per source (matches the execution branch below
# exactly) -- used only to compute the real estimated cost passed to check_apify_budget(), never
# to invent a different volume than what's really about to be called.
LINKEDIN_POSTS_PER_SOURCE = 5

EXEC_RESULTS_RETURNED = "results_returned"
EXEC_NO_RESULTS = "no_results"
EXEC_PROVIDER_FAILED = "provider_failed"
EXEC_BLOCKED_BY_CONTROL = "blocked_by_control"
EXEC_BLOCKED_BY_INELIGIBILITY = "blocked_by_ineligibility"
EXEC_BLOCKED_BY_BUDGET = "blocked_by_budget"
EXEC_BLOCKED_BY_GENERATION_FAILED = "blocked_by_generation_failed"
EXEC_BLOCKED_BY_UNSUPPORTED_SOURCE = "blocked_by_unsupported_source"
EXEC_BLOCKED_BY_MISSING_CREDENTIALS = "blocked_by_missing_credentials"

# Only these 4 -- exactly the ones S3/S4 can actually produce, per the explicit task scope.
SUPPORTED_SOURCES = {"linkedin_post_search", "web_search", "theirstack_job", "company_website"}


def _result(objective_id, source, action_type, status, *, raw_result_count=None, error_reason=None, started_at=None, investigation_attempt=None):
    return {
        "objective_id": objective_id,
        "source": source,
        "action_type": action_type,
        "status": status,
        "raw_result_count": raw_result_count,
        "error_reason": error_reason,
        "started_at": started_at,
        "completed_at": datetime.utcnow().isoformat(),
        "investigation_attempt": investigation_attempt,
    }


def _estimate_apify_cost_usd(source: str, parameters: dict) -> float:
    """The REAL, bounded call S5 is about to make for this source -- matches the execution
    branch below exactly, never a different/larger volume. Uses only the real, already-
    documented per-unit pricing from apify_client.py (never a guessed number)."""
    if source == "linkedin_post_search":
        query_count = len(parameters.get("queries", []))
        return query_count * LINKEDIN_POSTS_PER_SOURCE * LINKEDIN_POST_COST_PER_POST_USD
    if source == "web_search":
        return GOOGLE_SEARCH_COST_PER_QUERY_USD  # only the first generated query is ever executed
    raise ValueError(f"no real cost estimation exists for source {source!r}")


def _budget_check(db: Session, tenant_id: int, source: str, parameters: dict) -> str | None:
    """Returns None if execution may proceed, or a string error reason if blocked. See module
    docstring for the exact rule per cost_category."""
    capability = SOURCE_CAPABILITIES.get(source, {})
    cost_category = capability.get("cost_category")

    if cost_category == COST_FREE:
        return None  # genuinely nothing to guard against

    if cost_category == COST_DEEPLINE_BUDGET_GUARDED:
        daily_budget = get_control_config(db, tenant_id).get("discovery", {}).get("daily_budget_usd")
        if daily_budget is None:
            return "no real discovery.daily_budget_usd configured -- None is not treated as unlimited"
        try:
            BudgetGuard(budget_usd=daily_budget).check()
        except BudgetExceededError as e:
            return f"Deepline budget check failed: {e}"
        return None

    if source in ("linkedin_post_search", "web_search"):
        estimated_cost = _estimate_apify_cost_usd(source, parameters)
        result = check_apify_budget(db, tenant_id, estimated_cost)
        if result["status"] == APIFY_BUDGET_ALLOWED:
            return None
        return result["reason"]

    return f"no real budget guard exists for cost_category={cost_category!r} -- unattended production execution is not permitted yet"


def _record(db: Session, tenant_id: int, objective: InvestigationObjective, result: str, source: str) -> dict:
    updated = record_investigation_attempt(db, tenant_id, objective, result, source_attempted=source)
    return {
        "attempts": updated.attempts, "status": updated.status,
        "next_eligible_at": updated.next_eligible_at.isoformat() if updated.next_eligible_at else None,
        "stopped_reason": updated.stopped_reason,
    }


def execute_investigation_action(db: Session, tenant_id: int, action: dict) -> dict:
    """`action` is exactly what generate_investigation_action() (S4) returned. Fetches the real
    InvestigationObjective by action['objective_id'], scoped to tenant_id (tenant isolation)."""
    objective_id = action.get("objective_id")
    source = action.get("source")
    action_type = action.get("action_type")
    started_at = datetime.utcnow().isoformat()

    if action.get("status") != "ok":
        return _result(objective_id, source, action_type, EXEC_BLOCKED_BY_GENERATION_FAILED, error_reason=action.get("failure_reason") or action.get("status"), started_at=started_at)

    objective = db.get(InvestigationObjective, objective_id)
    if not objective or objective.tenant_id != tenant_id:
        return _result(objective_id, source, action_type, EXEC_BLOCKED_BY_UNSUPPORTED_SOURCE, error_reason="objective not found for this tenant", started_at=started_at)

    try:
        check_can_run(db, tenant_id)
    except ControlPlaneHalted as e:
        return _result(objective_id, source, action_type, EXEC_BLOCKED_BY_CONTROL, error_reason=f"control plane is not running (state={e})", started_at=started_at)

    if not is_eligible_for_attempt(objective):
        reason = f"stopped: {objective.stopped_reason}" if objective.status == STATUS_STOPPED else f"cooldown until {objective.next_eligible_at}"
        return _result(objective_id, source, action_type, EXEC_BLOCKED_BY_INELIGIBILITY, error_reason=reason, started_at=started_at)

    if source not in SOURCE_CAPABILITIES or source not in SUPPORTED_SOURCES:
        return _result(objective_id, source, action_type, EXEC_BLOCKED_BY_UNSUPPORTED_SOURCE, error_reason=f"{source!r} is not a source S5 executes", started_at=started_at)

    if source in ("linkedin_post_search", "web_search"):
        try:
            _get_apify_api_key(db, tenant_id)
        except ApifyError as e:
            return _result(objective_id, source, action_type, EXEC_BLOCKED_BY_MISSING_CREDENTIALS, error_reason=str(e), started_at=started_at)

    parameters = action.get("parameters") or {}
    budget_block_reason = _budget_check(db, tenant_id, source, parameters)
    if budget_block_reason:
        return _result(objective_id, source, action_type, EXEC_BLOCKED_BY_BUDGET, error_reason=budget_block_reason, started_at=started_at)

    try:
        if source == "linkedin_post_search":
            # One real Apify call, batching all (<=3) S4-generated formulations as one bounded
            # request -- search_linkedin_posts() already accepts a list of URLs in one call.
            search_urls = [
                f"https://www.linkedin.com/search/results/content/?keywords={quote(q)}&datePosted=%22past-week%22&origin=FACETED_SEARCH"
                for q in parameters.get("queries", [])
            ]
            signals = sense_linkedin_posts(db, tenant_id, search_urls, limit_per_source=5)
        elif source == "web_search":
            # sense_web_search() takes exactly one query -- no batching support exists in this
            # adapter, so only the FIRST generated formulation is executed (one bounded call),
            # per the explicit "do not auto-execute multiple formulations unless the provider
            # interface already supports that batch" instruction.
            queries = parameters.get("queries", [])
            signals = sense_web_search(db, tenant_id, queries[0]) if queries else []
        elif source == "theirstack_job":
            signals = sense_theirstack_jobs(db, tenant_id, offset=parameters.get("offset", 0), limit=parameters.get("limit", 25), exclude_domains=parameters.get("exclude_domains") or None)
        elif source == "company_website":
            signals = sense_company_website(db, tenant_id, parameters["domain"], company_id=parameters.get("company_id"))
        else:
            return _result(objective_id, source, action_type, EXEC_BLOCKED_BY_UNSUPPORTED_SOURCE, error_reason=f"{source!r} has no execution path", started_at=started_at)
    except Exception as e:  # ApifyError/DeeplineError/ScrapeError all land here -- any provider failure is recorded the same way
        attempt = _record(db, tenant_id, objective, RESULT_ERROR, source)
        return _result(objective_id, source, action_type, EXEC_PROVIDER_FAILED, error_reason=str(e), started_at=started_at, investigation_attempt=attempt)

    raw_result_count = len(signals)
    exec_status = EXEC_RESULTS_RETURNED if raw_result_count > 0 else EXEC_NO_RESULTS
    # Deliberately RESULT_INCONCLUSIVE either way -- raw signals existing is not the same as
    # evidence existing; that judgment is downstream interpretation's job, never this module's.
    attempt = _record(db, tenant_id, objective, RESULT_INCONCLUSIVE, source)
    return _result(objective_id, source, action_type, exec_status, raw_result_count=raw_result_count, started_at=started_at, investigation_attempt=attempt)
