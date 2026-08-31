"""GTM Strategy + Action Planning -- Batch 5. Deterministic only (Part M) -- we need to know
what structured information the system actually has before ever asking an LLM to synthesize
strategy from it; an LLM layer can be considered later, on top of THIS proven structured model,
not instead of it.

STRATEGY TAXONOMY (Part F) -- only types justifiable from data this codebase actually has today:
    insufficient_context  -- affected_function unknown, or (defensively) Problem/Demand tiers
                              don't actually qualify despite an Opportunity row existing
    diagnostic             -- Problem is only "implied_gap" tier (not yet explicitly declared) --
                              needs a diagnostic conversation to confirm/quantify it
    consultative            -- Problem is "declared" but Demand is only "demand" tier (general
                              outward-looking, not yet strong buying intent) -- explore fit
    execution-led            -- Problem "declared" AND Demand "buying_intent_adjacent" -- ready to
                              move toward concrete engagement
    nurture                  -- evidence otherwise supports engagement, but every configured
                              offering is "no_match"/"excluded" -- stay on radar, nothing to
                              actively pitch right now
"educational" and "product-led" were considered and deliberately excluded -- neither has a
data-backed trigger condition anywhere in this codebase today (no content-consumption/education
signal exists on any hypothesis, and "product-led" would require self-serve product usage data
this system has no source for). Adding them now would be guessing labels, not deriving them.

ACTION PLAN (Part I) -- deliberately embedded as a JSON list on GtmStrategy itself, not a new
table. An action here is a recommendation only, immutable once the strategy row is created, with
no independent lifecycle of its own yet (never executed by anything in this codebase). Promoting
it to its own table is the right move ONLY once a real execution layer needs to track individual
action state independently -- the same "smallest structure now, promote later if needed"
discipline this whole feature has followed throughout (e.g. TopicCandidate.cluster_key vs. a full
cluster table, Step 16E-5).

MARKET CONTEXT (Part L) -- pulls currently-active (emerging/accelerating/persistent)
ContentTopics as GENERAL context only, never matched to a specific opportunity (no fuzzy/inferred
link between an Opportunity's affected_function and any ContentTopic exists in this codebase, and
inventing one would be exactly the "let a trend automatically choose an offering" anti-pattern
Part L explicitly forbids). Included only in evidence_basis/reasoning_note -- NEVER read by
_select_strategy_type() or the offering-fit logic, so a trend can inform the explanation but can
structurally never change the decision itself."""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
import logging

from sqlalchemy.orm import Session

from app.db.models import Base, Contact
from app.gtm_os.content.topic import ContentTopic
from app.gtm_os.content.trend_intelligence import evaluate_topic_trend
from app.gtm_os.content.trend_config import get_trend_config
from app.gtm_os.intelligence.demand_detection import DEMAND_QUALIFYING_TIERS
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis, DemandHypothesisEvidence
from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis, ProblemHypothesisEvidence
from app.gtm_os.icp.icp_offering_matching import match_offerings_for_opportunity

logger = logging.getLogger(__name__)
from app.gtm_os.opportunity.opportunity import Opportunity

STRATEGY_TYPES = {"insufficient_context", "diagnostic", "consultative", "execution-led", "nurture"}
ACTIVE_TREND_STATES = {"emerging", "accelerating", "persistent"}

# Deterministic action chain (Part J), in dependency order. `prerequisite` on each action is the
# PREVIOUS action_type in this list, or None for the first -- a plain, explicit chain, not a
# generic dependency graph (Part J's own "do not over-engineer" instruction).
ACTION_CHAIN_ORDER = [
    "research_account",
    "identify_decision_maker",
    "validate_problem",
    "validate_demand",
    "review_offering_fit",
    "prepare_message",
    "request_meeting",
]


