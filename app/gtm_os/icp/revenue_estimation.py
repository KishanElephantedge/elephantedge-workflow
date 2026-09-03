"""Real revenue estimation for ICP matching -- 2026-08-25, explicit instruction.

CONFIRMED GAP (research done before writing this): no per-company revenue-lookup mechanism
existed anywhere in this codebase before this file. app/phases/discovery.py's REVENUE_MIN_USD and
app/phases/jd_first_discovery.py's JD_FIRST_MAX_REVENUE_USD are query-FILTER constants sent to
Crustdata/TheirStack's own search indexes -- they only ever compare against whatever revenue
figure those providers' bulk search responses happen to already carry, never independently look
one up for a specific company. Every real V2 company (resolved via job/post signals, never through
that bulk Crustdata search) has null estimated_revenue_lower_usd/higher_usd as a result, which
silently fails every ICP's revenue check in icp_matching.py -- confirmed live: Rayvector
Technologies matched icp_3's exact hiring trigger perfectly and still failed to match, purely
because revenue was null.

FALLBACK CHAIN, cheapest/least-committal first (explicit instruction):
  1. Google AI Overview search (Apify-backed, already-existing search_google_ai_overview) --
     cheap (~$0.0085/query), budget-gated via the real Apify guard.
  2. Deepline's free, exact-domain Crustdata identify call (crustdata_v3_company_identify --
     the SAME free lookup app/phases/jd_first_discovery.py's _real_firmographics() already uses
     for firmographics, reused here, not re-implemented) -- only when a real domain is known.
  3. If neither finds a real number: reports "not_found" honestly. Whether to then fall back to
     headcount as an ICP-matching proxy is a real ICP-configuration policy decision (what
     headcount range should count as which revenue band) that this module does NOT make --
     that's for icp_config.py's own thresholds, set by a human, not guessed here.

Same evidence-integrity discipline as every other LLM-assisted extraction added this session
(linkedin_post_interpretation.py, linkedin_job_interpretation.py): the LLM must quote the exact
source text a revenue figure came from; a quote that doesn't verifiably appear in the real search
result is discarded, never trusted blind. No revenue number is ever invented from a bare company
name/domain alone."""

import logging
import re

from sqlalchemy.orm import Session

from app.apify_budget_guard import STATUS_ALLOWED as APIFY_BUDGET_ALLOWED
from app.apify_budget_guard import check_apify_budget
from app.apify_client import GOOGLE_SEARCH_COST_PER_QUERY_USD, ApifyError
from app.apify_client import _get_api_key as _get_apify_api_key
from app.apify_client import search_google_ai_overview
from app.db.models import Batch, Company
from app.deepline_client import execute_tool
from app.llm_client import generate_json

logger = logging.getLogger(__name__)


