"""Apify REST API client -- runs the LinkedIn Jobs Scraper actor (fantastic-jobs/advanced-
linkedin-job-search-api) synchronously and returns its dataset items directly. Validated live
(2026-08-05/06): a single 24h day-wide pull against this actor found a real, current-day pool
of tech/SaaS companies denser and fresher than TheirStack's aggregated index at the same hour
(8/8 and 5/5 real qualifying companies across two real test days, vs. TheirStack's 2/5 on the
intervening real production day), at ~$0.005/job + $0.01/run flat fee."""
import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

BASE_URL = "https://api.apify.com/v2"
# fantastic-jobs/advanced-linkedin-job-search-api -- real-time LinkedIn jobs index, 10M+ new
# jobs/month, updated hourly (confirmed via Apify Store's own actor metadata).
LINKEDIN_JOBS_ACTOR_ID = "vIGxjRrHqDTPuE6M4"

# Real per-event pricing confirmed live across multiple test runs on 2026-08-05/06. Used only
# for a local spend estimate -- this sync endpoint doesn't return the actual billed amount.
COST_PER_JOB_USD = 0.005
COST_PER_RUN_USD = 0.01


class ApifyError(Exception):
    pass


def _get_api_key(db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == "apify_api_key")
        .first()
    )
    if not cred or not cred.value:
        raise ApifyError("apify_api_key credential is not set")
    return cred.value


def search_linkedin_jobs(
    api_key: str,
    title_search: list[str],
    location_search: list[str],
    organization_employees_gte: int,
    organization_employees_lte: int,
    industry_filter: list[str],
    time_range: str = "24h",
    remove_agency: bool = True,
    limit: int = 100,
) -> list[dict]:
    """Runs the actor synchronously and returns its dataset items directly.

    industry_filter: exact LinkedIn industry names, comma-free only -- confirmed live that an
    industry name containing a comma (e.g. "Technology, Information and Internet") silently
    breaks this filter and returns zero results instead of erroring, so callers must only pass
    comma-free industry names here."""
    payload = {
        "titleSearch": title_search,
        "locationSearch": location_search,
        "organizationEmployeesGte": organization_employees_gte,
        "organizationEmployeesLte": organization_employees_lte,
        "industryFilter": industry_filter,
        "timeRange": time_range,
        "removeAgency": remove_agency,
        "descriptionType": "text",
        "limit": limit,
    }
    response = httpx.post(
        f"{BASE_URL}/acts/{LINKEDIN_JOBS_ACTOR_ID}/run-sync-get-dataset-items",
        params={"token": api_key},
        json=payload,
        timeout=90,
    )
    if response.status_code >= 300:
        # Confirmed live (2026-08-07): run-sync-get-dataset-items can legitimately return 201,
        # not just 200, for a real successful run with real dataset items in the body -- the
        # old `!= 200` check rejected a genuine successful result as an error.
        raise ApifyError(f"LinkedIn jobs search failed ({response.status_code}): {response.text[:500]}")
    return response.json()


def estimate_cost_usd(job_count: int) -> float:
    return job_count * COST_PER_JOB_USD + COST_PER_RUN_USD


# apify/google-search-scraper -- used here specifically for its AI Overview add-on, which
# synthesizes an answer from Google's own knowledge graph + top results rather than us having
# to parse organic snippets ourselves. Confirmed live (2026-08-10/11): real, correct answers
# for real companies (DAKCS, GovEase) our other decision-maker sources missed entirely -- but
# also a real, dangerous false-positive for a short/generic company name ("Nebi" confused with
# the much larger "Nebius"), so callers MUST domain-qualify the query and verify the result
# against LinkedIn before trusting it (see free_decision_maker.py).
GOOGLE_SEARCH_ACTOR_ID = "nFJndFXA5zjCTuudP"
GOOGLE_SEARCH_COST_PER_QUERY_USD = 0.0085  # $0.0045 page + $0.003 AI Overview + $0.001 start


def search_google_ai_overview(api_key: str, query: str) -> str | None:
    """Returns the AI Overview's text content for one query, or None if Google didn't
    generate one (happens for some queries -- not an error)."""
    response = httpx.post(
        f"{BASE_URL}/acts/{GOOGLE_SEARCH_ACTOR_ID}/run-sync-get-dataset-items",
        params={"token": api_key},
        json={"queries": query, "maxPagesPerQuery": 1, "aiOverview": {"enabled": True}},
        timeout=60,
    )
    if response.status_code >= 300:
        raise ApifyError(f"Google search failed ({response.status_code}): {response.text[:500]}")
    items = response.json()
    if not items:
        return None
    overview = items[0].get("aiOverview") or {}
    return overview.get("content") or None


# memo23/linkedin-people-search -- searches LinkedIn's own data directly (not Google's index of
# it), unlike a generic web search. Validated live (2026-08-08): a name+company search returned
# the exact, cross-confirmed correct profile for one real person (Karan Talati, First
# Resonance) -- but also a real miss on a second person (Neal Sarraf, same company): the
# `company` filter appears to return "people who work here" ranked by prominence rather than
# strictly matching firstName/lastName, so a candidate's returned `name` must be verified
# against the requested name before ever trusting it (see find_person_free.py).
PEOPLE_SEARCH_ACTOR_ID = "xfz4tG0OB6ClIGVGv"
PEOPLE_SEARCH_COST_PER_START_USD = 0.005
PEOPLE_SEARCH_COST_PER_PROFILE_USD = 0.004


def search_linkedin_people(api_key: str, first_name: str, last_name: str, company: str, max_results: int = 3) -> list[dict]:
    payload = {
        "mode": "public",
        "firstName": first_name,
        "lastName": last_name,
        "company": company,
        "maxResults": max_results,
    }
    response = httpx.post(
        f"{BASE_URL}/acts/{PEOPLE_SEARCH_ACTOR_ID}/run-sync-get-dataset-items",
        params={"token": api_key},
        json=payload,
        timeout=60,
    )
    if response.status_code >= 300:
        raise ApifyError(f"LinkedIn people search failed ({response.status_code}): {response.text[:500]}")
    return response.json()