class GtmStrategy(Base):
    """One row per strategy EVALUATION, not one row per Opportunity -- multiple rows can exist
    for the same opportunity_id over time (Part N's "preserve previous versions rather than
    destroying them"). Never updated in place; the sweep only ever inserts a new row when the
    computed strategy actually differs from the most recent existing one for that opportunity
    (see _latest_strategy_for_opportunity / run_gtm_strategy_sweep) -- "current" is simply
    whichever row has the highest id/created_at for a given opportunity_id, no separate
    superseded/is_current flag needed (Part N's own "do not create speculative versioning
    infrastructure unless actually needed")."""
    __tablename__ = "gtm_strategies"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False)

    strategy_type = Column(String, nullable=False)  # one of STRATEGY_TYPES -- doubles as "strategy status" (Part C) and "taxonomy" (Part F), see module docstring
    recommended_approach = Column(Text, nullable=True)  # short plain-language recommendation, null when insufficient_context
    target_function = Column(String, nullable=True)  # copied from Opportunity.affected_function when known

    positioning_angle = Column(Text, nullable=True)
    offering_fit_status = Column(String, nullable=True)  # "candidate_match" | "no_match" | "excluded" | "insufficient_offering_context"
    matched_offering_name = Column(String, nullable=True)  # set only when offering_fit_status == "candidate_match"

    # Structured facts this strategy was built from -- NOT a score. Includes problem/demand
    # tiers, independent_evidence_count, offering match summary, and general market_context (see
    # module docstring) -- everything needed to answer "why," without re-deriving it from scratch.
    evidence_basis = Column(JSON, nullable=True)
    constraints = Column(JSON, nullable=True)  # list[str] -- from offering exclusions / business context, when known
    missing_information = Column(JSON, nullable=True)  # list[str]

    action_plan = Column(JSON, nullable=True)  # list[{action_type, objective, target_function, rationale, prerequisite, status}]
    recommended_next_step = Column(String, nullable=True)  # the first action_type in action_plan, for quick access

    # V2 Phase 6 (upstream staleness fix, 2026-08-21) -- the exact _decision_maker_known() value
    # this strategy was built from, snapshotted as its own narrow, dedicated field rather than
    # inferred from action_plan/recommended_next_step (which don't reliably change when this
    # flips -- recommended_next_step is always "research_account", the first action added
    # unconditionally). Added to _COMPARABLE_KEYS below so _facts_changed() detects a real
    # false->true (or true->false, e.g. a contact getting suppressed) transition and
    # run_gtm_strategy_sweep() re-versions -- see that function's own docstring for why this was
    # previously a silent dead end.
    decision_maker_known = Column(Boolean, nullable=True)

    reasoning_note = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    last_updated_at = Column(DateTime, default=datetime.utcnow)  # always equals created_at in practice -- rows are immutable, kept for schema consistency with sibling models


def _decision_maker_known(db: Session, company_id: int) -> bool:
    """V2 Phase 3/4 fix: a suppressed Contact (excluded_from_push) must NOT count as "known" --
    previously any Contact row at all satisfied this, including one nobody could actually be
    messaged through. Still only requires ONE eligible contact to be considered "known" (not
    the full up-to-3 target) -- that higher bar is V2's OWN contact-discovery-stage top-up
    eligibility check (app/gtm_os/sales/contact_discovery.py), a different question from
    "can the action chain move past identify_decision_maker at all.\""""
    return (
        db.query(Contact.id)
        .filter(Contact.company_id == company_id, Contact.excluded_from_push.isnot(True))
        .first()
        is not None
    )


def market_context(db: Session, tenant_id: int) -> list[dict]:
    """General, unmatched market context -- see module docstring for why this is deliberately
    NOT linked to any specific opportunity. Bounded to configured ContentTopics only (a small,
    known set per tenant, not an unbounded query).

    Public (no leading underscore) -- reused as-is by app/gtm_os/sales/sales_agent.py (Batch 6)
    rather than duplicated."""
    trend_config = get_trend_config(db, tenant_id)
    topics = db.query(ContentTopic).filter(ContentTopic.tenant_id == tenant_id).all()
    context = []
    for topic in topics:
        result = evaluate_topic_trend(db, tenant_id, topic, trend_config)
        if result["state"] in ACTIVE_TREND_STATES:
            context.append({"topic": topic.canonical_name, "origin": topic.origin, "state": result["state"]})
    return context


