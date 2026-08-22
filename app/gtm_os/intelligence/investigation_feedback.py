"""Investigation feedback + sufficiency loop -- Autonomous Sensing Phase S6.

Connects an S5 execution result back into the EXISTING, unmodified V2 pipeline (interpretation,
evidence tiers, company resolution, ICP matching, Opportunity eligibility) and decides what
happened to the InvestigationObjective as a result -- WITHOUT recreating any of that logic.

THE CENTRAL REUSE: "did this investigation objective's gap actually close?" is EXACTLY the
question identify_investigation_gaps() (S2) already answers every time it runs -- it already
calls the real evaluate_opportunity_eligibility() (never re-derived here), already supersedes a
thread the instant its gap closes (Rule 5), and already scopes strictly by (icp_id,
target_company_id) so one company's progress can never satisfy another's objective (S2's own
already-tested guarantee, inherited here for free, not reimplemented). S6's job is narrower than
it might look: run the real downstream sweeps so new evidence actually gets processed, re-run
gap-identification so the real authoritative decision gets made, then READ the resulting
objective state and translate it into a small, explainable result vocabulary for the caller.

CRITICAL DISTINCTION (per the task): "provider returned results" is never conflated with
"investigation succeeded." A `results_returned` S5 outcome only means raw GtmSignal rows exist --
whether they produced ANY usable evidence, let alone Opportunity-eligible evidence, is decided
entirely by the existing interpretation/evidence-tier/eligibility pipeline this module invokes
but never judges independently.

HONEST PRE-EXISTING LIMITATION (discovered here, not introduced): the real interpreter dispatch
table (interpretation.py's _INTERPRETERS) only covers linkedin_job/theirstack_job/linkedin_post/
linkedin_reply -- web_search and company_website signals are NEVER interpreted by the existing
pipeline (no interpreter is registered for either source). Running the interpretation sweep after
a web_search/company_website execution is therefore a real no-op for evidence purposes today --
S6 does not invent a workaround; it reports the resulting state exactly as it is
(`results_no_opening_evidence`), which is the honest, correct read given the current architecture."""

from app.db.models import Company
from app.gtm_os.icp.icp_matching import ICPMatch, run_icp_matching_sweep
from app.gtm_os.intelligence.demand_detection import run_demand_hypothesis_sweep
from app.gtm_os.intelligence.gap_identification import identify_investigation_gaps
from app.gtm_os.intelligence.interpretation import run_interpretation_sweep
from app.gtm_os.intelligence.investigation_memory import STATUS_STOPPED, InvestigationObjective
from app.gtm_os.intelligence.problem_detection import run_problem_hypothesis_sweep

# ALL_INTERPRETED_SOURCES is imported lazily inside process_investigation_feedback() below, not
# at module level -- sweep.py itself imports investigation_cycle.py (which imports this module)
# as part of Phase S7's wiring, so a top-level import here would be circular. Same lazy-import
# pattern this codebase already uses elsewhere for exactly this reason (e.g. sweep.py's own
# dry_run branch importing discovery.is_discovery_due lazily).

RESULT_NO_RESULTS = "no_results"
RESULT_NO_OPENING_EVIDENCE = "results_no_opening_evidence"
RESULT_OPENING_UNRESOLVED_COMPANY = "opening_evidence_unresolved_company"
RESULT_OPENING_COMPANY_RESOLVED = "opening_evidence_company_resolved"
RESULT_OPPORTUNITY_READY = "opportunity_ready"
RESULT_PROVIDER_FAILED = "provider_failed"
RESULT_BLOCKED = "blocked"

_BLOCKED_EXEC_STATUSES = {
    "blocked_by_control", "blocked_by_ineligibility", "blocked_by_budget",
    "blocked_by_generation_failed", "blocked_by_unsupported_source", "blocked_by_missing_credentials",
}


def _classify_objective(db, tenant_id: int, objective_id: int) -> str:
    """Reads the CURRENT state of one InvestigationObjective row (already updated, if at all, by
    the identify_investigation_gaps() call that ran just before this) and translates it into the
    S6 result vocabulary. Makes no independent evidence/eligibility judgment of its own -- every
    fact here traces back to what S2/the existing Opportunity gate already decided."""
    objective = db.get(InvestigationObjective, objective_id)
    if objective.status == STATUS_STOPPED:
        reason = (objective.stopped_reason or "").lower()
        if "opportunity" in reason:
            return RESULT_OPPORTUNITY_READY
        if objective.target_company_id is None and "matched" in reason:
            # The company-agnostic "no candidates yet" thread just got retired because real
            # ICPMatch rows appeared THIS pass -- identify_investigation_gaps() already
            # created/reused the more specific company-level thread(s) tracking the real state.
            # Inherit whichever of those is most advanced (one level of lookup, not a loop).
            specific = (
                db.query(InvestigationObjective)
                .filter(InvestigationObjective.tenant_id == tenant_id)
                .filter(InvestigationObjective.icp_id == objective.icp_id)
                .filter(InvestigationObjective.target_company_id.isnot(None))
                .order_by(InvestigationObjective.id.desc())
                .first()
            )
            if specific:
                return _classify_objective(db, tenant_id, specific.id)
            return RESULT_NO_OPENING_EVIDENCE  # defensive fallback; should not occur structurally
        return RESULT_NO_OPENING_EVIDENCE  # defensive fallback for any other stop reason

    if objective.evidence_sought == "identity_resolution":
        return RESULT_OPENING_UNRESOLVED_COMPANY
    if objective.evidence_sought == "additional_independent_evidence":
        return RESULT_OPENING_COMPANY_RESOLVED
    return RESULT_NO_OPENING_EVIDENCE


def process_investigation_feedback(db, tenant_id: int, objective_id: int, execution_result: dict) -> dict:
    """`execution_result` is exactly what execute_investigation_action() (S5) returned for this
    objective_id. Returns {objective_id, result, exec_status, raw_result_count, objective_status,
    stopped_reason}. Only ever advances the OBJECTIVE via S1's own memory API
    (identify_investigation_gaps -> get_or_create_objective/mark_superseded) -- never a second
    write path onto InvestigationObjective."""
    exec_status = execution_result.get("status")

    if exec_status in _BLOCKED_EXEC_STATUSES:
        result = RESULT_BLOCKED
    elif exec_status == "provider_failed":
        result = RESULT_PROVIDER_FAILED
    elif exec_status == "no_results":
        result = RESULT_NO_RESULTS
    elif exec_status == "results_returned":
        # Existing, unmodified downstream pipeline -- reused, never duplicated.
        from app.gtm_os.orchestration.sweep import ALL_INTERPRETED_SOURCES  # lazy -- see module-level comment
        run_interpretation_sweep(db, tenant_id, sources=ALL_INTERPRETED_SOURCES)
        run_problem_hypothesis_sweep(db, tenant_id)
        run_demand_hypothesis_sweep(db, tenant_id)
        run_icp_matching_sweep(db, tenant_id)  # so a newly-resolved company can become a real ICP candidate this same cycle
        identify_investigation_gaps(db, tenant_id)  # the ONE authoritative sufficiency decision -- not re-derived here
        result = _classify_objective(db, tenant_id, objective_id)
    else:
        result = RESULT_BLOCKED  # unrecognized exec_status -- treat conservatively, never assume success

    objective = db.get(InvestigationObjective, objective_id)
    return {
        "objective_id": objective_id,
        "result": result,
        "exec_status": exec_status,
        "raw_result_count": execution_result.get("raw_result_count"),
        "objective_status": objective.status if objective else None,
        "stopped_reason": objective.stopped_reason if objective else None,
    }
