"""Account Brief -- Batch 12. Deterministic account-level aggregation, no LLM, no new facts.
See app/gtm_os/account_agent/__init__.py for the scope boundary.

WHAT THIS IS: build_account_brief(db, tenant_id, company_id) assembles ONE coherent read of
everything already known about an account by calling into each existing layer's own
already-built, already-tested read function/query -- never re-deriving a decision that layer
already owns:
    ICPMatch (Batch 8, queried directly, same as market_account_context.py's
        get_account_evidence_summary) -- ICP status
    match_offerings_for_company()   (Batch 9, unmodified)         -- offering fit
    recommend_gtm_motion()          (Batch 10, unmodified)        -- GTM motion
    get_market_context_for_company()(Batch 11, unmodified)        -- market context
    ProblemHypothesis / DemandHypothesis (Batch 4, queried directly, confidence reused as-is)
    Opportunity                     (Batch 4, queried directly)
    GtmStrategy                     (Batch 5, latest row per opportunity, unmodified)
    evaluate_sales_readiness()      (Batch 6, unmodified)
    evaluate_decision_maker()       (Batch 6, unmodified)

WHAT THIS IS NOT: it never recomputes ICP eligibility, offering fit, motion selection, market
relevance, problem/demand tiers, strategy type, or sales readiness -- every one of those stays
owned by its existing module. The only new logic in this file is:
    1. account_status  -- a single deterministic ladder over the above (see
       `_derive_account_status`), because no existing layer already expresses "how far along is
       this account overall" as one value.
    2. next_investigation for accounts with NO Opportunity/GtmStrategy yet -- a minimum,
       explicitly-labeled gap ladder (see `_derive_minimum_next_investigation`), used ONLY
       because Part 8 explicitly requires *some* answer even before a Strategy exists to reuse.
       The moment a GtmStrategy exists, its own action_plan/readiness next_action is used
       instead, verbatim -- this ladder never runs against the vocabulary strategy.py already
       owns (ACTION_CHAIN_ORDER's action_types are reused by name, not reinvented).

PERSISTENCE DECISION: no new table. build_account_brief() is pure and computed on demand --
every input is a cheap, already-indexed query, same "prefer pure computation" precedent as
market_account_context.py, gtm_motion.py, icp_offering_matching.py, trend_intelligence.py, and
sales_agent.py. Nothing here needs history -- a brief is always "as of right now," and every
underlying fact it reads (ICPMatch, GtmStrategy, Opportunity...) already has its own persistence
and its own history where that matters. STOP-and-explain per Part 16: no concrete requirement for
durable Account-Agent-specific state was found -- everything the brief needs already exists
somewhere else in already-persisted form."""

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Batch, Company, Contact
from app.gtm_os.content.topic import ContentTopicEvidence
from app.gtm_os.gtm_motion.gtm_motion import recommend_gtm_motion
from app.gtm_os.icp.icp_candidates import get_icp_candidates_for_company
from app.gtm_os.icp.icp_config import get_icp_config
from app.gtm_os.icp.icp_matching import ICPMatch
from app.gtm_os.icp.icp_offering_matching import match_offerings_for_company
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.market_account.market_account_context import get_market_context_for_company
from app.gtm_os.opportunity.offering_recommendation import get_offering_recommendation_for_company
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.sales.sales_agent import evaluate_decision_maker, evaluate_sales_readiness
from app.gtm_os.strategy.strategy import GtmStrategy

# Ordered weakest -> strongest. Each state's requirement is a fact directly readable from an
# existing layer's own output -- never invented (Part 6's "only use states that can actually be
# derived from existing data"). account_status is the HIGHEST state reached across every
# Opportunity this account has, since an account can have multiple Opportunities at different
# stages and reporting only the weakest would understate real progress already made.
ACCOUNT_STATES_ORDER = [
    "insufficient_context",   # no company row, or company row with zero supporting evidence anywhere
    "identified",             # company row exists, nothing further yet
    "icp_matched",            # >=1 ICPMatch row
    "opportunity_identified", # >=1 Opportunity row (Batch 4's own eligibility gate already passed)
    "strategy_ready",         # >=1 Opportunity has a GtmStrategy with strategy_type != insufficient_context
    "sales_ready",            # >=1 Opportunity's evaluate_sales_readiness() status == ready_for_message
]


