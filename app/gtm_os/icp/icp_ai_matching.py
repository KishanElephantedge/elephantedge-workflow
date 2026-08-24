"""AI-assisted, holistic ICP fit judgment for hiring-based opportunities -- 2026-08-25, explicit
instruction.

The existing deterministic matcher (icp_matching.py) requires EVERY configured check (revenue,
sales team size, hiring-role trigger) to pass exactly as an AND-gate -- confirmed too rigid in
practice: 4 real, JD/role-verified hiring opportunities (Cara/Rayvector Technologies/Codeable/
K Group Companies) collapsed to only 1 clean ICP match once that strict gate was applied, even
though a human reviewing the full context (hiring role + strength, revenue figure and how
reliable it looks, employee count, the real concurrent-hiring/JD evidence already on file) could
reasonably judge a best fit for most of them -- e.g. K Group Companies' revenue fits icp_3's band
exactly, but its hiring_signal_role is "ae" not "head_of_sales", so the strict matcher rejects it
outright with no room for "close enough, other evidence points here."

This module does NOT replace icp_matching.py's deterministic matcher, its ICPMatch persistence,
or run_icp_matching_sweep -- those stay exactly as they are for a strict, auditable, rule-based
answer. This is an ADDITIVE, separately-invoked judgment specifically for hiring-based
opportunities, explicitly requested to use holistic judgment rather than an exact hiring-role-
title match. It writes to the SAME ICPMatch table via the existing record_icp_match() (not a new
persistence mechanism) but tagged trigger_evidence.method="ai_holistic_judgment" so an AI-derived
match is always distinguishable from a strict rule-based one.

Bounded to the SAME finite icp_id vocabulary already defined in icp_config.py -- the model is
given the three real ICP definitions (description/trigger_description/revenue band) verbatim and
must pick one of those three ids or "none"; it can never invent a new ICP or a new meaning for an
existing one.

Evidence-integrity discipline, same as every other LLM pass added this session: the model must
name which SPECIFIC real fact(s) it based its judgment on (quoting the actual value, e.g.
"estimated_revenue $30,000,000" or "hiring_signal_role='ae', hiring_signal_strength='strong'").
Facts it names are verified against what was actually given in the prompt before the judgment is
trusted -- a judgment citing a fact never provided is discarded, never trusted blind."""

from sqlalchemy.orm import Session

from app.db.models import Company
from app.gtm_os.icp.icp_config import get_icp_config
from app.gtm_os.icp.icp_matching import record_icp_match
from app.gtm_os.opportunity.opportunity import Opportunity
from app.llm_client import generate_json


def _gather_company_facts(db: Session, tenant_id: int, company: Company) -> dict:
    """Every real, already-available fact about this company -- nothing fabricated, nothing
    fetched fresh (no new provider call here; this only reads what's already on file)."""
    facts = {
        "name": company.name,
        "domain": company.domain,
        "hiring_signal_role": company.hiring_signal_role,
        "hiring_signal_strength": company.hiring_signal_strength,
        "hiring_signal_reasoning": company.hiring_signal_reasoning,
        "estimated_revenue_lower_usd": company.estimated_revenue_lower_usd,
        "estimated_revenue_higher_usd": company.estimated_revenue_higher_usd,
        "employee_count": company.employee_count,
        "sales_headcount_percent": company.sales_headcount_percent,
    }
    opportunity = db.query(Opportunity).filter(Opportunity.tenant_id == tenant_id, Opportunity.company_id == company.id).first()
    if opportunity is not None:
        facts["real_opportunity_evidence"] = opportunity.opportunity_statement
    return facts


def _facts_block(facts: dict) -> str:
    lines = []
    for key, value in facts.items():
        if value is None:
            continue
        lines.append(f"- {key}: {value}")
    return "\n".join(lines) if lines else "(no real facts available for this company)"