# 2026-08-25, explicit instruction: these three event types were built specifically so that a
# JD-verified first sales hire, a genuinely counted concurrent multi-role hiring surge, or a real
# head_of_sales-type hire, would each already BE the qualification work (real JD content read,
# real postings counted, a real ICP-defining role type matched) -- not a raw, unverified signal
# still awaiting confirmation the way a bare LinkedIn post inference is. Making the strategy layer
# ask to "validate_problem" again for these specifically would just repeat verification already
# done upstream. See linkedin_job_interpretation.py / interpretation.py's
# _promote_concurrent_hiring_signals for where each of these is actually produced and verified.
SELF_VERIFIED_HIRING_EVENT_TYPES = {"first_sales_hire_signal", "concurrent_hiring_surge", "leadership_hire_signal"}


def _problem_has_self_verified_hiring_evidence(db: Session, problem: ProblemHypothesis) -> bool:
    """True if ANY evidence linked to this ProblemHypothesis came from one of the
    SELF_VERIFIED_HIRING_EVENT_TYPES above -- real, already-verified hiring evidence, not a raw
    inference. Used only to skip the redundant validate_problem step/diagnostic strategy type for
    that evidence specifically; every other evidence-tier gate (Opportunity eligibility, Demand
    qualification, etc.) is completely untouched by this -- it only changes what the STRATEGY
    layer recommends doing next, never what counts as real evidence."""
    linked_signal_ids = [
        row[0]
        for row in db.query(ProblemHypothesisEvidence.interpreted_signal_id)
        .filter(ProblemHypothesisEvidence.problem_hypothesis_id == problem.id)
        .all()
    ]
    if not linked_signal_ids:
        return False
    event_types = {
        row[0]
        for row in db.query(InterpretedSignal.event_type).filter(InterpretedSignal.id.in_(linked_signal_ids)).all()
    }
    return bool(event_types & SELF_VERIFIED_HIRING_EVENT_TYPES)


def _demand_has_self_verified_hiring_evidence(db: Session, demand: DemandHypothesis) -> bool:
    """Same check as _problem_has_self_verified_hiring_evidence, applied to DemandHypothesis
    evidence (2026-08-25, explicit instruction: extend the same self-verified logic to Demand).
    A qualifying hiring signal (real JD content, a genuinely counted concurrent-hiring surge, or
    a real head_of_sales-type hire) is treated as sufficient for Demand too, not just Problem --
    same rationale: the qualification work already done upstream IS the verification, so asking
    to "validate_demand" again for this specific evidence would just repeat it."""
    linked_signal_ids = [
        row[0]
        for row in db.query(DemandHypothesisEvidence.interpreted_signal_id)
        .filter(DemandHypothesisEvidence.demand_hypothesis_id == demand.id)
        .all()
    ]
    if not linked_signal_ids:
        return False
    event_types = {
        row[0]
        for row in db.query(InterpretedSignal.event_type).filter(InterpretedSignal.id.in_(linked_signal_ids)).all()
    }
    return bool(event_types & SELF_VERIFIED_HIRING_EVENT_TYPES)



# Channels a message can actually be dispatched on today. "salesrobot" is the real LinkedIn
# sender (see send/channels.py); "heyreach" is configured alongside it in offering_config.
_DISPATCHABLE_CHANNELS = ("salesrobot", "heyreach")


def _offering_is_actionable(db: Session, tenant_id: int, offering_name: str) -> bool:
    """True when this offering has a campaign configured on at least one channel.

    Why this gates offering SELECTION (2026-08-31): matching and dispatchability were decided
    independently, so the system happily picked the best-fitting offering and only discovered at
    send time that there was nowhere to send it. Measured in production: 9 of 15 strategies
    matched "Consulting", which has no campaign on any channel -- 60% of qualified opportunities
    were dead on arrival by configuration, not by fit."""
    from app.gtm_os.opportunity.offering_config import get_offering_campaign_id

    return any(get_offering_campaign_id(db, tenant_id, offering_name, channel) for channel in _DISPATCHABLE_CHANNELS)