def _extract_revenue_from_text(text: str, db: Session, tenant_id: int) -> dict | None:
    """Given real free text (a Google AI Overview, or a firmographic description), asks the LLM
    to extract an annual revenue figure IF the text actually states one. Requires an exact quote
    verified against the real text before trusting it -- same guard as every other LLM extraction
    added this session. Returns {"lower_usd": int, "higher_usd": int, "quote": str} or None."""
    prompt = f"""You are extracting a company's ANNUAL REVENUE from real text, for a B2B sales-intelligence system.

Text:
\"\"\"{text}\"\"\"

If the text states a specific annual revenue figure or range for THIS EXACT company (e.g. "$5M in
annual revenue", "revenue of $10-20 million"), extract it. Otherwise return has_revenue: false.

Return has_revenue: false in ALL of these cases -- they are the ones that produce wrong numbers:
- The text says revenue is private, undisclosed, not reported, or estimated only ("has not
  publicly disclosed", "figures remain private"). A valuation or funding total sitting next to
  that sentence is NOT a substitute.
- The number is funding raised, valuation, total capital, market cap, or a growth multiple
  ("500% growth", "20x increase"). None of those are revenue.
- The text mentions other companies with a similar name, or you cannot tell which company a
  figure belongs to. A revenue figure for the wrong company is worse than no figure at all.

Return JSON exactly:
{{"has_revenue": true | false, "lower_usd": <int or null>, "higher_usd": <int or null>, "quote": "<exact sentence/clause from the text above stating the revenue figure, verbatim, or empty string if has_revenue is false>"}}"""
    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=200)
    except Exception:
        return None  # LLM unavailable -- fail closed, no fabricated number

    if not response.get("has_revenue"):
        return None

    quote = (response.get("quote") or "").strip()
    if not quote or quote.lower() not in text.lower():
        return None  # unverifiable/hallucinated -- discarded, not trusted

    # The model can still latch onto a valuation sitting beside a non-disclosure sentence, so the
    # same refusals are enforced deterministically rather than trusted to the prompt. Confirmed
    # live 2026-09-03: Google's own overview for Serval said "has not publicly disclosed its exact
    # financial revenue figures" and we stored a figure anyway; for Campfire it warned about
    # "other similarly named entities" and we took the number regardless.
    low_text = text.lower()
    NON_DISCLOSURE = ["not publicly disclose", "not disclosed", "remain private", "are private",
                      "does not disclose", "undisclosed", "not publicly available", "not reported"]
    AMBIGUITY = ["similarly named", "similar name", "other companies named", "not to be confused",
                 "separate estimated revenues", "different company"]
    if any(k in low_text for k in NON_DISCLOSURE):
        return None
    if any(k in low_text for k in AMBIGUITY):
        return None

    lower_usd = response.get("lower_usd")
    higher_usd = response.get("higher_usd")
    if not isinstance(lower_usd, int) and not isinstance(higher_usd, int):
        return None  # claimed has_revenue but gave no usable number -- discard, don't guess

    if lower_usd is None:
        lower_usd = higher_usd
    if higher_usd is None:
        higher_usd = lower_usd

    return {"lower_usd": lower_usd, "higher_usd": higher_usd, "quote": quote}



# The empirical spread across this tenant's own 562 companies with both figures is a median of
# $79,545 revenue per employee, p25 $50,000, p75 $125,000 (see icp_matching.REVENUE_PER_EMPLOYEE_USD).
# A figure implying far outside that is almost always the WRONG COMPANY rather than an unusual one:
# Domaine (271 employees) came back at $100-250M, i.e. up to $923k/employee, when Sales Navigator
# says $10-20M; PDQ (394) came back at up to $500M, i.e. $1.27M/employee, against a real $20-50M.
# Both are real revenue figures -- for a different business sharing the name.
#
# The ceiling is 4x the p75. An 8x ceiling was tried first and let Domaine through at
# $922k/employee against a real $10-20M -- generous bounds do not help if they still admit the
# case they exist to catch.
#
# The FLOOR is deliberately far lower than the p25, not symmetric with the ceiling. A well-funded
# early-stage company genuinely earns very little per head: Serval has 177 employees on
# $500k-$1M revenue ($2.8k/employee) after raising $127M, and that figure is correct. A symmetric
# floor would reject exactly the young companies these partner ICPs often want, which is the
# opposite of the mistake being fixed here.
REVENUE_PER_EMPLOYEE_MAX_USD = 500_000
REVENUE_PER_EMPLOYEE_MIN_USD = 2_000


def _implausible_for_headcount(lower_usd: int | None, higher_usd: int | None, headcount: int | None) -> str | None:
    """Returns why a figure is implausible for the company's size, or None if it is credible."""
    if not headcount or headcount < 5:
        return None
    top = higher_usd or lower_usd
    bottom = lower_usd or higher_usd
    if top and top / headcount > REVENUE_PER_EMPLOYEE_MAX_USD:
        return (f"${top:,} across {headcount} employees is ${top / headcount:,.0f}/employee, "
                f"above the ${REVENUE_PER_EMPLOYEE_MAX_USD:,} ceiling -- probably a different company")
    if bottom and bottom / headcount < REVENUE_PER_EMPLOYEE_MIN_USD:
        return (f"${bottom:,} across {headcount} employees is ${bottom / headcount:,.0f}/employee, "
                f"below the ${REVENUE_PER_EMPLOYEE_MIN_USD:,} floor -- probably a different company")
    return None


