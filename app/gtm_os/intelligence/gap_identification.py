"""Gap identification -- Autonomous Sensing Phase S2.

Answers exactly one question per ICP/company thread: "what should the system investigate next,
if anything?" -- using ONLY real, already-computed state (ICPMatch, ProblemHypothesis,
DemandHypothesis, Opportunity, evidence tiers). It does NOT sense, does NOT generate queries, and
does NOT invoke any interpretation/LLM logic -- that is S3+, explicitly out of scope here.

Distinguishes exactly the five states the approved design calls for:

    1. No candidates          -- zero ICPMatch rows exist yet for this icp_id at all
    2. Lacks opening-tier     -- a company IS ICP-matched, but no linked ProblemHypothesis has
                                  reached OPENING_TIERS (declared|implied_gap)
    3. Identity unresolved    -- opening-tier evidence exists under a bare company_name_raw
                                  (ProblemHypothesis.company_id is None) that can be tied back to
                                  an already ICP-matched Company by exact name -- ONLY that case;
                                  see the honest limitation noted at the bottom of this docstring
    4. Opportunity blocked    -- opening-tier Problem evidence exists, but
                                  evaluate_opportunity_eligibility() (REUSED verbatim, not
                                  re-derived) says NOT YET eligible for a reason more evidence
                                  could plausibly fix (independent_evidence_count too low, demand
                                  tier not yet qualifying, evidence gone stale)
    5. Sufficient             -- an Opportunity already exists, OR eligibility now says YES --
                                  either way, no more sensing helps; the existing ICP/Opportunity
                                  pipeline owns what happens next, unchanged

Each pass reconciles state going both directions: it creates/continues objectives for gaps that
still exist, AND supersedes (stops) any previously-open objective whose gap has since closed
(company got ICP-matched, evidence appeared, Opportunity got created) -- "if sufficient evidence
exists, do NOT create another investigation objective" cuts both ways.

HONEST LIMITATION (Rule 3): a ProblemHypothesis with company_id=None whose company_name_raw does
NOT exact-match any Company that has ALSO already been ICP-matched has no icp_id to attach an
InvestigationObjective to under the current schema (icp_id is derived from ICPMatch, which
requires a resolved company_id). Such threads are real and are reported in this module's return
value under `unresolved_no_icp_context`, but no objective is created for them -- inventing a
sentinel/icp-agnostic icp_id was deliberately avoided rather than fabricating a business concept
that doesn't exist in the approved design.

No priority/ordering score is computed -- the approved design explicitly forbids inventing
numeric scoring, and no real deterministic priority basis exists yet (gtm_motion_config.py's own
`priority` field is still unconfigured `None` for every motion). Objectives are returned in a
stable (icp_id, company_id) order only -- a tie-break, not a business decision."""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Batch, Company
from app.gtm_os.icp.icp_config import get_icp_config
from app.gtm_os.icp.icp_matching import ICPMatch
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.intelligence.investigation_memory import get_active_objective, get_or_create_objective, mark_superseded
from app.gtm_os.intelligence.problem_detection import OPENING_TIERS
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
from app.gtm_os.opportunity.opportunity import Opportunity, evaluate_opportunity_eligibility
from app.gtm_os.opportunity.opportunity_config import get_opportunity_config

# Conceptual claim text per ICP, transcribed verbatim from the approved architecture design --
# not invented here. These describe the information gap, never a search query.
CLAIM_BY_ICP: dict[str, str] = {
    "icp_1": "Find evidence that an ICP1-consistent company is experiencing a sales/GTM scaling problem.",
    "icp_2": "Find evidence of AI-enabled GTM capability building or GTM-engineer hiring.",
    "icp_3": "Find evidence of sales-leadership need consistent with fractional/interim leadership.",
}

EVIDENCE_SOUGHT_OPENING_TIER = "opening_tier"
EVIDENCE_SOUGHT_IDENTITY_RESOLUTION = "identity_resolution"
EVIDENCE_SOUGHT_ADDITIONAL_INDEPENDENT_EVIDENCE = "additional_independent_evidence"


def _claim_for(icp_id: str) -> str:
    return CLAIM_BY_ICP.get(icp_id, f"Find evidence relevant to {icp_id}.")


def _best_problem_for_company(db: Session, tenant_id: int, company_id: int) -> ProblemHypothesis | None:
    problems = (
        db.query(ProblemHypothesis)
        .filter(ProblemHypothesis.tenant_id == tenant_id, ProblemHypothesis.company_id == company_id)
        .all()
    )
    if not problems:
        return None
    tier_rank = {"contextual": 0, "implied_gap": 1, "declared": 2}
    return max(problems, key=lambda p: tier_rank.get((p.confidence or {}).get("best_evidence_tier"), -1))


