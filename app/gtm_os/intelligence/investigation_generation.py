"""Runtime investigation generation -- Autonomous Sensing Phase S4.

Turns (InvestigationObjective, SensingStrategy) into the actual runtime action the selected
source's REAL adapter can consume -- nothing more. This module generates; it never executes.
No provider call (LinkedIn/Google/Deepline), no GtmSignal, no interpretation, no Problem/Demand/
Opportunity/Strategy write happens anywhere in this file.

GENERATED QUERY TEXT IS NOT BUSINESS CONFIGURATION. Nothing here is persisted onto
InvestigationObjective -- the durable memory stays exactly what S1 already defined (objective +
attempts + source + result). The caller may log/return the generated action for the current run
only; this module deliberately provides no second permanent query-history table.

LLM USE IS NARROW AND ALREADY-EXISTING: reuses generate_json() (app/llm_client.py, the same
Gemini-first/Claude-fallback helper reverse_discovery.py already uses for its own Google-result
classification) only for the two sources that genuinely require natural-language formulation
(linkedin_post_search, web_search). Every other source's action is built directly from real,
already-existing adapter parameters -- no LLM call, per the explicit "if the selected source
does not require language generation, do not call the LLM" instruction.

THE LLM NEVER JUDGES EVIDENCE. It is given the objective's business context and asked only to
produce up to MAX_FORMULATIONS natural-language search formulations -- it never sees raw signals,
never classifies anything, never touches evidence tiers, ICP matching, or Opportunity logic."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Company
from app.gtm_os.icp.icp_config import get_icp_config
from app.gtm_os.intelligence.investigation_memory import InvestigationObjective, is_eligible_for_attempt
from app.gtm_os.opportunity.offering_config import get_offering_config
from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run
from app.llm_client import generate_json

MAX_FORMULATIONS = 3  # a runtime safety bound, not a business rule -- see module docstring

# V2-owned employee-headcount band for linkedin_job sensing (2026-08-24, explicit instruction).
# Deliberately separate from V1's APIFY_EMPLOYEE_MIN/MAX (app/phases/apify_discovery.py, 25-50)
# -- that band is intentional for V1's own fractional-leadership motion and must stay untouched.
# V2 serves multiple offerings and can also fit somewhat larger companies.
V2_LINKEDIN_JOB_EMPLOYEE_MIN = 25
V2_LINKEDIN_JOB_EMPLOYEE_MAX = 100

STATUS_OK = "ok"
STATUS_GENERATION_FAILED = "generation_failed"
STATUS_BLOCKED = "blocked"

# Sources whose action requires natural-language formulation -- everything else is built
# directly from real, already-existing adapter parameters, no LLM involved.
LLM_DRIVEN_SOURCES = {"linkedin_post_search", "web_search"}

LINKEDIN_PROMPT_TEMPLATE = """You are helping formulate search phrases for an autonomous B2B sales-intelligence system.

Business context (for your understanding only -- do not repeat these labels back verbatim):
- Segment description: {icp_description}
- What indicates this segment: {icp_trigger_description}

Current investigation:
- Goal: {claim}
- Why this is being investigated: {reason}
- What kind of evidence would help: {expected_evidence}

Generate up to {max_formulations} short, natural, first-person LinkedIn post phrasings that a
REAL PROSPECT might plausibly write, expressing the kind of problem/question/situation described
above -- the way an actual person would post it, in plain conversational English.

Do NOT include any internal segment names, codes, or labels (e.g. "ICP1", "icp_2", segment/tier
numbers) -- only genuine, natural human language a real person on LinkedIn would write.

Return JSON exactly: {{"formulations": ["...", "...", "..."]}}"""

WEB_SEARCH_PROMPT_TEMPLATE = """You are helping formulate web-search queries for an autonomous B2B sales-intelligence system.

Business context (for your understanding only -- do not repeat these labels back verbatim):
- Segment description: {icp_description}
- What indicates this segment: {icp_trigger_description}

Current investigation:
- Goal: {claim}
- Why this is being investigated: {reason}
- What kind of evidence would help: {expected_evidence}

Generate up to {max_formulations} short web-search queries (the way someone would type into
Google) that could surface public evidence relevant to the goal above.