def _pick_matched_offering(db: Session, tenant_id: int, candidate_matches: list[dict]) -> tuple[str, bool]:
    """Chooses among genuinely-matching offerings, preferring one that can actually be actioned.

    Never widens what counts as a match -- every candidate here already passed the real ICP and
    offering-fit rules in icp_offering_matching.py, which are untouched. This only breaks the tie
    that `candidate_matches[0]` was resolving arbitrarily by config order.

    If NO candidate is dispatchable, the best-fitting one is still returned (with False), because
    the honest state is "this account fits an offering we cannot currently send for" -- silently
    reporting no_match would hide a real, qualified opportunity behind a configuration gap."""
    for match in candidate_matches:
        if _offering_is_actionable(db, tenant_id, match["offering"]):
            return match["offering"], True
    return candidate_matches[0]["offering"], False


def _select_strategy_type(db: Session, problem: ProblemHypothesis, demand: DemandHypothesis, opportunity: Opportunity, offering_fit_status: str) -> tuple[str, list[str]]:
    """Pure, deterministic taxonomy selection -- reads ONLY Problem/Demand tiers, affected_function,
    and offering_fit_status. Never reads market/trend context (see module docstring)."""
    missing_information = []

    if not opportunity.affected_function or opportunity.affected_function == "unknown":
        missing_information.append("affected_function is unknown -- cannot determine target persona/function for an approach")
        return "insufficient_context", missing_information

    problem_tier = (problem.confidence or {}).get("best_evidence_tier")
    if problem_tier == "implied_gap" and _problem_has_self_verified_hiring_evidence(db, problem):
        problem_tier = "declared"  # already-verified hiring evidence -- treat as confirmed, same as a declared problem statement
    demand_tier = (demand.confidence or {}).get("best_evidence_tier")
    if demand_tier == "demand" and _demand_has_self_verified_hiring_evidence(db, demand):
        demand_tier = "buying_intent_adjacent"  # already-verified hiring evidence -- treat as confirmed buying intent

    if problem_tier not in ("declared", "implied_gap") or demand_tier not in DEMAND_QUALIFYING_TIERS:
        # Defensive fallback -- should not happen given Opportunity's own eligibility gate
        # (Batch 4), but this module never assumes that gate always held true elsewhere.
        missing_information.append(f"underlying evidence tiers (problem={problem_tier!r}, demand={demand_tier!r}) do not clearly support a strategy")
        return "insufficient_context", missing_information

    if offering_fit_status in ("no_match", "excluded"):
        return "nurture", missing_information

    if problem_tier == "declared" and demand_tier == "buying_intent_adjacent":
        return "execution-led", missing_information
    if problem_tier == "declared":
        return "consultative", missing_information
    return "diagnostic", missing_information  # implied_gap problem tier