def _latest_strategy(db: Session, tenant_id: int, opportunity_id: int) -> GtmStrategy | None:
    return (
        db.query(GtmStrategy)
        .filter(GtmStrategy.tenant_id == tenant_id, GtmStrategy.opportunity_id == opportunity_id)
        .order_by(GtmStrategy.id.desc())
        .first()
    )


def _company_dict(company: Company) -> dict:
    fields = (
        "id", "name", "domain", "industry", "employee_count", "location", "linkedin_url", "source",
        "estimated_revenue_lower_usd", "estimated_revenue_higher_usd", "last_funding_round_type",
        "last_funding_date", "headcount_growth_12m_percent", "hiring_signal_role",
        "hiring_signal_hire_type", "hiring_signal_strength", "hiring_signal_reasoning",
        "hiring_signal_posting_count", "active_job_title", "detected_tech_stack",
        "has_outbound_tooling", "has_ai_sdr_tool", "hot_lead", "hot_lead_reasoning",
    )
    return {field: getattr(company, field, None) for field in fields}


def _icp_section(db: Session, tenant_id: int, company_id: int) -> dict:
    """Reuses persisted ICPMatch rows directly (Batch 8) -- never re-runs
    evaluate_icp_matches_for_company(), same discipline icp_offering_matching.py already
    established for reading ICP state."""
    matches = (
        db.query(ICPMatch)
        .filter(ICPMatch.tenant_id == tenant_id, ICPMatch.company_id == company_id)
        .order_by(ICPMatch.icp_id)
        .all()
    )
    icp_config_by_id = {icp["id"]: icp["name"] for icp in get_icp_config(db, tenant_id)}
    return {
        "status": "matched" if matches else "unmatched",
        "matches": [
            {
                "icp_id": m.icp_id,
                "icp_name": icp_config_by_id.get(m.icp_id, m.icp_id),
                "reasons": m.reasons,
                "trigger_evidence": m.trigger_evidence,
                "evaluated_at": m.evaluated_at,
            }
            for m in matches
        ],
    }


def _derive_minimum_next_investigation(
    icp_section: dict,
    offering_result: dict,
    has_problem: bool,
    has_demand: bool,
    decision_maker: dict,
) -> tuple[str | None, str]:
    """ONLY reached when the account has no Opportunity yet (so no GtmStrategy/action_plan to
    reuse). A minimum, explicit gap ladder over facts each already-existing layer already
    surfaces -- never invents a state, never guesses. Reuses ACTION_CHAIN_ORDER's own
    "identify_decision_maker" label where the same real gap applies (no known Contact)."""
    if decision_maker["status"] != "known":
        return "identify_decision_maker", "no known Contact exists for this account yet"
    if icp_section["status"] != "matched":
        return "run_icp_matching", "no ICPMatch exists for this account yet -- run the Batch 8 ICP matching sweep"
    has_candidate_offering = any(
        any(o["status"] == "candidate_match" for o in icp_entry["offerings"])
        for icp_entry in offering_result["icp_matches"]
    )
    if not has_candidate_offering:
        return "review_offering_fit", "ICP matched, but no offering has a candidate_match yet"
    if not has_problem:
        return "gather_problem_evidence", "no ProblemHypothesis exists yet for this account"
    if not has_demand:
        return "validate_demand", "a ProblemHypothesis exists, but no DemandHypothesis has been established yet"
    return "await_additional_evidence", "Problem and Demand evidence both exist but have not yet reached Opportunity eligibility (Batch 4's own gate) -- no further deterministic action to recommend until more independent evidence accumulates"


