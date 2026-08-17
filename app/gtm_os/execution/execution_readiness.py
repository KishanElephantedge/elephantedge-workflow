"""Execution Readiness -- Batch 13. Deterministic, read-only, no LLM. Answers exactly the six
questions Part 3 of the spec asks for, by reading (never recomputing) what GtmStrategy (Batch 5),
evaluate_sales_readiness() (Batch 6), and MessageDraft (Batch 7) already established:

    1. What action is currently recommended?      -> `action`
    2. Why is it recommended?                       -> `reason`
    3. What prerequisite is blocking it?             -> `blocking_prerequisite`
    4. Is the account ready for the action?          -> `status`
    5. Does the action require human approval?       -> `requires_human_review`
    6. What evidence supports the action?            -> `evidence_basis`

NOT A SECOND STRATEGY/READINESS ENGINE (Part 2/10): this module never re-derives strategy_type,
offering fit, or a readiness verdict -- it reads GtmStrategy's own fields and calls
evaluate_sales_readiness() unmodified. The only genuinely new logic here is the thin layer that
sits ON TOP of readiness once it reaches "ready_for_message": whether a MessageDraft already
exists for this exact (opportunity, strategy version) pair, and if so, what its own
already-governed lifecycle status is (Batch 7's insufficient_context -> draft -> ready_for_review
-> approved, read verbatim, never inferred -- Part 8's human-approval boundary is preserved by
construction: this module contains no call to approve_message_draft() and no call to
generate_message_draft() either, see below).

CRITICAL SAFETY PROPERTY (Part 7/13): get_next_execution_action() NEVER calls
generate_message_draft(). It only QUERIES the message_drafts table for an existing row. Calling
generate_message_draft() triggers a real LLM call (Batch 7's generate_json()) on a cache miss --
an execution-readiness READ must never have that side effect, or every repeated call / every
sweep run would risk spending money. Actually triggering message generation remains a deliberate,
separate, explicit action for a caller to take (by calling generate_message_draft() itself,
exactly as Batch 7 already exposes it) -- this module only ever reports whether that step is
AVAILABLE (`message_generation_available`), never performs it.

EXECUTION STATES (Part 5) -- only states genuinely derivable from data that exists today:
    no_opportunity            -- opportunity_id does not resolve
    strategy_required          -- no GtmStrategy exists yet for this Opportunity
    insufficient_context       -- GtmStrategy exists but strategy_type == insufficient_context
    blocked                    -- evaluate_sales_readiness() status != ready_for_message
    message_generation_available -- readiness is ready_for_message, no MessageDraft exists yet
    message_insufficient_context -- a MessageDraft exists but its own status is insufficient_context
    message_blocked_by_quality_gate -- a MessageDraft exists with status "draft" (failed Part J's quality gate)
    ready_for_review            -- a MessageDraft exists with status "ready_for_review"
    approved                    -- a MessageDraft exists with status "approved"
    message_rejected            -- a MessageDraft exists with status "rejected" (Phase 7)
    message_changes_requested   -- a MessageDraft exists with status "changes_requested" (Phase 7)
"completed" is deliberately NOT a state (Part 5's own explicit instruction) -- no mechanism in
this codebase marks a recommended execution action as done; SalesOutcome (Batch 7) records an
OBSERVED reply event, which is a different concept entirely (see `_note_on_outcomes` below and
Part 9) and is never conflated with "the action completed."

REQUIRES_HUMAN_REVIEW (Part 3 Q5): True for every action from "prepare_message" onward in
ACTION_CHAIN_ORDER, and for every message-lifecycle state at or beyond a drafted message
(message_insufficient_context / message_blocked_by_quality_gate / ready_for_review / approved) --
because nothing in this codebase auto-sends, auto-approves, or auto-executes anything past that
point, a human is always the one who must act next. False for the earlier evidence-gathering
chain steps (research_account/identify_decision_maker/validate_problem/validate_demand/
review_offering_fit), which the system's own sweeps (Batches 6/8/9) already populate without
requiring a human to manually trigger them.

PERSISTENCE DECISION (Part 12): NO new table. Every fact this module reports already lives on
Opportunity, GtmStrategy, or MessageDraft. get_next_execution_action() is pure and computed on
demand, same "prefer pure computation" precedent as account_agent.py/market_account_context.py/
gtm_motion.py/icp_offering_matching.py. No independently-tracked action lifecycle is required:
MessageDraft already IS that lifecycle for the one action type (message) that has one."""