def _estimate_via_google(db: Session, tenant_id: int, company: Company) -> dict:
    """Step 1 of the fallback chain -- cheap, tried first."""
    try:
        api_key = _get_apify_api_key(db, tenant_id)
    except ApifyError as e:
        return {"status": "unavailable", "reason": f"apify_credential_unavailable: {e}"}

    budget = check_apify_budget(db, tenant_id, GOOGLE_SEARCH_COST_PER_QUERY_USD)
    if budget["status"] != APIFY_BUDGET_ALLOWED:
        return {"status": "unavailable", "reason": f"apify_budget_blocked: {budget['reason']}"}

    # Domain-qualified, not name-only (2026-08-31, real mis-attribution confirmed live): the
    # name-only query "Aligned annual revenue" returned $4.035B -- Aligned Data Centers, a
    # completely different company from alignedup.com, a ~80-person Series B startup. A wrong
    # revenue is strictly worse than none: it silently files a company into the wrong ICP
    # revenue band and qualifies (or disqualifies) it on a fabricated basis. The domain is the
    # one unambiguous identifier we always have here, so it goes in the query.
    query = f"{company.name} {company.domain} annual revenue" if company.domain else f"{company.name} annual revenue"
    try:
        overview_text = search_google_ai_overview(api_key, query)
    except ApifyError as e:
        return {"status": "unavailable", "reason": f"google_search_provider_error: {e}"}

    if not overview_text:
        return {"status": "not_found", "reason": "no_ai_overview_generated_for_query"}

    extracted = _extract_revenue_from_text(overview_text, db, tenant_id)
    if extracted is None:
        return {"status": "not_found", "reason": "ai_overview_had_no_verifiable_revenue_figure"}

    return {"status": "found", "source": "google_ai_overview", **extracted}


def _estimate_via_deepline_identify(db: Session, tenant_id: int, company: Company) -> dict:
    """Step 2 of the fallback chain -- Deepline's free, exact-domain Crustdata identify call,
    the same one app/phases/jd_first_discovery.py's _real_firmographics() already uses. Only
    attempted when a real domain is on file; fails closed (never guesses) on any collision,
    error, or missing revenue field."""
    if not company.domain:
        return {"status": "unavailable", "reason": "no_domain_on_file_for_this_company"}

    try:
        response = execute_tool("crustdata_v3_company_identify", {"domains": [company.domain]})
    except Exception as e:
        return {"status": "unavailable", "reason": f"deepline_tool_error: {e}"}

    raw = (response or {}).get("toolResponse", {}).get("raw", {})
    if not raw or not isinstance(raw, list):
        return {"status": "not_found", "reason": "no_deepline_identify_result"}

    matches = raw[0].get("matches", [])
    distinct_names = {m.get("company_data", {}).get("basic_info", {}).get("name") for m in matches}
    if len(distinct_names) != 1:
        return {"status": "not_found", "reason": "domain_collision_or_no_match -- cannot trust which company this is"}

    basic_info = matches[0]["company_data"]["basic_info"] if matches else {}
    lower_usd = basic_info.get("estimated_revenue_lower_bound_usd")
    higher_usd = basic_info.get("estimated_revenue_higher_bound_usd")
    if lower_usd is None and higher_usd is None:
        return {"status": "not_found", "reason": "deepline_identify_result_has_no_revenue_field"}

    return {
        "status": "found", "source": "deepline_crustdata_identify",
        "lower_usd": lower_usd if lower_usd is not None else higher_usd,
        "higher_usd": higher_usd if higher_usd is not None else lower_usd,
        "quote": f"crustdata_v3_company_identify basic_info for domain {company.domain!r}",
    }


