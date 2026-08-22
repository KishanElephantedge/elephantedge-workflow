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

from app.gtm_os.intelligence.gap_identification import identify_investigation_gaps
from app.gtm_os.intelligence.investigation_execution import execute_investigation_action
from app.gtm_os.intelligence.investigation_feedback import process_investigation_feedback
from app.gtm_os.intelligence.investigation_generation import generate_investigation_action
from app.gtm_os.intelligence.investigation_memory import InvestigationObjective, STATUS_STOPPED, is_eligible_for_attempt
from app.gtm_os.intelligence.sensing_strategy import select_sensing_strategy
from app.gtm_os.orchestration.control import get_control_config


def _select_bounded_objectives(db: Session, tenant_id: int, limit: int) -> list[InvestigationObjective]:
    """Eligible = not stopped AND (never attempted OR its cooldown has elapsed) -- reuses
    is_eligible_for_attempt() (S1) row-by-row rather than re-deriving eligibility in SQL. Ordered
    by id ascending (oldest-first) -- a stable tie-break, not a business priority score (no real
    deterministic priority basis exists yet, per the approved architecture design)."""
    candidates = (
        db.query(InvestigationObjective)
        .filter(InvestigationObjective.tenant_id == tenant_id)
        .filter(InvestigationObjective.status != STATUS_STOPPED)
        .order_by(InvestigationObjective.id.asc())
        .all()
    )
    eligible = [o for o in candidates if is_eligible_for_attempt(o)]
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