from sqlalchemy.orm import Session

from app.db.models import Company
from app.gtm_os.account_agent.account_agent import build_account_brief
from app.gtm_os.gtm_motion.gtm_motion import recommend_gtm_motion
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.sales.sales_agent import evaluate_sales_readiness
from app.gtm_os.strategy.strategy import GtmStrategy

EXECUTION_STATUSES = {
    "no_opportunity",
    "strategy_required",
    "insufficient_context",
    "blocked",
    "message_generation_available",
    "message_insufficient_context",
    "message_blocked_by_quality_gate",
    "ready_for_review",
    "approved",
    "message_rejected",       # Phase 7 -- a human explicitly rejected this MessageDraft
    "message_changes_requested",  # Phase 7 -- a human requested changes to this MessageDraft
}

# Action types from ACTION_CHAIN_ORDER at or after which a human must act next (see module
# docstring's REQUIRES_HUMAN_REVIEW section) -- reused by name, never a second vocabulary.
_HUMAN_REVIEW_ACTIONS = {"prepare_message", "request_meeting"}


def _latest_strategy(db: Session, tenant_id: int, opportunity_id: int) -> GtmStrategy | None:
    return (
        db.query(GtmStrategy)
        .filter(GtmStrategy.tenant_id == tenant_id, GtmStrategy.opportunity_id == opportunity_id)
        .order_by(GtmStrategy.id.desc())
        .first()
    )


def _existing_message_draft(db: Session, tenant_id: int, opportunity_id: int, gtm_strategy_id: int) -> MessageDraft | None:
    return (
        db.query(MessageDraft)
        .filter(
            MessageDraft.tenant_id == tenant_id,
            MessageDraft.opportunity_id == opportunity_id,
            MessageDraft.gtm_strategy_id == gtm_strategy_id,
        )
        .first()
    )


