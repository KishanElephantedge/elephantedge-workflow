"""Autonomous Sensing Phase S7 -- the bounded, per-tick investigation cycle.

Wires S2 (gap identification) through S6 (feedback) into ONE callable stage, meant to be invoked
once per hourly V2 intelligence sweep tick (see sweep.py). Contains no new sensing/interpretation/
evidence/eligibility logic of its own -- every step here is a plain call into an already-built,
already-tested S1-S6 function.

BOUNDING (per the approved S7 design): S2 itself is free/local (no external call, no LLM) and
always runs, so InvestigationObjective state stays current every tick regardless of configuration.
S3->S6 (which can select/generate/execute a real sensing action) are bounded by
control.investigation.max_objectives_per_tick -- when that's unconfigured (None), S3-S6 are
SKIPPED entirely for this tick and an explicit "configuration_required" status is reported. None
is never treated as unlimited."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.gtm_os.intelligence.gap_identification import (
    EVIDENCE_SOUGHT_ADDITIONAL_INDEPENDENT_EVIDENCE, EVIDENCE_SOUGHT_IDENTITY_RESOLUTION,
    EVIDENCE_SOUGHT_OPENING_TIER, identify_investigation_gaps,
)
from app.gtm_os.intelligence.investigation_execution import execute_investigation_action
from app.gtm_os.intelligence.investigation_feedback import process_investigation_feedback
from app.gtm_os.intelligence.investigation_generation import generate_investigation_action
from app.gtm_os.intelligence.investigation_memory import InvestigationObjective, STATUS_STOPPED, is_eligible_for_attempt
from app.gtm_os.intelligence.sensing_strategy import select_sensing_strategy
from app.gtm_os.orchestration.control import get_control_config

# Categorical closeness-to-Opportunity-eligibility, per gap_identification.py's own already-named
# rules -- NOT a numeric/weighted score, just an ordering of the three real evidence_sought states
# that already exist. ADDITIONAL_INDEPENDENT_EVIDENCE (Rule 4/opening-evidence-already-exists) is
# genuinely one step from Opportunity eligibility; IDENTITY_RESOLUTION (Rule 3) already has
# opening-tier Problem evidence, just blocked on a resolved company; OPENING_TIER (Rule 2) hasn't
# established any Problem evidence for this company yet at all -- the furthest of the three.
_EVIDENCE_SOUGHT_PROXIMITY_RANK = {
    EVIDENCE_SOUGHT_ADDITIONAL_INDEPENDENT_EVIDENCE: 0,
    EVIDENCE_SOUGHT_IDENTITY_RESOLUTION: 1,
    EVIDENCE_SOUGHT_OPENING_TIER: 2,
}
_MIN_DATETIME = datetime.min


def _objective_priority_key(objective: InvestigationObjective):
    """Deterministic, real-field-only ordering -- every dimension here reads an already-real
    column, never an invented/weighted score. Priority, highest first:
      1. company-specific over company-agnostic (a real target_company_id only ever exists
         because gap_identification.py already found a real ICPMatch for it -- Rules 2/3/4; a
         company-agnostic objective (target_company_id=None) exists specifically because NO
         ICPMatch exists yet at all -- Rule 1. So "company-specific" and "has a matched ICP" are
         the same real fact here, not two separate signals to invent.
      2. fewer attempts so far (a never-attempted objective is preferred over one already tried
         and cooling down).
      3. "freshest" eligibility -- next_eligible_at=None (never attempted, no cooldown standing
         in the way at all) sorts as freshest; among cooldown-cleared objectives, the one whose
         cooldown ended earliest (has been sitting ready longest) sorts next.
      4. closest to Opportunity eligibility, via the real evidence_sought categories above.
      5. id ascending -- the final, stable tie-break (unchanged from before)."""
    is_company_agnostic = objective.target_company_id is None
    proximity = _EVIDENCE_SOUGHT_PROXIMITY_RANK.get(objective.evidence_sought, 3)
    return (
        is_company_agnostic,
        objective.attempts,
        objective.next_eligible_at or _MIN_DATETIME,
        proximity,
        objective.id,
    )


def _select_bounded_objectives(db: Session, tenant_id: int, limit: int) -> list[InvestigationObjective]:
    """Eligible = not stopped AND (never attempted OR its cooldown has elapsed) -- reuses
    is_eligible_for_attempt() (S1) row-by-row rather than re-deriving eligibility in SQL.

    Ordered by _objective_priority_key() -- a real company-specific investigation (backed by an
    actual ICPMatch) now genuinely outranks a company-agnostic one, rather than an arbitrary id
    ordering always favoring whichever objective happened to be created first. Still a
    deterministic tuple sort over existing real fields only -- no numeric/weighted score."""
    candidates = (
        db.query(InvestigationObjective)
        .filter(InvestigationObjective.tenant_id == tenant_id)
        .filter(InvestigationObjective.status != STATUS_STOPPED)
        .all()
    )
    eligible = [o for o in candidates if is_eligible_for_attempt(o)]
    eligible.sort(key=_objective_priority_key)
    return eligible[:limit]


def run_investigation_cycle(db: Session, tenant_id: int) -> dict:
    """The S7 stage. Never raises -- any per-objective failure is captured in its own result
    entry, same error-isolation discipline as every other sweep stage in this codebase."""
    started_at = datetime.utcnow().isoformat()

    gap_result = identify_investigation_gaps(db, tenant_id)
    gap_summary = {
        "created": len(gap_result["created"]),
        "reused": len(gap_result["reused"]),
        "superseded": len(gap_result["superseded"]),
        "unresolved_no_icp_context": len(gap_result["unresolved_no_icp_context"]),
    }

    config = get_control_config(db, tenant_id)
    max_per_tick = config.get("investigation", {}).get("max_objectives_per_tick")

    if not max_per_tick:
        return {
            "status": "configuration_required",
            "reason": "investigation.max_objectives_per_tick is not configured -- S3-S6 skipped this tick, S2 gap-identification still ran",
            "gap_identification": gap_summary,
            "objectives_processed": 0,
            "results": [],
            "started_at": started_at,
            "completed_at": datetime.utcnow().isoformat(),
        }

    objectives = _select_bounded_objectives(db, tenant_id, max_per_tick)
    results = []
    any_failed = False
    for objective in objectives:
        try:
            strategy = select_sensing_strategy(db, tenant_id, objective)
            action = generate_investigation_action(db, tenant_id, objective, strategy)
            execution = execute_investigation_action(db, tenant_id, action)
            feedback = process_investigation_feedback(db, tenant_id, objective.id, execution)
            results.append({
                "objective_id": objective.id, "icp_id": objective.icp_id, "target_company_id": objective.target_company_id,
                "strategy_source": strategy.get("source"), "exec_status": execution.get("status"), "feedback_result": feedback.get("result"),
            })
        except Exception as e:  # noqa: BLE001 -- one objective's failure must never block the others
            any_failed = True
            results.append({"objective_id": objective.id, "error": str(e)})

    return {
        "status": "partial" if any_failed else "succeeded",
        "gap_identification": gap_summary,
        "objectives_processed": len(objectives),
        "results": results,
        "started_at": started_at,
        "completed_at": datetime.utcnow().isoformat(),
    }