def identify_investigation_gaps(db: Session, tenant_id: int) -> dict:
    """One pass over every configured ICP. Returns a report dict -- never raises on a single
    ICP/company's failure (same per-item error-isolation style as every other sweep in this
    feature), though nothing here calls out to a provider, so failures would only ever be
    programming errors, not transient/network ones."""
    icp_configs = get_icp_config(db, tenant_id)
    opportunity_config = get_opportunity_config(db, tenant_id)

    created, reused, superseded = [], [], []

    def _create_or_reuse(icp_id, target_company_id, evidence_sought, reason):
        objective, was_created = get_or_create_objective(
            db, tenant_id, icp_id, target_company_id,
            claim=_claim_for(icp_id), evidence_sought=evidence_sought, reason=reason,
        )
        (created if was_created else reused).append(objective)
        return objective

    def _supersede_if_open(icp_id, target_company_id, reason):
        active = get_active_objective(db, tenant_id, icp_id, target_company_id)
        if active:
            superseded.append(mark_superseded(db, active, reason))

    for icp in icp_configs:
        icp_id = icp["id"]
        matches = (
            db.query(ICPMatch)
            .filter(ICPMatch.tenant_id == tenant_id, ICPMatch.icp_id == icp_id)
            .all()
        )

        if not matches:
            # Rule 1 -- no candidates at all yet for this ICP.
            _create_or_reuse(icp_id, None, EVIDENCE_SOUGHT_OPENING_TIER, "no ICP-consistent companies matched yet")
            continue

        # Real candidates exist now -- the company-agnostic Rule-1 thread (if any) is stale.
        _supersede_if_open(icp_id, None, "ICP-consistent companies have since been matched")

        for match in matches:
            company_id = match.company_id
            best_problem = _best_problem_for_company(db, tenant_id, company_id)
            best_tier = (best_problem.confidence or {}).get("best_evidence_tier") if best_problem else None
            opening_reached = best_tier in OPENING_TIERS

            if not opening_reached:
                # Rule 2 -- ICP-matched company, no opening-tier Problem evidence yet.
                _create_or_reuse(
                    icp_id, company_id, EVIDENCE_SOUGHT_OPENING_TIER,
                    f"ICP {icp_id} match exists but no opening-tier ProblemHypothesis evidence yet (best tier: {best_tier!r})",
                )
                continue

            demands = (
                db.query(DemandHypothesis)
                .filter(DemandHypothesis.tenant_id == tenant_id, DemandHypothesis.problem_hypothesis_id == best_problem.id)
                .all()
            )
            if not demands:
                # Opening-tier Problem exists, but no DemandHypothesis has been derived from it
                # yet -- still a real, sensing-addressable gap, same bucket as Rule 4 conceptually
                # (more/different evidence could resolve it), not invented as a 6th rule.
                _create_or_reuse(
                    icp_id, company_id, EVIDENCE_SOUGHT_ADDITIONAL_INDEPENDENT_EVIDENCE,
                    "opening-tier ProblemHypothesis evidence exists but no DemandHypothesis has been derived from it yet",
                )
                continue

            demand = max(demands, key=lambda d: d.id)  # most recently created; DemandHypothesis has no ranking field of its own
            existing_opportunity = (
                db.query(Opportunity)
                .filter(Opportunity.tenant_id == tenant_id, Opportunity.demand_hypothesis_id == demand.id)
                .first()
            )
            if existing_opportunity:
                # Rule 5 -- sufficient evidence, already acted on downstream.
                _supersede_if_open(icp_id, company_id, f"Opportunity #{existing_opportunity.id} already exists for this company/ICP")
                continue

            eligibility = evaluate_opportunity_eligibility(best_problem, demand, opportunity_config)
            if eligibility["eligible"]:
                # Rule 5 -- sufficient evidence, awaiting the (unchanged) Opportunity sweep to
                # actually create the row. Not this layer's job, and NOT a sensing gap.
                _supersede_if_open(icp_id, company_id, "Opportunity-eligible; awaiting the existing opportunity sweep, not a sensing gap")
                continue

            # Rule 4 -- opening-tier Problem evidence exists, but Opportunity eligibility isn't
            # met yet, for a reason more/different evidence could plausibly address.
            _create_or_reuse(
                icp_id, company_id, EVIDENCE_SOUGHT_ADDITIONAL_INDEPENDENT_EVIDENCE,
                f"opening-tier evidence exists but Opportunity not yet eligible: {eligibility['reason']}",
            )

    # Rule 3 -- unresolved-identity Problem evidence that can be tied back to an already
    # ICP-matched Company by exact name. See module docstring's honest limitation note for the
    # case this does NOT cover.
    unresolved_no_icp_context = []
    unresolved_problems = (
        db.query(ProblemHypothesis)
        .filter(ProblemHypothesis.tenant_id == tenant_id, ProblemHypothesis.company_id.is_(None))
        .filter(ProblemHypothesis.company_name_raw.isnot(None))
        .all()
    )
    for problem in unresolved_problems:
        tier = (problem.confidence or {}).get("best_evidence_tier")
        if tier not in OPENING_TIERS:
            continue
        normalized = problem.company_name_raw.strip().lower()
        candidate = (
            db.query(Company)
            .join(Batch, Company.batch_id == Batch.id)
            .filter(Batch.tenant_id == tenant_id)
            .filter(func.lower(Company.name) == normalized)
            .first()
        )
        if not candidate:
            unresolved_no_icp_context.append(problem.id)
            continue
        match = (
            db.query(ICPMatch)
            .filter(ICPMatch.tenant_id == tenant_id, ICPMatch.company_id == candidate.id)
            .first()
        )
        if not match:
            unresolved_no_icp_context.append(problem.id)
            continue
        _create_or_reuse(
            match.icp_id, candidate.id, EVIDENCE_SOUGHT_IDENTITY_RESOLUTION,
            f"opening-tier evidence exists under company_name_raw={problem.company_name_raw!r} "
            f"(ProblemHypothesis #{problem.id}) but is not linked to resolved Company #{candidate.id}",
        )

    return {
        "created": created,
        "reused": reused,
        "superseded": superseded,
        "unresolved_no_icp_context": unresolved_no_icp_context,
    }