def estimate_company_revenue(db: Session, tenant_id: int, company: Company) -> dict:
    """Real fallback chain, cheapest first -- see module docstring. Writes
    estimated_revenue_lower_usd/higher_usd onto `company` and commits ONLY when a real,
    verified figure is found. Returns a structured result reporting exactly what was tried and
    why, never silent. Does nothing (returns immediately) if the company already has a revenue
    estimate on file -- never overwrites existing real data."""
    if company.estimated_revenue_lower_usd is not None or company.estimated_revenue_higher_usd is not None:
        return {
            "status": "already_had_revenue",
            "lower_usd": company.estimated_revenue_lower_usd,
            "higher_usd": company.estimated_revenue_higher_usd,
        }

    attempts = []

    google_result = _estimate_via_google(db, tenant_id, company)
    if google_result.get("status") == "found":
        why = _implausible_for_headcount(google_result.get("lower_usd"), google_result.get("higher_usd"), company.employee_count)
        if why:
            google_result = {"status": "rejected_implausible", "reason": why, "quote": google_result.get("quote")}
    attempts.append({"source": "google_ai_overview", **google_result})
    if google_result["status"] == "found":
        company.estimated_revenue_lower_usd = google_result["lower_usd"]
        company.estimated_revenue_higher_usd = google_result["higher_usd"]
        db.commit()
        return {"status": "resolved", "source": "google_ai_overview", "lower_usd": google_result["lower_usd"], "higher_usd": google_result["higher_usd"], "attempts": attempts}

    deepline_result = _estimate_via_deepline_identify(db, tenant_id, company)
    if deepline_result.get("status") == "found":
        why = _implausible_for_headcount(deepline_result.get("lower_usd"), deepline_result.get("higher_usd"), company.employee_count)
        if why:
            deepline_result = {"status": "rejected_implausible", "reason": why}
    attempts.append({"source": "deepline_crustdata_identify", **deepline_result})
    if deepline_result["status"] == "found":
        company.estimated_revenue_lower_usd = deepline_result["lower_usd"]
        company.estimated_revenue_higher_usd = deepline_result["higher_usd"]
        db.commit()
        return {"status": "resolved", "source": "deepline_crustdata_identify", "lower_usd": deepline_result["lower_usd"], "higher_usd": deepline_result["higher_usd"], "attempts": attempts}

    return {"status": "not_found", "attempts": attempts}


def run_revenue_backfill_sweep(db: Session, tenant_id: int, limit: int = 30) -> dict:
    """2026-08-26, real fix -- confirmed live: 1,461 of 1,500 real ICP checks in one run came back
    "insufficient_information", overwhelmingly because revenue is null, not because a company was
    genuinely evaluated and disqualified. estimate_company_revenue() existed but had no automatic
    caller anywhere -- it only ever ran when invoked by hand. This sweep runs it, bounded, for
    companies genuinely missing revenue, BEFORE icp_matching's own sweep in the same cycle (see
    orchestration/sweep.py), so a company enriched here gets a real chance at a real ICP verdict
    in the SAME run instead of waiting for a future one.

    Bounded at limit=30 by default (not the icp_matching-sized 500): unlike ICP matching (free,
    local computation), each company here can trigger a real, budget-gated Apify call -- a small
    default keeps this sweep's real cost predictable per run rather than trying to enrich the
    whole backlog at once. One company's failure never aborts the sweep."""
    counts = {"evaluated": 0, "resolved": 0, "not_found": 0, "unavailable": 0, "failed": 0}

    company_ids = [
        row[0]
        for row in db.query(Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id)
        .filter(Company.estimated_revenue_lower_usd.is_(None), Company.estimated_revenue_higher_usd.is_(None))
        .order_by(Company.icp_last_evaluated_at.is_(None).desc(), Company.icp_last_evaluated_at.asc())
        .limit(limit)
        .all()
    ]

    for company_id in company_ids:
        try:
            company = db.get(Company, company_id)
            if company is None:
                continue
            counts["evaluated"] += 1
            result = estimate_company_revenue(db, tenant_id, company)
            status = result["status"]
            if status in ("resolved", "already_had_revenue"):
                counts["resolved"] += 1
            elif status == "not_found":
                counts["not_found"] += 1
            else:
                counts["unavailable"] += 1
        except Exception as e:  # noqa: BLE001 -- one company's failure must never block the others
            db.rollback()  # same real fix as contact_discovery.py/icp_matching.py -- never leave the shared session invalid for the next company
            counts["failed"] += 1
            logger.error("run_revenue_backfill_sweep: company %s failed unexpectedly -- %s", company_id, e)

    return counts