def build_account_brief(db: Session, tenant_id: int, company_id: int) -> dict:
    """Pure, read-only. Aggregates every existing account-relevant layer for one company into a
    single coherent view. Makes zero writes, zero LLM calls, zero external API calls."""
    missing_information: list[str] = []

    company = db.get(Company, company_id)
    if company is None:
        return {
            "company_id": company_id,
            "account_status": "insufficient_context",
            "company": None,
            "missing_information": [f"company_id {company_id} does not resolve to a real Company row"],
            "account_summary": {
                "account_status": "insufficient_context",
                "strongest_evidence": [],
                "current_blocker": "company does not exist",
                "next_investigation": None,
                "readiness": None,
                "confidence": None,
            },
        }

    contacts = db.query(Contact).filter(Contact.company_id == company_id).all()
    decision_maker = evaluate_decision_maker(db, tenant_id, company_id)

    icp_section = _icp_section(db, tenant_id, company_id)
    if icp_section["status"] != "matched":
        missing_information.append("no ICPMatch exists for this account -- run the Batch 8 ICP matching sweep")

    offering_result = match_offerings_for_company(db, tenant_id, company_id)
    has_candidate_offering = any(
        any(o["status"] == "candidate_match" for o in icp_entry["offerings"])
        for icp_entry in offering_result["icp_matches"]
    )
    offering_section = {
        "matches": offering_result["icp_matches"],
        "unconfigured_offerings": offering_result["unconfigured_offerings"],
        "best_fit": offering_result["best_fit"],
        "best_fit_reason": offering_result["best_fit_reason"],
        "status": "candidate_match" if has_candidate_offering else ("insufficient_context" if icp_section["status"] != "matched" else "no_match"),
    }
    if offering_section["status"] != "candidate_match":
        missing_information.append(f"offering match status is {offering_section['status']!r}")

    motion_result = recommend_gtm_motion(db, tenant_id, company_id)
    if motion_result["status"] != "recommended":
        missing_information.extend(motion_result["missing_information"])

    problems = (
        db.query(ProblemHypothesis)
        .filter(ProblemHypothesis.tenant_id == tenant_id, ProblemHypothesis.company_id == company_id)
        .order_by(ProblemHypothesis.id)
        .all()
    )
    demands = (
        db.query(DemandHypothesis)
        .filter(DemandHypothesis.tenant_id == tenant_id, DemandHypothesis.company_id == company_id)
        .order_by(DemandHypothesis.id)
        .all()
    )
    problem_section = [
        {
            "type": "problem",
            "source": "ProblemHypothesis",
            "id": p.id,
            "affected_function": p.affected_function,
            "problem_statement": p.problem_statement,
            "confidence": p.confidence,
        }
        for p in problems
    ]
    demand_section = [
        {
            "type": "demand",
            "source": "DemandHypothesis",
            "id": d.id,
            "affected_function": d.affected_function,
            "demand_statement": d.demand_statement,
            "confidence": d.confidence,
        }
        for d in demands
    ]
    if not problems:
        missing_information.append("no ProblemHypothesis evidence exists yet for this account")
    if not demands:
        missing_information.append("no DemandHypothesis evidence exists yet for this account")

    opportunities = (
        db.query(Opportunity)
        .filter(Opportunity.tenant_id == tenant_id, Opportunity.company_id == company_id)
        .order_by(Opportunity.id)
        .all()
    )

    opportunity_section = []
    best_readiness = None
    best_strategy_type = None
    for opp in opportunities:
        strategy = _latest_strategy(db, tenant_id, opp.id)
        readiness = evaluate_sales_readiness(strategy) if strategy is not None else None
        opportunity_section.append({
            "type": "opportunity",
            "source": "Opportunity",
            "id": opp.id,
            "status": opp.status,
            "affected_function": opp.affected_function,
            "opportunity_statement": opp.opportunity_statement,
            "confidence": opp.confidence,
            "strategy": None if strategy is None else {
                "id": strategy.id,
                "strategy_type": strategy.strategy_type,
                "offering_fit_status": strategy.offering_fit_status,
                "action_plan": strategy.action_plan,
                "evidence_basis": strategy.evidence_basis,
                "reasoning_note": strategy.reasoning_note,
                "recommended_next_step": strategy.recommended_next_step,
            },
            "sales_readiness": readiness,
        })
        if strategy is not None and strategy.strategy_type != "insufficient_context":
            if best_strategy_type is None:
                best_strategy_type = strategy.strategy_type
        if readiness is not None:
            if readiness["status"] == "ready_for_message":
                best_readiness = readiness
            elif best_readiness is None:
                best_readiness = readiness

    if opportunities and all(o["strategy"] is None for o in opportunity_section):
        missing_information.append("Opportunity exists, but no GtmStrategy has been generated yet -- run the Batch 5 strategy sweep")

    market_context_result = get_market_context_for_company(db, tenant_id, company_id)
    missing_information.extend(market_context_result["missing_information"])

    # --- account_status: highest state reached across all evidence, per module docstring ---
    account_status = "identified"
    if icp_section["status"] == "matched":
        account_status = "icp_matched"
    if opportunities:
        account_status = "opportunity_identified"
    if best_strategy_type is not None:
        account_status = "strategy_ready"
    if best_readiness is not None and best_readiness["status"] == "ready_for_message":
        account_status = "sales_ready"
    if (
        icp_section["status"] != "matched"
        and not opportunities
        and not contacts
        and not problems
        and not demands
        and not market_context_result["market_context"]
    ):
        account_status = "insufficient_context"

    # --- next_investigation: reuse strategy's own chain if any opportunity has one; otherwise
    # use the minimum gap ladder (see _derive_minimum_next_investigation docstring) ---
    next_investigation = None
    current_blocker = None
    if best_readiness is not None:
        if best_readiness["status"] == "ready_for_message":
            next_investigation = "prepare_message"
            current_blocker = None
        else:
            next_investigation = best_readiness["next_action"]
            current_blocker = best_readiness["blocking_prerequisite"] or best_readiness["reason"]
    elif not opportunities:
        next_investigation, current_blocker = _derive_minimum_next_investigation(
            icp_section, offering_result, bool(problems), bool(demands), decision_maker,
        )

    strongest_evidence = []
    if icp_section["matches"]:
        strongest_evidence.append(f"ICP match: {icp_section['matches'][0]['icp_name']}")
    if offering_section["best_fit"]:
        strongest_evidence.append(f"offering best fit: {offering_section['best_fit']['offering']}")
    if demands:
        strongest_evidence.append(f"demand evidence: {demands[-1].demand_statement}")
    elif problems:
        strongest_evidence.append(f"problem evidence: {problems[-1].problem_statement}")
    if market_context_result["market_context"]:
        strongest_evidence.append(f"market context: {market_context_result['market_context'][0]['topic_name']} ({market_context_result['market_context'][0]['trend_state']})")

    account_summary = {
        "account_status": account_status,
        "strongest_evidence": strongest_evidence,
        "current_blocker": current_blocker,
        "next_investigation": next_investigation,
        "readiness": best_readiness["status"] if best_readiness is not None else None,
        "confidence": opportunities[-1].confidence if opportunities else None,
    }

    return {
        "company_id": company_id,
        "account_status": account_status,
        "company": _company_dict(company),
        "contacts": [
            {"id": c.id, "first_name": c.first_name, "last_name": c.last_name, "title": c.title, "linkedin_url": c.linkedin_url, "has_email": c.email is not None}
            for c in contacts
        ],
        "decision_maker": decision_maker,
        "icp": icp_section,
        "triggers": [m["trigger_evidence"] for m in icp_section["matches"]],
        # Cross-ICP / problem-following offering recommendation (architecture upgrade, Parts 3-4)
        # -- additive, does not replace `icp`/`offerings` above (kept for backward compatibility),
        # just adds the primary-vs-alternative-vs-insufficient-evidence distinction and the
        # problem-aligned offering explanation neither of those fields express on their own.
        "icp_candidates": get_icp_candidates_for_company(db, tenant_id, company_id),
        "offering_recommendation": get_offering_recommendation_for_company(db, tenant_id, company_id),
        "offerings": offering_section,
        "gtm_motion": motion_result,
        "problems": problem_section,
        "demand": demand_section,
        "opportunities": opportunity_section,
        "market_context": market_context_result["market_context"],
        "missing_information": missing_information,
        "account_summary": account_summary,
    }


