"""Sales Agent Foundation -- Batch 6. Execution-PREPARATION only, never execution -- see
app/gtm_os/sales/__init__.py's module docstring for the hard import boundary.

PERSISTENCE DECISION (Part C): NOTHING new is persisted by this module. Account research,
decision-maker evaluation, message preparation, and readiness evaluation are all pure, read-only,
computed-on-demand functions -- exactly the same "read, don't recompute, don't duplicate" pattern
already proven by trend_intelligence.py (zero persistent Trend model) and promotion.py's
evaluate_cluster(). The Action Plan itself already lives on GtmStrategy (Batch 5) and is reused
unmodified, not duplicated into a new table -- per Part C's own explicit instruction ("if the
existing embedded action list is sufficient... reuse it instead of creating another table"). This
also makes idempotency trivial by construction (Part M): nothing is ever written, so there is
nothing to duplicate on a rerun -- recomputing from unchanged underlying data always produces an
identical result.

MESSAGE-GENERATION NOTE: this codebase already has a real, approved message-generation component
-- Phase 13's PersonalizedMessage (app/db/models.py) + its company/contact research + LLM
synthesis pipeline. It is DELIBERATELY NOT reused/called here: that pipeline operates on a
completely different input (Score/batch-based Company/Contact rows from the Deepline/Jobo
discovery pipeline) and makes real paid enrichment calls (Crustdata/Aviato) plus real LLM
synthesis calls per invocation. Wiring it into this batch's Opportunity/GtmStrategy-driven flow
would mean a sweep silently spending money and generating real message text -- exactly what Part G
says NOT to do ("DO NOT generate a polished outbound message yet"). prepare_message() below
produces only the structured PREPARATION object Part G actually asks for."""

from app.db.models import Company, Contact
from app.gtm_os.context.business_context import get_business_context
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.strategy.strategy import GtmStrategy, market_context

READINESS_STATES = {
    "insufficient_context",
    "decision_maker_required",
    "problem_validation_required",
    "demand_validation_required",
    "offering_fit_required",
    "ready_for_message",
}

# COMPANY_RESEARCH_FIELDS: real Company columns that MAY already be populated by the separate,
# existing Score/batch discovery pipeline (Phase 3/5/8) for this same company row. Reusing them
# here is not fabrication -- it's exactly Part E's "gather ONLY information that is actually
# available from existing sources" -- but every field is represented explicitly as present or
# missing, never defaulted/guessed when unset.
COMPANY_RESEARCH_FIELDS = (
    "name", "domain", "industry", "employee_count", "location", "linkedin_url", "source",
    "estimated_revenue_lower_usd", "estimated_revenue_higher_usd",
    "last_funding_round_type", "last_funding_date", "headcount_growth_12m_percent",
    "hiring_signal_role", "hiring_signal_hire_type", "hiring_signal_strength",
    "hiring_signal_reasoning", "hiring_signal_posting_count", "active_job_title",
    "detected_tech_stack", "has_outbound_tooling", "has_ai_sdr_tool",
)


def gather_account_research(db, tenant_id: int, opportunity: Opportunity) -> dict:
    """Read-only account research (Part E). Every field is either a real value already present
    on the Company row, or explicitly None ("missing") -- nothing is invented. Preserves
    provenance: each fact is traceable back to the specific model/column it came from."""
    research = {
        "company_id": opportunity.company_id,
        "company_name": opportunity.company_name_raw,
        "company_fields": {},
        "known_contacts": [],
        "known_content_signals": [],
        "known_problem_evidence": {
            "problem_hypothesis_id": opportunity.problem_hypothesis_id,
            "problem_statement": None,
        },
        "known_demand_evidence": {
            "demand_hypothesis_id": opportunity.demand_hypothesis_id,
            "demand_statement": None,
        },
        "relevant_market_context": market_context(db, tenant_id),
        "missing_information": [],
    }

    if opportunity.company_id is None:
        research["missing_information"].append("no company_id -- account research cannot proceed beyond what the Opportunity already carries")
        return research

    company = db.get(Company, opportunity.company_id)
    if company is None:
        research["missing_information"].append(f"company_id {opportunity.company_id} does not resolve to a real Company row")
        return research

    for field in COMPANY_RESEARCH_FIELDS:
        value = getattr(company, field, None)
        research["company_fields"][field] = value
        if value is None:
            research["missing_information"].append(f"Company.{field} not available")

    contacts = db.query(Contact).filter(Contact.company_id == company.id).all()
    research["known_contacts"] = [
        {"id": c.id, "first_name": c.first_name, "last_name": c.last_name, "title": c.title, "linkedin_url": c.linkedin_url, "has_email": c.email is not None}
        for c in contacts
    ]
    if not contacts:
        research["missing_information"].append("no known Contact rows for this company")

    content_signals = (
        db.query(GtmSignal)
        .filter(GtmSignal.tenant_id == tenant_id, GtmSignal.company_id == company.id)
        .order_by(GtmSignal.id.desc())
        .limit(20)
        .all()
    )
    research["known_content_signals"] = [
        {"id": s.id, "source": s.source, "signal_type": s.signal_type, "observed_at": s.observed_at}
        for s in content_signals
    ]

    problem = db.get(ProblemHypothesis, opportunity.problem_hypothesis_id)
    if problem is not None:
        research["known_problem_evidence"]["problem_statement"] = problem.problem_statement
    demand = db.get(DemandHypothesis, opportunity.demand_hypothesis_id)
    if demand is not None:
        research["known_demand_evidence"]["demand_statement"] = demand.demand_statement

    return research