def _build_action_plan(
    db: Session,
    problem: ProblemHypothesis,
    demand: DemandHypothesis,
    opportunity: Opportunity,
    offering_fit_status: str,
    decision_maker_known: bool,
) -> list[dict]:
    """Deterministic, explainable action chain (Part J) -- stops recommending downstream actions
    the moment a real prerequisite is missing, never guesses past a gap. Every action carries
    status="recommended" -- nothing in this function or anywhere in this package executes
    anything (Part K).

    decision_maker_known is passed in (V2 Phase 6), computed ONCE by the caller
    (generate_strategy_facts), rather than re-queried here -- the caller also needs this exact
    value as its own top-level fact for staleness detection (see _COMPARABLE_KEYS), so a single
    source of truth avoids two independent DB reads of the same real answer within one facts
    computation."""
    plan: list[dict] = []
    prerequisite = None

    def add(action_type: str, objective: str, rationale: str) -> None:
        nonlocal prerequisite
        plan.append({
            "action_type": action_type,
            "objective": objective,
            "target_function": opportunity.affected_function,
            "rationale": rationale,
            "prerequisite": prerequisite,
            "status": "recommended",
        })
        prerequisite = action_type

    add("research_account", f"Understand {opportunity.company_name_raw or 'the account'}'s current situation before any outreach.", "Baseline research always precedes account-specific outreach.")

    if not decision_maker_known:
        add("identify_decision_maker", "Find the right person to engage for this opportunity.", "No known Contact exists for this account yet -- required before any message can be prepared.")
        return plan  # cannot safely recommend anything past this without a person to address

    problem_tier = (problem.confidence or {}).get("best_evidence_tier")
    self_verified = problem_tier == "implied_gap" and _problem_has_self_verified_hiring_evidence(db, problem)
    if self_verified:
        problem_tier = "declared"  # already-verified hiring evidence -- see SELF_VERIFIED_HIRING_EVENT_TYPES
    if problem_tier != "declared":
        add("validate_problem", "Confirm the problem directly with the account rather than relying on implied evidence alone.", f"ProblemHypothesis evidence tier is {problem_tier!r}, not yet explicitly declared by the account.")
        return plan  # do not recommend demand/message/meeting steps ahead of confirming the problem itself

    demand_tier = (demand.confidence or {}).get("best_evidence_tier")
    if demand_tier == "demand" and _demand_has_self_verified_hiring_evidence(db, demand):
        demand_tier = "buying_intent_adjacent"  # already-verified hiring evidence -- see SELF_VERIFIED_HIRING_EVENT_TYPES
    if demand_tier != "buying_intent_adjacent":
        add("validate_demand", "Confirm the account is genuinely evaluating solutions, not just discussing the problem.", f"DemandHypothesis evidence tier is {demand_tier!r}, weaker than buying-intent-adjacent.")
        return plan  # per Part J's own example: weak demand -> validate, don't request a meeting yet

    if offering_fit_status != "candidate_match":
        add("review_offering_fit", "Determine which offering (if any) genuinely fits before positioning anything.", f"offering_fit_status is {offering_fit_status!r} -- cannot credibly prepare a message without knowing what to offer.")
        return plan

    add("prepare_message", "Draft an account-specific message grounded in the confirmed problem, demand, and offering fit.", "All prerequisite evidence (decision maker, problem, demand, offering fit) is confirmed.")
    add("request_meeting", "Ask for time to discuss the confirmed opportunity.", "Full prerequisite chain satisfied -- ready for direct engagement.")
    return plan