Do NOT include any internal segment names, codes, or labels (e.g. "ICP1", "icp_2", segment/tier
numbers) -- only genuine search-engine query language.

Return JSON exactly: {{"formulations": ["...", "...", "..."]}}"""


def _result(objective_id: int, source: str | None, action_type: str | None, parameters: dict | None, status: str, failure_reason: str | None = None) -> dict:
    return {
        "objective_id": objective_id,
        "source": source,
        "action_type": action_type,
        "parameters": parameters,
        "generated_at": datetime.utcnow().isoformat(),
        "status": status,
        "failure_reason": failure_reason,
    }


def _forbidden_tokens(db: Session, tenant_id: int) -> set[str]:
    """Real internal vocabulary the generated text must never contain verbatim -- sourced from
    the actual live config, not a guessed/hardcoded list."""
    tokens = {"icp", "icp1", "icp2", "icp3"}
    for icp in get_icp_config(db, tenant_id):
        tokens.add(icp["id"].lower())
        tokens.add(icp["id"].replace("_", "").lower())
    for offering in get_offering_config(db, tenant_id):
        name = (offering.get("name") or "").strip().lower()
        if name:
            tokens.add(name)
    return tokens


def _sanitize_formulations(formulations: list, forbidden_tokens: set[str]) -> list[str]:
    cleaned = []
    for f in formulations:
        if not isinstance(f, str) or not f.strip():
            continue
        lowered = f.lower()
        if any(token in lowered for token in forbidden_tokens):
            continue
        cleaned.append(f.strip())
        if len(cleaned) >= MAX_FORMULATIONS:
            break
    return cleaned


def _generate_formulations(db: Session, tenant_id: int, objective: InvestigationObjective, expected_evidence: str, prompt_template: str) -> list[str]:
    icp_entry = next((icp for icp in get_icp_config(db, tenant_id) if icp["id"] == objective.icp_id), None)
    prompt = prompt_template.format(
        icp_description=(icp_entry or {}).get("description", "a B2B company matching this segment"),
        icp_trigger_description=(icp_entry or {}).get("trigger_description", "not further specified"),
        claim=objective.claim,
        reason=objective.reason,
        expected_evidence=expected_evidence,
        max_formulations=MAX_FORMULATIONS,
    )
    response = generate_json(prompt, db, tenant_id, max_tokens=400)
    raw_formulations = response.get("formulations")
    if not isinstance(raw_formulations, list):
        raise ValueError(f"LLM response missing a 'formulations' list: {response!r}")
    return _sanitize_formulations(raw_formulations, _forbidden_tokens(db, tenant_id))


def generate_investigation_action(db: Session, tenant_id: int, objective: InvestigationObjective, strategy: dict) -> dict:
    """`strategy` is exactly the dict select_sensing_strategy() (S3) returned for this objective.
    Never mutates `objective` -- the durable memory is untouched by this module (test: calling
    this twice for the same objective leaves its DB row identical)."""
    source = strategy.get("source")
    if not source:
        return _result(objective.id, None, None, None, STATUS_BLOCKED, f"no sensing strategy was selected: {strategy.get('rationale')}")

    try:
        check_can_run(db, tenant_id)
    except ControlPlaneHalted as e:
        return _result(objective.id, source, None, None, STATUS_BLOCKED, f"control plane is not running (state={e})")

    if not is_eligible_for_attempt(objective):
        return _result(objective.id, source, None, None, STATUS_BLOCKED, f"objective is not eligible for a new attempt right now (status={objective.status}, next_eligible_at={objective.next_eligible_at})")

    expected_evidence = strategy.get("expected_evidence") or objective.evidence_sought

    if source == "linkedin_post_search":
        try:
            formulations = _generate_formulations(db, tenant_id, objective, expected_evidence, LINKEDIN_PROMPT_TEMPLATE)
        except Exception as e:
            return _result(objective.id, source, "linkedin_post_search", None, STATUS_GENERATION_FAILED, str(e))
        if not formulations:
            return _result(objective.id, source, "linkedin_post_search", None, STATUS_GENERATION_FAILED, "LLM returned no usable formulations after sanitization")
        return _result(objective.id, source, "linkedin_post_search", {"queries": formulations}, STATUS_OK)

    if source == "web_search":
        try:
            formulations = _generate_formulations(db, tenant_id, objective, expected_evidence, WEB_SEARCH_PROMPT_TEMPLATE)
        except Exception as e:
            return _result(objective.id, source, "web_search", None, STATUS_GENERATION_FAILED, str(e))
        if not formulations:
            return _result(objective.id, source, "web_search", None, STATUS_GENERATION_FAILED, "LLM returned no usable formulations after sanitization")
        return _result(objective.id, source, "web_search", {"queries": formulations}, STATUS_OK)

    if source == "company_website":
        # No LLM call -- not a keyword query at all, per the explicit instruction. Uses the
        # real Company.domain directly; only ever selected by S3 when a domain is on file.
        company = db.get(Company, objective.target_company_id) if objective.target_company_id else None
        if not company or not company.domain:
            return _result(objective.id, source, "company_website", None, STATUS_GENERATION_FAILED, "no Company.domain on file for the target company")
        return _result(objective.id, source, "company_website", {"domain": company.domain, "company_id": company.id}, STATUS_OK)

    if source == "linkedin_job":
        # No LLM call -- structured filter parameters, not a keyword query, same discipline as
        # theirstack_job below. Real fix (2026-08-24): S5 previously had no execution path for
        # this source at all; reuses V1's own real, live-validated title_search/industry_filter
        # verbatim (app/phases/apify_discovery.py's APIFY_TITLE_SEARCH/APIFY_INDUSTRY_FILTER).
        #
        # Employee band is DELIBERATELY a separate, V2-owned constant, NOT V1's shared
        # APIFY_EMPLOYEE_MIN/MAX (2026-08-24 explicit instruction: V1's 25-50 band is
        # intentional and must stay untouched for V1's own real daily discovery; V2 -- with
        # multiple offerings, not just V1's fractional-leadership motion -- should also reach
        # somewhat larger companies). No revenue filter exists here or ever will via this
        # source: confirmed, neither Apify's LinkedIn Jobs actor nor the free Crustdata lookup
        # expose a real revenue/funding number to filter on (see apify_discovery.py's own
        # docstring) -- employee headcount is the only real, filterable proxy this source has.
        from app.phases.apify_discovery import APIFY_INDUSTRY_FILTER, APIFY_TITLE_SEARCH
        return _result(
            objective.id, source, "linkedin_job",
            {
                "title_search": APIFY_TITLE_SEARCH, "location_search": ["United States"],
                "organization_employees_gte": V2_LINKEDIN_JOB_EMPLOYEE_MIN, "organization_employees_lte": V2_LINKEDIN_JOB_EMPLOYEE_MAX,
                "industry_filter": APIFY_INDUSTRY_FILTER, "time_range": "7d",
            },
            STATUS_OK,
        )

    if source == "theirstack_job":
        # Real sense_theirstack_jobs(db, tenant_id, offset, limit, exclude_domains) signature --
        # its job-title/revenue/employee-count filters are fixed module constants in sensing.py,
        # NOT parameters the real adapter accepts per-call, so none of that is (re)invented here.
        # Only the parameters the real function genuinely exposes are surfaced.
        return _result(objective.id, source, "theirstack_job", {"offset": 0, "limit": 25, "exclude_domains": []}, STATUS_OK)

    if source in ("hackernews_story", "rss_article"):
        # Real sense_hackernews_stories(db, tenant_id, limit)/sense_rss_articles(db, tenant_id,
        # limit_per_feed) accept NO per-investigation query/keyword parameter at all -- both run
        # entirely against the tenant's pre-configured Content Intelligence topics/feeds. Honestly
        # reported rather than inventing a parameter the adapter can't consume.
        return _result(
            objective.id, source, source,
            {"note": "no per-investigation parameters exist for this source; it senses against pre-configured Content Intelligence topics/feeds only"},
            STATUS_OK,
        )

    return _result(objective.id, source, None, None, STATUS_GENERATION_FAILED, f"unsupported source for runtime generation: {source!r}")