def get_next_execution_action(db: Session, tenant_id: int, opportunity_id: int) -> dict:
    """Pure, read-only. NEVER calls generate_message_draft() or approve_message_draft() -- see
    module docstring's CRITICAL SAFETY PROPERTY. Safe to call repeatedly with no side effects."""
    base = {
        "opportunity_id": opportunity_id,
        "status": None,
        "action": None,
        "reason": None,
        "blocking_prerequisite": None,
        "requires_human_review": False,
        "evidence_basis": None,
        "message_generation_available": False,
        "message_draft_exists": False,
        "message_ready_for_review": False,
        "message_approved": False,
    }

    opportunity = db.get(Opportunity, opportunity_id)
    if opportunity is None or opportunity.tenant_id != tenant_id:
        base["status"] = "no_opportunity"
        base["reason"] = f"opportunity_id {opportunity_id} does not resolve to a real Opportunity row for this tenant"
        return base

    strategy = _latest_strategy(db, tenant_id, opportunity.id)
    if strategy is None:
        base["status"] = "strategy_required"
        base["action"] = None
        base["reason"] = "no GtmStrategy exists yet for this Opportunity -- run the Batch 5 strategy sweep"
        return base

    base["evidence_basis"] = strategy.evidence_basis

    if strategy.strategy_type == "insufficient_context":
        base["status"] = "insufficient_context"
        base["reason"] = strategy.reasoning_note or "strategy is insufficient_context"
        base["missing_information"] = strategy.missing_information or []
        return base

    readiness = evaluate_sales_readiness(strategy)

    if readiness["status"] != "ready_for_message":
        base["status"] = "blocked"
        base["action"] = readiness["next_action"]
        base["reason"] = readiness["reason"]
        base["blocking_prerequisite"] = readiness["blocking_prerequisite"]
        base["requires_human_review"] = readiness["next_action"] in _HUMAN_REVIEW_ACTIONS
        return base

    # Readiness is ready_for_message -- message-drafting is the next step. Only QUERY for an
    # existing draft, never generate one (see module docstring's CRITICAL SAFETY PROPERTY).
    draft = _existing_message_draft(db, tenant_id, opportunity.id, strategy.id)

    if draft is None:
        base["status"] = "message_generation_available"
        base["action"] = "prepare_message"
        base["reason"] = "all prerequisites (decision maker, problem, demand, offering fit) are satisfied -- no MessageDraft has been generated yet"
        base["requires_human_review"] = True
        base["message_generation_available"] = True
        return base

    base["message_draft_exists"] = True

    if draft.status == "insufficient_context":
        base["status"] = "message_insufficient_context"
        base["action"] = None
        base["reason"] = "a MessageDraft was attempted but could not produce a usable message"
        base["blocking_prerequisite"] = None
        base["requires_human_review"] = True
        base["missing_information"] = draft.missing_information or []
        return base

    if draft.status == "draft":
        base["status"] = "message_blocked_by_quality_gate"
        base["action"] = None
        base["reason"] = "a MessageDraft exists but did not pass the deterministic quality gate"
        base["blocking_prerequisite"] = draft.quality_gate_reasons
        base["requires_human_review"] = True
        return base

    if draft.status == "ready_for_review":
        base["status"] = "ready_for_review"
        base["action"] = "approve_message_draft"
        base["reason"] = "MessageDraft passed the quality gate -- awaiting human review/approval"
        base["requires_human_review"] = True
        base["message_ready_for_review"] = True
        return base

    if draft.status == "rejected":
        base["status"] = "message_rejected"
        base["action"] = None
        base["reason"] = "a human rejected this MessageDraft -- no further automatic action is recommended"
        base["requires_human_review"] = False
        return base

    if draft.status == "changes_requested":
        base["status"] = "message_changes_requested"
        base["action"] = None
        base["reason"] = "a human requested changes to this MessageDraft -- no further automatic action is recommended"
        base["requires_human_review"] = False
        return base

    # draft.status == "approved"
    base["status"] = "approved"
    base["action"] = "request_meeting"
    base["reason"] = "MessageDraft has been approved by a human -- next chain step is requesting a meeting"
    base["requires_human_review"] = True
    base["message_ready_for_review"] = True
    base["message_approved"] = True
    return base


def get_execution_readiness_for_company(db: Session, tenant_id: int, company_id: int) -> dict:
    """Account Agent integration (Part 11) -- reuses build_account_brief() unmodified rather than
    recreating account aggregation. Computes get_next_execution_action() for each of the
    account's Opportunities (a company can have more than one)."""
    brief = build_account_brief(db, tenant_id, company_id)
    opportunity_entries = brief.get("opportunities", [])

    if not opportunity_entries:
        return {
            "company_id": company_id,
            "account_status": brief["account_status"],
            "opportunities": [],
            "missing_information": ["no Opportunity exists yet for this account -- nothing to execute against"],
        }

    return {
        "company_id": company_id,
        "account_status": brief["account_status"],
        "opportunities": [
            {
                "opportunity_id": entry["id"],
                "execution": get_next_execution_action(db, tenant_id, entry["id"]),
            }
            for entry in opportunity_entries
        ],
        "missing_information": [],
    }


def run_execution_readiness_sweep(db: Session, tenant_id: int, limit: int = 200) -> dict:
    """Bounded, standalone, tenant-scoped, read-only reporting sweep -- writes NOTHING (pure
    computation, see module docstring's persistence decision). One opportunity's failure never
    aborts the sweep. NOT wired into the scheduler (per scope boundary)."""
    counts = {status: 0 for status in EXECUTION_STATUSES}
    counts["evaluated"] = 0
    counts["failed"] = 0

    opportunity_ids = [
        row[0]
        for row in db.query(Opportunity.id)
        .filter(Opportunity.tenant_id == tenant_id)
        .order_by(Opportunity.id)
        .limit(limit)
        .all()
    ]

    for opportunity_id in opportunity_ids:
        try:
            counts["evaluated"] += 1
            result = get_next_execution_action(db, tenant_id, opportunity_id)
            counts[result["status"]] += 1
        except Exception:  # noqa: BLE001 -- one opportunity's failure must never block the others
            counts["failed"] += 1

    return counts