def evaluate_icp_fit_holistic(db: Session, tenant_id: int, company: Company) -> dict:
    """Real, LLM-assisted judgment of which ICP (if any) best fits this company, using ALL real
    facts on file -- not a single rigid AND-gate on one field. Returns
    {"icp_id": "icp_1"|"icp_2"|"icp_3"|None, "reasoning": str, "facts_cited": list[str]}.
    Fails closed (icp_id=None) on any LLM error, invalid icp_id, or a judgment that cites a fact
    never actually given -- never a fabricated fit."""
    icps = get_icp_config(db, tenant_id)
    valid_icp_ids = {icp["id"] for icp in icps}
    facts = _gather_company_facts(db, tenant_id, company)
    facts_block = _facts_block(facts)

    icp_definitions = "\n".join(
        f"- {icp['id']} ({icp['name']}): {icp['description']} Trigger: {icp['trigger_description']}"
        for icp in icps
    )

    prompt = f"""You are judging which Ideal Customer Profile (ICP), if any, best fits a real company,
for a B2B sales-intelligence system. Use holistic judgment across ALL the facts given -- do not
require an exact match on any single field (e.g. the hiring role title doesn't have to be a
perfect keyword match if other real evidence -- revenue, team size, the real opportunity evidence
-- clearly points to one ICP being the best fit).

The three real ICP definitions (pick exactly one id, or "none" if genuinely no reasonable fit):
{icp_definitions}

Real facts on file for this company:
{facts_block}

If a revenue figure looks unreliable (e.g. an extremely wide range spanning $100M+, which usually
means a generic industry-wide answer rather than this specific company's real revenue), say so
explicitly and weigh it less, rather than treating it as precise.

Return JSON exactly:
{{"icp_id": "icp_1" | "icp_2" | "icp_3" | "none", "reasoning": "<1-2 sentences, must explicitly reference at least one specific fact listed above by name and value>", "facts_cited": ["<fact key>", ...]}}"""
    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=350)
    except Exception:
        return {"icp_id": None, "reasoning": "LLM unavailable", "facts_cited": []}

    icp_id = response.get("icp_id")
    if icp_id == "none":
        return {"icp_id": None, "reasoning": response.get("reasoning") or "", "facts_cited": []}
    if icp_id not in valid_icp_ids:
        return {"icp_id": None, "reasoning": "invalid icp_id returned", "facts_cited": []}

    facts_cited = response.get("facts_cited") or []
    if not facts_cited or not all(fc in facts for fc in facts_cited):
        return {"icp_id": None, "reasoning": "judgment cited a fact never actually provided -- discarded", "facts_cited": []}

    return {"icp_id": icp_id, "reasoning": response.get("reasoning") or "", "facts_cited": facts_cited}


def evaluate_and_record_icp_fit_holistic(db: Session, tenant_id: int, company: Company) -> dict:
    """Runs evaluate_icp_fit_holistic() and, if it found a real fit, records it via the EXISTING
    record_icp_match() (same ICPMatch table/persistence icp_matching.py already uses) -- tagged
    trigger_evidence.method="ai_holistic_judgment" so it is always distinguishable from a strict
    rule-based match. Returns the judgment dict plus the resulting ICPMatch id, or None if no fit
    was found."""
    judgment = evaluate_icp_fit_holistic(db, tenant_id, company)
    if judgment["icp_id"] is None:
        return {**judgment, "icp_match_id": None}

    icp_names = {icp["id"]: icp["name"] for icp in get_icp_config(db, tenant_id)}
    facts = _gather_company_facts(db, tenant_id, company)
    match = record_icp_match(db, tenant_id, company.id, {
        "icp_id": judgment["icp_id"],
        "reasons": [f"AI holistic judgment ({icp_names.get(judgment['icp_id'], judgment['icp_id'])}): {judgment['reasoning']}"],
        "trigger_evidence": {
            "method": "ai_holistic_judgment",
            "facts_cited": judgment["facts_cited"],
            "facts_at_evaluation_time": {k: facts[k] for k in judgment["facts_cited"] if k in facts},
        },
    })
    return {**judgment, "icp_match_id": match.id}