def run_account_agent_sweep(db: Session, tenant_id: int, limit: int = 200) -> dict:
    """Bounded, standalone, tenant-scoped, read-only reporting sweep -- writes NOTHING (pure
    computation, see module docstring's persistence decision). One company's failure never
    aborts the sweep. NOT wired into the scheduler (per scope boundary). Counts mirror
    ACCOUNT_STATES_ORDER exactly, since that's the only account-status vocabulary this batch
    actually defines."""
    counts = {state: 0 for state in ACCOUNT_STATES_ORDER}
    counts["evaluated"] = 0
    counts["failed"] = 0

    company_ids = [
        row[0]
        for row in db.query(Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id)
        .order_by(Company.id)
        .limit(limit)
        .all()
    ]

    for company_id in company_ids:
        try:
            counts["evaluated"] += 1
            brief = build_account_brief(db, tenant_id, company_id)
            counts[brief["account_status"]] += 1
        except Exception:  # noqa: BLE001 -- one company's failure must never block the others
            counts["failed"] += 1

    return counts


def summarize_account_states(db: Session, tenant_id: int, limit: int = 5000) -> dict:
    """Phase 3 (V2 Accounts Summary API) -- the SAME account_status ladder build_account_brief()
    computes per company (see ACCOUNT_STATES_ORDER above), but derived from a small, FIXED number
    of bulk queries instead of one build_account_brief() call per company. This function exists
    ONLY because an HTTP endpoint calling build_account_brief() once per company on every Accounts
    page load would mean N (company count) x ~10 queries per request -- run_account_agent_sweep()
    above already accepts that cost for an offline/manual sweep, but a page-load-triggered API
    endpoint should not.

    EXACT SAME SEMANTICS, DIFFERENT SHAPE OF QUERY -- never a second ladder:
        - reuses ACCOUNT_STATES_ORDER verbatim (no new state is invented)
        - reuses evaluate_sales_readiness() verbatim, called on already-fetched GtmStrategy
          objects (zero extra queries -- it's a pure function over fields already on the row)
        - the "insufficient_context" / "identified" / "icp_matched" / "opportunity_identified" /
          "strategy_ready" / "sales_ready" boundary conditions are copied line-for-line from
          build_account_brief()'s own ladder below -- if that ladder ever changes, this one must
          be updated to match, since this function's whole purpose is to agree with it exactly.

    Six fixed-count queries total (never one per company):
        1. company ids for the tenant
        2-6. one EXISTS-shaped query each for: ICPMatch, Contact, ProblemHypothesis,
             DemandHypothesis, and the market-account bridge (GtmSignal x
             ContentTopicEvidence) -- returning the SET of company_ids with at least one row,
             not per-company detail (existence is all account_status needs from these layers)
        7. all Opportunity rows for the tenant, grouped by company_id in Python
        8. the LATEST GtmStrategy per opportunity_id, in ONE query via Postgres's
           `DISTINCT ON (opportunity_id) ... ORDER BY opportunity_id, id DESC` -- the same
           "latest row per opportunity" concept _latest_strategy() above computes one row at a
           time, expressed here as a single set-based query instead of N lookups.
    """
    company_ids = [
        row[0]
        for row in db.query(Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id)
        .order_by(Company.id)
        .limit(limit)
        .all()
    ]
    if not company_ids:
        counts = {state: 0 for state in ACCOUNT_STATES_ORDER}
        return {"total_accounts": 0, "account_states": counts}

    def _company_ids_with(model, id_field=None) -> set[int]:
        col = id_field if id_field is not None else model.company_id
        return {row[0] for row in db.query(col).filter(model.tenant_id == tenant_id, col.in_(company_ids)).distinct().all()}

    icp_matched_ids = _company_ids_with(ICPMatch)
    problem_ids = _company_ids_with(ProblemHypothesis)
    demand_ids = _company_ids_with(DemandHypothesis)

    contact_ids = {
        row[0]
        for row in db.query(Contact.company_id)
        .join(Company, Contact.company_id == Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id, Contact.company_id.in_(company_ids))
        .distinct()
        .all()
    }

    # Market-account bridge (Batch 11's own Case B) -- existence only, no trend evaluation
    # needed for this boolean check.
    market_context_ids = {
        row[0]
        for row in db.query(GtmSignal.company_id)
        .join(ContentTopicEvidence, ContentTopicEvidence.gtm_signal_id == GtmSignal.id)
        .filter(
            GtmSignal.tenant_id == tenant_id,
            ContentTopicEvidence.tenant_id == tenant_id,
            GtmSignal.company_id.in_(company_ids),
        )
        .distinct()
        .all()
    }

    opportunities = (
        db.query(Opportunity)
        .filter(Opportunity.tenant_id == tenant_id, Opportunity.company_id.in_(company_ids))
        .all()
    )
    opportunity_ids_by_company: dict[int, list[int]] = {}
    for o in opportunities:
        opportunity_ids_by_company.setdefault(o.company_id, []).append(o.id)
    all_opportunity_ids = [o.id for o in opportunities]

    latest_strategy_by_opportunity: dict[int, GtmStrategy] = {}
    if all_opportunity_ids:
        latest_strategies = (
            db.query(GtmStrategy)
            .filter(GtmStrategy.tenant_id == tenant_id, GtmStrategy.opportunity_id.in_(all_opportunity_ids))
            .distinct(GtmStrategy.opportunity_id)
            .order_by(GtmStrategy.opportunity_id, GtmStrategy.id.desc())
            .all()
        )
        latest_strategy_by_opportunity = {s.opportunity_id: s for s in latest_strategies}

    counts = {state: 0 for state in ACCOUNT_STATES_ORDER}

    for company_id in company_ids:
        has_icp_match = company_id in icp_matched_ids
        opp_ids = opportunity_ids_by_company.get(company_id, [])
        has_opportunity = bool(opp_ids)

        best_strategy_type = None
        has_sales_ready = False
        for opp_id in opp_ids:
            strategy = latest_strategy_by_opportunity.get(opp_id)
            if strategy is None:
                continue
            if strategy.strategy_type != "insufficient_context" and best_strategy_type is None:
                best_strategy_type = strategy.strategy_type
            if evaluate_sales_readiness(strategy)["status"] == "ready_for_message":
                has_sales_ready = True

        # Ladder copied line-for-line from build_account_brief()'s own account_status logic --
        # see this function's docstring.
        account_status = "identified"
        if has_icp_match:
            account_status = "icp_matched"
        if has_opportunity:
            account_status = "opportunity_identified"
        if best_strategy_type is not None:
            account_status = "strategy_ready"
        if has_sales_ready:
            account_status = "sales_ready"
        if (
            not has_icp_match
            and not has_opportunity
            and company_id not in contact_ids
            and company_id not in problem_ids
            and company_id not in demand_ids
            and company_id not in market_context_ids
        ):
            account_status = "insufficient_context"

        counts[account_status] += 1

    return {"total_accounts": len(company_ids), "account_states": counts}