def evaluate_decision_maker(db, tenant_id: int, company_id: int | None) -> dict:
    """Decision-maker discovery abstraction (Part F) -- read-only against already-known Contact
    rows. Never performs external discovery, never invents a name."""
    if company_id is None:
        return {"status": "required", "reason": "no_company_identity", "contacts": []}

    contacts = db.query(Contact).filter(Contact.company_id == company_id).all()
    if not contacts:
        return {"status": "required", "reason": "no_relevant_contact_available", "contacts": []}

    return {
        "status": "known",
        "reason": None,
        "contacts": [
            {"id": c.id, "name": f"{c.first_name or ''} {c.last_name or ''}".strip() or None, "title": c.title, "linkedin_url": c.linkedin_url, "has_email": c.email is not None}
            for c in contacts
        ],
    }


def build_sales_handoff(db, tenant_id: int, opportunity: Opportunity, strategy: GtmStrategy) -> dict:
    """The clean Strategy -> Sales handoff package (Part B). The Sales Agent (not built --
    execution is explicitly out of scope, Part H/S) would consume exactly this and nothing more
    -- it never independently reinterprets business_context or invents an opportunity; the
    Strategy remains the decision layer, this is purely an assembled read of what already exists."""
    research = gather_account_research(db, tenant_id, opportunity)
    decision_maker = evaluate_decision_maker(db, tenant_id, opportunity.company_id)
    business_context = get_business_context(db, tenant_id)

    return {
        "opportunity": {
            "id": opportunity.id,
            "company_id": opportunity.company_id,
            "company_name": opportunity.company_name_raw,
            "affected_function": opportunity.affected_function,
            "opportunity_statement": opportunity.opportunity_statement,
            "status": opportunity.status,
        },
        "strategy": {
            "id": strategy.id,
            "strategy_type": strategy.strategy_type,
            "recommended_approach": strategy.recommended_approach,
            "positioning_angle": strategy.positioning_angle,
            "offering_fit_status": strategy.offering_fit_status,
            "matched_offering_name": strategy.matched_offering_name,
            "reasoning_note": strategy.reasoning_note,
        },
        "action_plan": strategy.action_plan or [],
        "account_research": research,
        "decision_maker": decision_maker,
        # General, tenant-wide reference context (Part J) -- passed through verbatim, never
        # selected/matched against this specific opportunity (same boundary as market_context).
        "business_context_reference": {
            "gtm_motions": business_context.get("gtm_motions", []),
            "sales_methodology": business_context.get("sales_methodology", []),
            "messaging_objections": business_context.get("messaging_objections", []),
        },
        "evidence_basis": strategy.evidence_basis,
        "missing_information": strategy.missing_information or [],
    }