def _build_pipeline_item(db: Session, tenant_id: int, opp: Opportunity) -> dict:
    """Shared by list_pipeline_items() and get_pipeline_item() (Phase 7) so the single-opportunity
    read is never a second, drifted copy of the list's own per-item logic. Reuses
    get_next_execution_action() (strategy + evaluate_sales_readiness() + message-draft state, all
    unmodified) and recommend_gtm_motion() (Batch 10, unmodified) -- never re-derives strategy,
    readiness, or motion logic itself."""
    company = db.get(Company, opp.company_id) if opp.company_id else None
    problem = db.get(ProblemHypothesis, opp.problem_hypothesis_id)
    demand = db.get(DemandHypothesis, opp.demand_hypothesis_id)
    strategy = _latest_strategy(db, tenant_id, opp.id)
    execution = get_next_execution_action(db, tenant_id, opp.id)
    motion = recommend_gtm_motion(db, tenant_id, opp.company_id) if opp.company_id else None

    return {
        "opportunity_id": opp.id,
        "opportunity_status": opp.status,
        "company_id": opp.company_id,
        "company_name": opp.company_name_raw or (company.name if company else None),
        "affected_function": opp.affected_function,
        "opportunity_statement": opp.opportunity_statement,
        "problem_statement": problem.problem_statement if problem else None,
        "problem_tier": (problem.confidence or {}).get("best_evidence_tier") if problem else None,
        "demand_statement": demand.demand_statement if demand else None,
        "demand_tier": (demand.confidence or {}).get("best_evidence_tier") if demand else None,
        "strategy": None if strategy is None else {
            "id": strategy.id,
            "strategy_type": strategy.strategy_type,
            "offering_fit_status": strategy.offering_fit_status,
            "matched_offering_name": strategy.matched_offering_name,
            "recommended_next_step": strategy.recommended_next_step,
            "reasoning_note": strategy.reasoning_note,
            "action_plan": strategy.action_plan,
            "evidence_basis": strategy.evidence_basis,
        },
        "gtm_motion": None if motion is None else {
            "status": motion["status"],
            "primary_motion": motion["primary_motion"],
            "reason": motion.get("reason"),
        },
        "execution": execution,
    }


def list_pipeline_items(db: Session, tenant_id: int, page: int = 1, page_size: int = 25) -> dict:
    """Phase 3 (V2 Pipeline API) -- a paginated, cross-account list of Opportunities, each built
    by _build_pipeline_item() above. Bounded by `page_size` (default 25, matching the existing
    /companies convention), not by total company count -- one query per opportunity on this page
    is the same per-item cost every other sweep in this codebase already accepts at real data
    volume, capped here to a single page instead of the whole tenant."""
    page = max(page, 1)
    page_size = max(1, min(page_size, 100))

    total = db.query(Opportunity.id).filter(Opportunity.tenant_id == tenant_id).count()
    opportunities = (
        db.query(Opportunity)
        .filter(Opportunity.tenant_id == tenant_id)
        .order_by(Opportunity.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
        "items": [_build_pipeline_item(db, tenant_id, opp) for opp in opportunities],
    }


def get_pipeline_item(db: Session, tenant_id: int, opportunity_id: int) -> dict | None:
    """Phase 7 (V2 Opportunity Detail API) -- the single-opportunity counterpart to
    list_pipeline_items(), reusing the exact same _build_pipeline_item() so the detail page can
    never show facts that disagree with what the list page already showed. Returns None (not an
    exception) when the opportunity doesn't resolve for this tenant -- the caller (the API route)
    turns that into a 404, same pattern as every other /gtm-os/* route in this codebase."""
    opp = db.get(Opportunity, opportunity_id)
    if opp is None or opp.tenant_id != tenant_id:
        return None
    return _build_pipeline_item(db, tenant_id, opp)
