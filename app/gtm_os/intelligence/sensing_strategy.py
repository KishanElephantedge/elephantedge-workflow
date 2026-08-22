"""Sensing strategy selection -- Autonomous Sensing Phase S3.

Answers exactly: "For this InvestigationObjective, which existing sensing capability should the
agent use next?" -- nothing about HOW to invoke it (no query text, no provider calls). That is
S4's job, explicitly out of scope here.

DELIBERATELY NOT a disguised ICP->source routing table. The objective SHAPE (derived from real,
already-existing ICP config -- icp_config.py's own trigger_mode/trigger_hiring_roles -- plus the
objective's own evidence_sought field) drives selection, not the ICP id itself. Two different
ICPs that happen to share the same trigger shape get the same candidate ordering; an ICP whose
config changed would change its own routing automatically, without this module's code changing.

Every decision is explainable via `rationale`, using only:
    - source_capabilities.py's registry (evidence type, identity support, cost, wiring)
    - the objective's own evidence_sought / target_company_id / icp_id
    - S1's own memory fields (source_attempted, attempts, status, next_eligible_at) -- REUSED,
      not re-implemented. This module invents no second retry/cooldown system: if an objective
      isn't eligible for a new attempt at all (per investigation_memory.is_eligible_for_attempt),
      S3 returns "no strategy" without even considering candidates.
    - control.py's check_can_run() -- REUSED, not re-implemented.

No external calls, no LLM calls, no query generation, no writes to GtmSignal/Problem/Demand/
Company anywhere in this module."""

from sqlalchemy.orm import Session

from app.db.models import Company
from app.gtm_os.icp.icp_config import get_icp_config
from app.gtm_os.intelligence.investigation_memory import STATUS_STOPPED, InvestigationObjective, is_eligible_for_attempt
from app.gtm_os.intelligence.source_capabilities import SOURCE_CAPABILITIES
from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run

SHAPE_HIRING_TRIGGER = "hiring_trigger"
SHAPE_IDENTITY_RESOLUTION = "identity_resolution"
SHAPE_GENERAL_PROBLEM = "general_problem"


def _no_strategy(objective: InvestigationObjective, rationale: str) -> dict:
    return {
        "source": None,
        "objective_id": objective.id,
        "rationale": rationale,
        "expected_evidence": None,
        "fallback_allowed": False,
        "cost_category": None,
    }


def _objective_shape(db: Session, tenant_id: int, objective: InvestigationObjective) -> str:
    if objective.evidence_sought == "identity_resolution":
        return SHAPE_IDENTITY_RESOLUTION
    icp_entry = next((icp for icp in get_icp_config(db, tenant_id) if icp["id"] == objective.icp_id), None)
    if icp_entry and icp_entry.get("trigger_mode") == "requires_presence" and icp_entry.get("trigger_hiring_roles"):
        return SHAPE_HIRING_TRIGGER
    return SHAPE_GENERAL_PROBLEM


def _candidates_for_shape(db: Session, tenant_id: int, objective: InvestigationObjective, shape: str) -> list[str]:
    """Ordered by capability fit for the shape, per the principles in the module docstring --
    never by icp_id directly. See each shape's own comment for the real principle applied."""
    if shape == SHAPE_HIRING_TRIGGER:
        # "hiring-trigger evidence -> prefer a real hiring/job source" -- both are real structured
        # job sources; theirstack_job is wired into the hourly sweep AND budget-guarded today,
        # linkedin_job exists but has no approved search-criteria config yet (registry-recorded
        # fact, not invented) so it's ranked second, not excluded.
        base = ["theirstack_job", "linkedin_job"]
        base.append("web_search")  # last-resort, per "broader web search only if cheaper sources inconclusive"
        return base

    if shape == SHAPE_IDENTITY_RESOLUTION:
        # "company-specific unresolved evidence -> prefer a source capable of producing evidence
        # tied to that company." company_website is the ONLY source that ties a signal to a
        # company_id directly at creation, no resolver ambiguity at all -- the exact fit this
        # objective type needs. Deliberately NO generic fallback here: falling back to a
        # person-level or broad-web source for an identity-resolution gap would just produce
        # MORE unresolved-identity evidence, not resolve the one this objective already has.
        if not objective.target_company_id:
            return []
        company = db.get(Company, objective.target_company_id)
        if not company or not company.domain:
            return []  # no domain on file -- company_website genuinely isn't viable; not invented
        return ["company_website"]

    # SHAPE_GENERAL_PROBLEM (default, covers Rule 1/2/4's company-agnostic and open-ended cases)
    # "person-level problem evidence -> prefer person-level LinkedIn evidence"
    return ["linkedin_post_search", "web_search"]


def _deprioritize_just_attempted(candidates: list[str], objective: InvestigationObjective) -> list[str]:
    """"Avoid selecting the same source repeatedly if it was just attempted... unless it's the
    only viable option" -- moves objective.source_attempted to the back of the ordering rather
    than excluding it outright, so an alternative wins when one exists (test 4) but the same
    source can still be re-selected when it's genuinely the only candidate (test 5)."""
    if not objective.source_attempted or objective.source_attempted not in candidates:
        return candidates
    return [c for c in candidates if c != objective.source_attempted] + [objective.source_attempted]


def select_sensing_strategy(db: Session, tenant_id: int, objective: InvestigationObjective) -> dict:
    """Always returns a structured dict -- `source` is None (with an explicit `rationale`) when
    no strategy should be selected, never omitted/raised. See module docstring for the exact
    checks performed, in order."""
    if objective.status == STATUS_STOPPED:
        return _no_strategy(objective, f"objective already stopped/superseded: {objective.stopped_reason!r}")

    if not is_eligible_for_attempt(objective):
        return _no_strategy(objective, f"objective is in cooldown until {objective.next_eligible_at} (max_attempts/backoff reused from control.py, not re-implemented)")

    try:
        check_can_run(db, tenant_id)
    except ControlPlaneHalted as e:
        return _no_strategy(objective, f"control plane is not running (state={e})")

    shape = _objective_shape(db, tenant_id, objective)
    candidates = _candidates_for_shape(db, tenant_id, objective, shape)
    candidates = _deprioritize_just_attempted(candidates, objective)

    if not candidates:
        return _no_strategy(objective, f"no viable sensing source exists for objective shape {shape!r} given current state (e.g. no company domain on file) -- not inventing one")

    chosen = candidates[0]
    capability = SOURCE_CAPABILITIES[chosen]
    reused_note = ""
    if objective.source_attempted and chosen == objective.source_attempted:
        reused_note = f" (re-selected {chosen!r} despite a prior attempt -- no viable alternative exists for this objective shape, respecting attempt/cooldown limits already enforced above)"
    elif objective.source_attempted:
        reused_note = f" (avoided repeating {objective.source_attempted!r}, just attempted)"

    return {
        "source": chosen,
        "objective_id": objective.id,
        "rationale": (
            f"objective shape={shape!r}, evidence_sought={objective.evidence_sought!r} -> "
            f"{chosen!r} matches capability principle "
            f"({capability['evidence_type']}){reused_note}"
        ),
        "expected_evidence": capability["evidence_type"],
        "fallback_allowed": capability["fallback_allowed"],
        "cost_category": capability["cost_category"],
    }