def generate_strategy_facts(db: Session, tenant_id: int, opportunity: Opportunity) -> dict:
    """Pure, read-only computation of everything a GtmStrategy row needs. Never writes. Split out
    from the sweep so it's independently testable and so the sweep can diff against the previous
    result without a DB write in between."""
    problem = db.get(ProblemHypothesis, opportunity.problem_hypothesis_id)
    demand = db.get(DemandHypothesis, opportunity.demand_hypothesis_id)

    # V2 Phase 6 -- computed ONCE here (single source of truth for both the action chain below
    # and this function's own returned fact, used by _facts_changed() for staleness detection).
    # Already suppression-aware (Phase 3/4's fix to _decision_maker_known itself).
    decision_maker_known = _decision_maker_known(db, opportunity.company_id) if opportunity.company_id else False

    offering_matches = match_offerings_for_opportunity(db, tenant_id, opportunity)
    candidate_matches = [m for m in offering_matches if m["status"] == "candidate_match"]
    if candidate_matches:
        offering_fit_status = "candidate_match"
        matched_offering_name, offering_dispatchable = _pick_matched_offering(db, tenant_id, candidate_matches)
        if not offering_dispatchable:
            logger.warning(
                "gtm_strategy: opportunity_id=%s matched offering %r, which has no campaign configured on any channel "
                "-- it cannot be sent until one is set up",
                opportunity.id, matched_offering_name,
            )
    elif any(m["status"] == "excluded" for m in offering_matches):
        offering_fit_status = "excluded"
        matched_offering_name = None
    elif any(m["status"] == "no_match" for m in offering_matches):
        offering_fit_status = "no_match"
        matched_offering_name = None
    else:
        offering_fit_status = "insufficient_offering_context"
        matched_offering_name = None

    strategy_type, missing_information = _select_strategy_type(db, problem, demand, opportunity, offering_fit_status)

    if offering_fit_status == "insufficient_offering_context":
        missing_information.append("offering definitions are not yet configured -- cannot confirm which offering fits (see app/gtm_os/opportunity/offering_config.py)")

    market_context_list = market_context(db, tenant_id)

    evidence_basis = {
        "problem_tier": (problem.confidence or {}).get("best_evidence_tier"),
        "demand_tier": (demand.confidence or {}).get("best_evidence_tier"),
        "demand_independent_evidence_count": (demand.confidence or {}).get("independent_evidence_count"),
        "offering_matches": offering_matches,
        "market_context": market_context_list,
        # V2 Phase 2 -- read-only exposure of the Opportunity's own ICP snapshot (see
        # opportunity.py::Opportunity.icp_context). A soft signal for visibility only; nothing
        # in _select_strategy_type/_build_action_plan reads this -- ICP is not a strategy gate.
        "icp_context": opportunity.icp_context,
        # 2026-08-25 -- transparency flag for auditability: True when this Problem's evidence
        # already came from a JD-verified/counted hiring signal (SELF_VERIFIED_HIRING_EVENT_TYPES)
        # -- explains why an "implied_gap" problem_tier still skipped validate_problem/diagnostic.
        "problem_self_verified_via_hiring_evidence": _problem_has_self_verified_hiring_evidence(db, problem),
        "demand_self_verified_via_hiring_evidence": _demand_has_self_verified_hiring_evidence(db, demand),
    }

    constraints: list[str] = []
    if offering_fit_status == "candidate_match":
        matched = candidate_matches[0]
        for reason in matched.get("missing_information", []):
            constraints.append(reason)

    reasoning_parts = [f"strategy_type={strategy_type!r} based on problem_tier={evidence_basis['problem_tier']!r}, demand_tier={evidence_basis['demand_tier']!r}, offering_fit_status={offering_fit_status!r}."]
    if market_context_list:
        topic_summaries = ", ".join(f"{c['topic']} ({c['state']})" for c in market_context_list)
        reasoning_parts.append(f"General market context (not specific to this opportunity): {topic_summaries}.")
    reasoning_note = " ".join(reasoning_parts)

    if strategy_type == "insufficient_context":
        recommended_approach = None
        positioning_angle = None
        action_plan: list[dict] = []
    else:
        recommended_approach = {
            "diagnostic": "Run a diagnostic conversation to confirm and quantify the implied problem before positioning anything.",
            "consultative": "Engage consultatively to explore fit -- the problem is confirmed but buying intent is not yet strong.",
            "execution-led": "Move toward concrete engagement -- both the problem and buying intent are well-evidenced.",
            "nurture": "Stay on radar -- evidence supports a real problem/demand, but no configured offering currently fits.",
        }.get(strategy_type)
        positioning_angle = f"Lead with the confirmed {opportunity.affected_function} problem" + (f", positioned around {matched_offering_name}" if matched_offering_name else "") + "."
        action_plan = _build_action_plan(db, problem, demand, opportunity, offering_fit_status, decision_maker_known)

    return {
        "strategy_type": strategy_type,
        "decision_maker_known": decision_maker_known,
        "recommended_approach": recommended_approach,
        "target_function": opportunity.affected_function,
        "positioning_angle": positioning_angle,
        "offering_fit_status": offering_fit_status,
        "matched_offering_name": matched_offering_name,
        "evidence_basis": evidence_basis,
        "constraints": constraints,
        "missing_information": missing_information,
        "action_plan": action_plan,
        "recommended_next_step": action_plan[0]["action_type"] if action_plan else None,
        "reasoning_note": reasoning_note,
    }


