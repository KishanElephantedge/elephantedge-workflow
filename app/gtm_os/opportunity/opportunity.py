"""Opportunity Engine -- Batch 4 Parts B-E, J. Answers exactly one question:

    "Given this account's already-established Problem + Demand evidence, is there sufficient
     evidence that this represents a potentially actionable commercial situation?"

BOUNDARY (Part B) -- four distinct concepts, never collapsed:
    PROBLEM     "Something appears to be wrong / changing / difficult."        (ProblemHypothesis)
    DEMAND      "The company appears to be looking outward for a solution."    (DemandHypothesis)
    OPPORTUNITY "Sufficient evidence this account is a potentially actionable  (Opportunity, this file)
                 commercial situation."
    OFFERING FIT "Given the opportunity, which offering(s) could address it?"  (offering_matcher.py)

An Opportunity is NEVER just "a DemandHypothesis exists" -- see evaluate_opportunity_eligibility()
for the real, structured gate. Trend state (app/gtm_os/content/trend_intelligence.py) is NEVER
used as an Opportunity input -- trend answers "is this MARKET topic active," Opportunity answers
"is this ACCOUNT worth acting on," a different question about different evidence entirely.

EVIDENCE MODEL (Part C) -- deliberately NO new evidence/join table. An Opportunity links to
exactly one DemandHypothesis (which already requires exactly one ProblemHypothesis, which already
has its own fully-traced ProblemHypothesisEvidence/DemandHypothesisEvidence ->
InterpretedSignal -> GtmSignal -> raw_evidence chain). Duplicating that chain here would be new
evidence storage for data that already exists and is already queryable -- exactly what Part C's
own "do not duplicate raw signals" instruction rules out. "Why was this Opportunity created" is
answered by: Opportunity.confidence (the eligibility facts snapshot) + the linked
DemandHypothesis's own confidence + its evidence rows -- all real, all already built.

ACCOUNT VS MARKET (Part E/I) -- an Opportunity is written ONLY when company_id is known (never
fabricated -- if a DemandHypothesis has no company_id, it simply never produces an Opportunity;
its evidence is preserved exactly as-is on the DemandHypothesis, nothing is lost or hidden).
Market-level intelligence ("companies are discussing AI sales") is a SEPARATE, already-existing
representation -- ContentTopic (app/gtm_os/content/topic.py) + its trend evaluation
(trend_intelligence.py) -- deliberately NOT duplicated into a second "MarketOpportunityCandidate"
object here (Part I explicitly says not to implement full Market Opportunity scoring yet, and
ContentTopic/trend already fully represent "the market is discussing X" without needing a new
model). No function anywhere in this codebase promotes a ContentTopic/trend into an Opportunity
-- that boundary is enforced by simple absence, not a runtime check, exactly the "don't pretend a
trend automatically equals a sales opportunity" instruction.

LIFECYCLE (Part J) -- no existing CRM/pipeline-stage model was found anywhere in this codebase
(Company/Score/Contact have no deal-stage concept; Score.tier is a fit-tier, not a pipeline
stage). Smallest useful state machine, matching gpt.txt's own example exactly:
    candidate -> qualified -> dismissed | converted
`status` starts at "candidate" on creation; nothing in this batch ever transitions it further
(no UI/workflow to do so yet) -- the field exists so a later batch can wire real transitions
without a schema change."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, exists
from sqlalchemy.orm import Session

from app.db.models import Base
from app.gtm_os.icp.icp_matching import ICPMatch, get_icp_context_for_company
from app.gtm_os.intelligence.demand_detection import DEMAND_QUALIFYING_TIERS
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.intelligence.problem_detection import OPENING_TIERS
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
from app.gtm_os.opportunity.opportunity_config import get_opportunity_config

VALID_STATUSES = {"candidate", "qualified", "dismissed", "converted"}

# An Opportunity in one of these states is FINISHED and must not be re-worked by any downstream
# stage. "converted" is set when its outreach actually reached a provider (see send.py);
# "dismissed" is a human or rule decision not to pursue it.
#
# Every stage used to query Opportunity with no status filter at all, so all 18 open
# opportunities were re-evaluated, re-strategised and re-drafted on every single daily run --
# then the send stage blocked the drafts because those people had already been messaged. Real
# LLM cost, every day, that could never produce a send.
TERMINAL_STATUSES = {"converted", "dismissed"}


class Opportunity(Base):
    __tablename__ = "opportunities"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)  # nullable at the schema level (matches ProblemHypothesis/DemandHypothesis's own convention) -- but the eligibility gate never creates a row without one, see module docstring
    company_name_raw = Column(String, nullable=True)

    demand_hypothesis_id = Column(Integer, ForeignKey("demand_hypotheses.id"), nullable=False)  # exactly one Opportunity per DemandHypothesis -- see unique index in ensure_indexes()
    problem_hypothesis_id = Column(Integer, ForeignKey("problem_hypotheses.id"), nullable=False)  # denormalized from the DemandHypothesis's own required FK, for direct traceability without an extra join
    affected_function = Column(String, nullable=False)  # inherited from the DemandHypothesis, not re-derived

    opportunity_statement = Column(Text, nullable=False)  # plain language, source-grounded -- never a revenue/forecast claim
    reasoning_note = Column(Text, nullable=True)

    status = Column(String, nullable=False, default="candidate")  # "candidate" | "qualified" | "dismissed" | "converted"

    # Structured eligibility facts snapshot -- NOT a single numeric score (Part D's own explicit
    # instruction). Keys: qualifying_problem, qualifying_demand, independent_evidence_count,
    # sufficiently_independent, recent_enough, no_overwhelming_contradiction, has_company_identity.
    confidence = Column(JSON, nullable=True)

    # V2 Phase 2 -- ICP awareness (app/gtm_os/icp/icp_matching.py::get_icp_context_for_company).
    # A SOFT signal snapshot, taken at creation time, never a gate -- Problem+Demand eligibility
    # above is what actually decided this row exists. Keys: has_icp_match, matches (list of
    # {icp_id, icp_name, reasons, evaluated_at}), status ("matched" | "no_match_recorded" |
    # "no_company_identity"). No numeric ICP score anywhere -- see that function's own docstring
    # for why "no_match_recorded" is deliberately not split into a 3rd "insufficient" state.
    icp_context = Column(JSON, nullable=True)

    first_observed_at = Column(DateTime, nullable=True)  # copied from the DemandHypothesis at creation time
    last_updated_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)


def _generic_opportunity_statement(demand: DemandHypothesis) -> str:
    company = demand.company_name_raw or "This company"
    function = demand.affected_function or "an unspecified function"
    return f"{company} shows sufficient independent evidence of a {function}-related problem and outward-looking demand to be worth evaluating as a commercial opportunity."


def evaluate_opportunity_eligibility(
    problem: ProblemHypothesis,
    demand: DemandHypothesis,
    config: dict,
    now: datetime | None = None,
) -> dict:
    """Pure, structured AND-decision (Part D) -- never a single opaque score. Every fact named
    explicitly in gpt.txt's own example is computed here: qualifying_problem, qualifying_demand,
    evidence_recent, evidence_independent, no_overwhelming_contradiction -- plus the hard,
    non-calibration company-identity requirement (Part E)."""
    now = now or datetime.utcnow()
    problem_confidence = problem.confidence or {}
    demand_confidence = demand.confidence or {}

    reasons_failed = []

    qualifying_problem = problem_confidence.get("best_evidence_tier") in OPENING_TIERS
    if not qualifying_problem:
        reasons_failed.append(f"underlying ProblemHypothesis best_evidence_tier {problem_confidence.get('best_evidence_tier')!r} does not qualify")

    qualifying_demand = demand_confidence.get("best_evidence_tier") in DEMAND_QUALIFYING_TIERS
    if not qualifying_demand:
        reasons_failed.append(f"DemandHypothesis best_evidence_tier {demand_confidence.get('best_evidence_tier')!r} does not qualify")

    independent_evidence_count = demand_confidence.get("independent_evidence_count", 0)
    sufficiently_independent = independent_evidence_count >= config["min_independent_evidence"]
    if not sufficiently_independent:
        reasons_failed.append(f"independent_evidence_count {independent_evidence_count} < required {config['min_independent_evidence']}")

    recent_enough = False
    if demand.last_updated_at is not None:
        age_days = (now - demand.last_updated_at).days
        recent_enough = age_days <= config["recent_window_days"]
        if not recent_enough:
            reasons_failed.append(f"DemandHypothesis last_updated_at is {age_days} day(s) old, outside the {config['recent_window_days']}-day recent window")
    else:
        reasons_failed.append("DemandHypothesis has no last_updated_at to evaluate recency")

    demand_balanced = demand_confidence.get("contradicting_count", 0) <= demand_confidence.get("supporting_count", 0)
    problem_balanced = problem_confidence.get("contradicting_count", 0) <= problem_confidence.get("supporting_count", 0)
    no_overwhelming_contradiction = demand_balanced and problem_balanced
    if not no_overwhelming_contradiction:
        reasons_failed.append("contradicting evidence outweighs supporting evidence on the Problem and/or Demand hypothesis")

    has_company_identity = demand.company_id is not None
    if not has_company_identity:
        reasons_failed.append("no company_id -- cannot create an account-scoped Opportunity without a known account (Part E)")

    eligible = len(reasons_failed) == 0
    reason = "sufficient, independent, recent, uncontradicted evidence for a known account" if eligible else "; ".join(reasons_failed)

    return {
        "qualifying_problem": qualifying_problem,
        "qualifying_demand": qualifying_demand,
        "independent_evidence_count": independent_evidence_count,
        "sufficiently_independent": sufficiently_independent,
        "recent_enough": recent_enough,
        "no_overwhelming_contradiction": no_overwhelming_contradiction,
        "has_company_identity": has_company_identity,
        "eligible": eligible,
        "reason": reason,
    }


def _existing_opportunity_id(db: Session, tenant_id: int, demand_hypothesis_id: int) -> int | None:
    row = (
        db.query(Opportunity.id)
        .filter(Opportunity.tenant_id == tenant_id, Opportunity.demand_hypothesis_id == demand_hypothesis_id)
        .first()
    )
    return row[0] if row else None


def create_opportunity(db: Session, tenant_id: int, problem: ProblemHypothesis, demand: DemandHypothesis, eligibility: dict) -> Opportunity:
    """The only function in this module that writes. Never modifies ProblemHypothesis,
    DemandHypothesis, or their evidence rows -- purely a new, additive classification on top of
    already-complete, already-evidenced data.

    icp_context is a read-only snapshot (get_icp_context_for_company reads existing ICPMatch
    rows, never re-evaluates them) -- a soft signal attached for downstream Strategy/Account
    Agent visibility, never part of the eligibility decision above."""
    opportunity = Opportunity(
        tenant_id=tenant_id,
        company_id=demand.company_id,
        company_name_raw=demand.company_name_raw,
        demand_hypothesis_id=demand.id,
        problem_hypothesis_id=problem.id,
        affected_function=demand.affected_function,
        opportunity_statement=demand.demand_statement or _generic_opportunity_statement(demand),
        reasoning_note=f"Opportunity opened: {eligibility['reason']}",
        status="candidate",
        confidence=eligibility,
        icp_context=get_icp_context_for_company(db, tenant_id, demand.company_id),
        first_observed_at=demand.first_observed_at,
    )
    db.add(opportunity)
    db.commit()
    return opportunity


def run_opportunity_intelligence_sweep(db: Session, tenant_id: int, limit: int = 200) -> dict:
    """Evaluates up to `limit` DemandHypothesis rows for one tenant, creating an Opportunity for
    each newly-eligible one. Deliberately NOT wired into the scheduler yet (Part L) -- callable
    directly/manually until real data justifies a controlled scheduling decision.

    One DemandHypothesis's failure never aborts the sweep -- same per-item error-isolation style
    as every other sweep in this feature (promotion.py/trend_intelligence.py)."""
    config = get_opportunity_config(db, tenant_id)

    counts = {
        "evaluated": 0,
        "eligible": 0,
        "created": 0,
        "not_yet_eligible": 0,
        "no_company_identity": 0,
        "already_exists": 0,
        "failures": 0,
    }

    # V2 Phase 2 prioritization -- ICP-matched companies' DemandHypothesis rows are evaluated
    # FIRST (still subject to the same `limit`), so when there are more pending hypotheses than
    # `limit` allows evaluating this pass, ICP-matched ones become Opportunities sooner. A
    # correlated EXISTS against the already-computed ICPMatch table, not a new computation and
    # not a numeric score -- pure boolean ORDER BY, same eligibility logic below either way.
    icp_matched_exists = (
        exists()
        .where(ICPMatch.tenant_id == tenant_id, ICPMatch.company_id == DemandHypothesis.company_id)
    )
    demand_ids = [
        row[0]
        for row in db.query(DemandHypothesis.id)
        .filter(DemandHypothesis.tenant_id == tenant_id)
        .order_by(icp_matched_exists.desc(), DemandHypothesis.id)
        .limit(limit)
        .all()
    ]

    for demand_id in demand_ids:
        try:
            demand = db.get(DemandHypothesis, demand_id)
            if demand is None or demand.tenant_id != tenant_id:
                continue
            counts["evaluated"] += 1

            if _existing_opportunity_id(db, tenant_id, demand.id) is not None:
                counts["already_exists"] += 1
                continue

            problem = db.get(ProblemHypothesis, demand.problem_hypothesis_id)
            if problem is None:
                counts["failures"] += 1
                continue

            eligibility = evaluate_opportunity_eligibility(problem, demand, config)

            if not eligibility["has_company_identity"]:
                counts["no_company_identity"] += 1
                continue

            if not eligibility["eligible"]:
                counts["not_yet_eligible"] += 1
                continue

            counts["eligible"] += 1
            create_opportunity(db, tenant_id, problem, demand, eligibility)
            counts["created"] += 1

        except Exception:  # noqa: BLE001 -- one hypothesis's failure must never block the others, see docstring
            counts["failures"] += 1

    return counts