def prepare_message(db, tenant_id: int, opportunity: Opportunity, strategy: GtmStrategy, research: dict, decision_maker: dict) -> dict:
    """Structured message-PREPARATION object (Part G) -- never a sent or even a fully-drafted
    message. No LLM call anywhere in this function."""
    missing_information = list(strategy.missing_information or [])

    if strategy.strategy_type == "insufficient_context":
        missing_information.append("strategy is insufficient_context -- no approach has been determined yet")
        return {"status": "insufficient_context", "channel": None, "objective": None, "target_role": None,
                "positioning_angle": None, "evidence_basis": strategy.evidence_basis, "personalization_inputs": [],
                "missing_information": missing_information}

    if decision_maker["status"] != "known":
        missing_information.append("no known decision-maker contact -- cannot target a message")
        return {"status": "insufficient_context", "channel": None, "objective": None, "target_role": None,
                "positioning_angle": strategy.positioning_angle, "evidence_basis": strategy.evidence_basis,
                "personalization_inputs": [], "missing_information": missing_information}

    if strategy.offering_fit_status != "candidate_match":
        missing_information.append(f"offering_fit_status is {strategy.offering_fit_status!r} -- cannot credibly position a message without a confirmed offering")
        return {"status": "insufficient_context", "channel": None, "objective": None,
                "target_role": decision_maker["contacts"][0]["title"], "positioning_angle": strategy.positioning_angle,
                "evidence_basis": strategy.evidence_basis, "personalization_inputs": [], "missing_information": missing_information}

    next_action_types = [a["action_type"] for a in (strategy.action_plan or [])]
    if "prepare_message" not in next_action_types:
        missing_information.append("action plan has not yet reached prepare_message -- earlier prerequisites still open")
        return {"status": "insufficient_context", "channel": None, "objective": None,
                "target_role": decision_maker["contacts"][0]["title"], "positioning_angle": strategy.positioning_angle,
                "evidence_basis": strategy.evidence_basis, "personalization_inputs": [], "missing_information": missing_information}

    contact = decision_maker["contacts"][0]
    channel = "linkedin" if contact.get("linkedin_url") else ("email" if contact.get("has_email") else None)
    if channel is None:
        missing_information.append("known contact has neither a LinkedIn URL nor an email -- no channel to prepare a message for")

    personalization_inputs = []
    if research["known_problem_evidence"]["problem_statement"]:
        personalization_inputs.append(f"problem: {research['known_problem_evidence']['problem_statement']}")
    if research["known_demand_evidence"]["demand_statement"]:
        personalization_inputs.append(f"demand: {research['known_demand_evidence']['demand_statement']}")
    if strategy.matched_offering_name:
        personalization_inputs.append(f"matched offering: {strategy.matched_offering_name}")

    return {
        "status": "ready" if channel else "insufficient_context",
        "channel": channel,
        "objective": next((a["objective"] for a in strategy.action_plan if a["action_type"] == "prepare_message"), None),
        "target_role": contact.get("title"),
        "positioning_angle": strategy.positioning_angle,
        "evidence_basis": strategy.evidence_basis,
        "personalization_inputs": personalization_inputs,
        "missing_information": missing_information,
    }


def evaluate_sales_readiness(strategy: GtmStrategy) -> dict:
    """Deterministic readiness evaluation (Part I) -- derived entirely from GtmStrategy's own
    already-computed action_plan (Batch 5), never recomputed independently. No opaque score --
    every result names the blocking prerequisite and the next action explicitly.

    States used (6 of the 8 example states -- "research_required" and "ready_for_human_review"
    deliberately dropped, see module docstring's own reasoning: research is never itself a gate
    in the current action chain, and nothing in this codebase distinguishes a separate
    "human review" checkpoint from "ready_for_message")."""
    if strategy.strategy_type == "insufficient_context":
        return {"status": "insufficient_context", "reason": "GtmStrategy could not determine a strategy type from current evidence.", "next_action": None, "blocking_prerequisite": None}

    action_plan = strategy.action_plan or []
    if not action_plan:
        return {"status": "insufficient_context", "reason": "no action plan was generated for this strategy.", "next_action": None, "blocking_prerequisite": None}

    last_action = action_plan[-1]
    action_type = last_action["action_type"]

    status_by_action_type = {
        "identify_decision_maker": "decision_maker_required",
        "validate_problem": "problem_validation_required",
        "validate_demand": "demand_validation_required",
        "review_offering_fit": "offering_fit_required",
        "request_meeting": "ready_for_message",  # chain reached the end -- prepare_message already satisfied
    }
    status = status_by_action_type.get(action_type, "insufficient_context")

    return {
        "status": status,
        "reason": last_action["rationale"],
        "next_action": action_type if status != "ready_for_message" else "prepare_message",
        "blocking_prerequisite": last_action["prerequisite"] if status != "ready_for_message" else None,
    }


def run_sales_agent_sweep(db, tenant_id: int, limit: int = 200, dry_run: bool = False) -> dict:
    """Reporting sweep only -- writes NOTHING (see module docstring). `dry_run` accepted purely
    for interface consistency with this feature's other sweeps; behavior is identical either way.

    One opportunity's failure never aborts the sweep -- same per-item error-isolation style as
    every other sweep in this feature. `evaluated`/`ready`/`blocked`/`skipped`/`failed` match
    Part O's exact required counts: `skipped` = no GtmStrategy exists yet for that Opportunity
    (Batch 5's own sweep hasn't run for it), `blocked` = a real, explained readiness gap."""
    counts = {"evaluated": 0, "ready": 0, "blocked": 0, "skipped": 0, "failed": 0, "dry_run": dry_run}

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
            opportunity = db.get(Opportunity, opportunity_id)
            if opportunity is None or opportunity.tenant_id != tenant_id:
                continue
            counts["evaluated"] += 1

            strategy = (
                db.query(GtmStrategy)
                .filter(GtmStrategy.tenant_id == tenant_id, GtmStrategy.opportunity_id == opportunity.id)
                .order_by(GtmStrategy.id.desc())
                .first()
            )
            if strategy is None:
                counts["skipped"] += 1
                continue

            readiness = evaluate_sales_readiness(strategy)
            if readiness["status"] == "ready_for_message":
                counts["ready"] += 1
            else:
                counts["blocked"] += 1

        except Exception:  # noqa: BLE001 -- one opportunity's failure must never block the others, see docstring
            counts["failed"] += 1

    return counts