def list_account_states(db: Session, tenant_id: int, company_ids: list[int]) -> dict[int, dict]:
    """V2 Accounts list enrichment -- the SAME account_status ladder and bulk-query shape as
    summarize_account_states() above, scoped to an explicit, caller-supplied set of company_ids
    (a single list page's ~25 rows, not the whole tenant) instead of every company. Exists
    because the Accounts list previously showed zero GTM-OS state per row -- ICP match,
    offering fit, and GTM motion are real, already-computed concepts, but computing them via
    build_account_brief() for every row of a page would mean 25x the backend work per list
    view (see Accounts.jsx's own comment on this exact tradeoff). This reuses
    summarize_account_states()'s exact bulk-query pattern instead of build_account_brief(), so
    the added cost is six fixed queries scoped to ~25 ids, not 25 separate ~10-query briefs.

    Adds two counts summarize_account_states() computes internally but discards:
    opportunity_count (free -- already grouped by company in that function, just never
    returned) and signal_count (one query changed from a distinct-existence set to a
    GROUP BY count, same cost).

    Returns {company_id: {"account_status": str, "signal_count": int, "opportunity_count": int}}
    for every id in company_ids -- callers merge this into their own per-company response dicts
    rather than this function assuming any particular response shape."""
    if not company_ids:
        return {}

    def _company_ids_with(model, id_field=None) -> set[int]:
        col = id_field if id_field is not None else model.company_id
        return {row[0] for row in db.query(col).filter(model.tenant_id == tenant_id, col.in_(company_ids)).distinct().all()}

    icp_matched_ids = _company_ids_with(ICPMatch)

    contact_ids = {
        row[0]
        for row in db.query(Contact.company_id)
        .join(Company, Contact.company_id == Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id, Contact.company_id.in_(company_ids))
        .distinct()
        .all()
    }
    problem_ids = _company_ids_with(ProblemHypothesis)
    demand_ids = _company_ids_with(DemandHypothesis)

    # Same market-account bridge as summarize_account_states(), but grouped for a real count
    # instead of a plain existence set -- same query cost, one more aggregate column.
    signal_count_by_company: dict[int, int] = {
        row[0]: row[1]
        for row in db.query(GtmSignal.company_id, func.count(GtmSignal.id))
        .join(ContentTopicEvidence, ContentTopicEvidence.gtm_signal_id == GtmSignal.id)
        .filter(
            GtmSignal.tenant_id == tenant_id,
            ContentTopicEvidence.tenant_id == tenant_id,
            GtmSignal.company_id.in_(company_ids),
        )
        .group_by(GtmSignal.company_id)
        .all()
    }
    market_context_ids = set(signal_count_by_company.keys())

    opportunities = (
        db.query(Opportunity)
        .filter(Opportunity.tenant_id == tenant_id, Opportunity.company_id.in_(company_ids))
        .all()
    )
    opportunity_ids_by_company: dict[int, list[int]] = {}
    for o in opportunities:
        opportunity_ids_by_company.setdefault(o.company_id, []).append(o.id)
    all_opportunity_ids = [o.id for o in opportunities]

    latest_strategy_by_opportunity: dict[int, GtmStrategy] = {}
    if all_opportunity_ids:
        latest_strategies = (
            db.query(GtmStrategy)
            .filter(GtmStrategy.tenant_id == tenant_id, GtmStrategy.opportunity_id.in_(all_opportunity_ids))
            .distinct(GtmStrategy.opportunity_id)
            .order_by(GtmStrategy.opportunity_id, GtmStrategy.id.desc())
            .all()
        )
        latest_strategy_by_opportunity = {s.opportunity_id: s for s in latest_strategies}

    result: dict[int, dict] = {}
    for company_id in company_ids:
        has_icp_match = company_id in icp_matched_ids
        opp_ids = opportunity_ids_by_company.get(company_id, [])
        has_opportunity = bool(opp_ids)

        best_strategy_type = None
        has_sales_ready = False
        for opp_id in opp_ids:
            strategy = latest_strategy_by_opportunity.get(opp_id)
            if strategy is None:
                continue
            if strategy.strategy_type != "insufficient_context" and best_strategy_type is None:
                best_strategy_type = strategy.strategy_type
            if evaluate_sales_readiness(strategy)["status"] == "ready_for_message":
                has_sales_ready = True

        # Same ladder as summarize_account_states() / build_account_brief() -- see those
        # functions' own docstrings for why this must stay copied line-for-line, not re-derived.
        account_status = "identified"
        if has_icp_match:
            account_status = "icp_matched"
        if has_opportunity:
            account_status = "opportunity_identified"
        if best_strategy_type is not None:
            account_status = "strategy_ready"
        if has_sales_ready:
            account_status = "sales_ready"
        if (
            not has_icp_match
            and not has_opportunity
            and company_id not in contact_ids
            and company_id not in problem_ids
            and company_id not in demand_ids
            and company_id not in market_context_ids
        ):
            account_status = "insufficient_context"

        result[company_id] = {
            "account_status": account_status,
            "signal_count": signal_count_by_company.get(company_id, 0),
            "opportunity_count": len(opp_ids),
        }

    return result