_COMPARABLE_KEYS = ("strategy_type", "offering_fit_status", "matched_offering_name", "target_function", "missing_information", "recommended_next_step", "decision_maker_known")


def _latest_strategy_for_opportunity(db: Session, tenant_id: int, opportunity_id: int) -> GtmStrategy | None:
    return (
        db.query(GtmStrategy)
        .filter(GtmStrategy.tenant_id == tenant_id, GtmStrategy.opportunity_id == opportunity_id)
        .order_by(GtmStrategy.id.desc())
        .first()
    )


def _facts_changed(existing: GtmStrategy, facts: dict) -> bool:
    for key in _COMPARABLE_KEYS:
        if getattr(existing, key) != facts[key]:
            return True
    return False


def create_strategy(db: Session, tenant_id: int, opportunity: Opportunity, facts: dict) -> GtmStrategy:
    strategy = GtmStrategy(
        tenant_id=tenant_id,
        opportunity_id=opportunity.id,
        strategy_type=facts["strategy_type"],
        recommended_approach=facts["recommended_approach"],
        target_function=facts["target_function"],
        positioning_angle=facts["positioning_angle"],
        offering_fit_status=facts["offering_fit_status"],
        matched_offering_name=facts["matched_offering_name"],
        evidence_basis=facts["evidence_basis"],
        constraints=facts["constraints"],
        missing_information=facts["missing_information"],
        action_plan=facts["action_plan"],
        recommended_next_step=facts["recommended_next_step"],
        decision_maker_known=facts["decision_maker_known"],
        reasoning_note=facts["reasoning_note"],
    )
    db.add(strategy)
    db.commit()
    return strategy


def run_gtm_strategy_sweep(db: Session, tenant_id: int, limit: int = 200, dry_run: bool = False) -> dict:
    """Evaluates up to `limit` Opportunity rows for one tenant. Creates a new GtmStrategy row
    only when none exists yet for that opportunity, or when the computed facts genuinely differ
    from the most recent existing one (Part N) -- never duplicates on an unchanged rerun, never
    overwrites a historical row. `dry_run=True` computes everything but writes nothing.

    Zero outbound API calls, zero LLM calls, zero CRM writes anywhere in this function or
    anything it calls (Part K/M) -- confirmed by construction: generate_strategy_facts() only
    ever reads DemandHypothesis/ProblemHypothesis/ContentTopic/Contact and calls
    match_offerings_for_opportunity() (icp_offering_matching.py -- ICP-aware when Opportunity.
    icp_context has a real match, falls back to offering_matcher.py's own match_offerings()
    otherwise), all pure reads.

    One opportunity's failure never aborts the sweep -- same per-item error-isolation style as
    every other sweep in this feature."""
    counts = {
        "evaluated": 0,
        "strategy_created": 0,
        "strategy_updated": 0,
        "insufficient_context": 0,
        "skipped": 0,
        "failed": 0,
        "dry_run": dry_run,
    }

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

            facts = generate_strategy_facts(db, tenant_id, opportunity)
            if facts["strategy_type"] == "insufficient_context":
                counts["insufficient_context"] += 1

            existing = _latest_strategy_for_opportunity(db, tenant_id, opportunity.id)

            if existing is None:
                if not dry_run:
                    create_strategy(db, tenant_id, opportunity, facts)
                counts["strategy_created"] += 1
            elif _facts_changed(existing, facts):
                if not dry_run:
                    create_strategy(db, tenant_id, opportunity, facts)
                counts["strategy_updated"] += 1
            else:
                counts["skipped"] += 1

        except Exception:  # noqa: BLE001 -- one opportunity's failure must never block the others, see docstring
            counts["failed"] += 1

    return counts
