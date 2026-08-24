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


def _post(url: str, **kwargs) -> httpx.Response:
    """Real bug fix (2026-08-19): every call site below used a bare httpx.post() with no
    exception handling at all -- confirmed live (production batch 63) that a genuine network
    timeout (httpx.ReadTimeout, "The read operation timed out") propagated all the way up
    through find_free_decision_maker() -> find_decision_maker() -> the per-company loop in
    autonomous_orchestrator.py, crashing the entire day's run outright after 15 companies had
    already been discovered and 6 real contacts found. Every caller in this codebase already
    only catches `ApifyError` (never a raw httpx exception), so a real network-level failure --
    not a bad status code, an actual timeout/connection error -- was invisible to that handling.
    This is the same class of bug already fixed twice elsewhere for Deepline (decision_maker.py,
    hiring_signal.py); this is the equivalent fix for Apify's own client."""
    try:
        return httpx.post(url, **kwargs)
    except httpx.HTTPError as e:
        raise ApifyError(f"request to {url} failed: {e}") from e


def _get(url: str, **kwargs) -> httpx.Response:
    """Same network-exception-wrapping discipline as _post() above, for the read-only account/
    usage endpoints (Phase S8) -- these are plain GETs, not actor-run calls."""
    try:
        return httpx.get(url, **kwargs)
    except httpx.HTTPError as e:
        raise ApifyError(f"request to {url} failed: {e}") from e


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
    response = _post(
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
    response = _post(
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


# Same actor/endpoint as search_google_ai_overview() above, but reads organicResults instead of
# aiOverview -- confirmed live (2026-08-24) that this field is real and already present in every
# response from this actor, just never read before. AI Overview is explicitly disabled here
# (not needed for organic-result discovery), saving the $0.003/query AI Overview cost --
# see GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD below.
GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD = 0.0055  # $0.0045 page + $0.001 start (no AI Overview)


def search_google_organic_results(api_key: str, query: str, max_pages: int = 1) -> list[dict]:
    """Returns the real organicResults list for one query (each item carries at least url/title/
    description, per this actor's own real response shape, confirmed live 2026-08-24) -- used for
    genuine keyword-based content discovery, as opposed to search_google_ai_overview()'s single
    summarized-text use case."""
    response = _post(
        f"{BASE_URL}/acts/{GOOGLE_SEARCH_ACTOR_ID}/run-sync-get-dataset-items",
        params={"token": api_key},
        json={"queries": query, "maxPagesPerQuery": max_pages, "aiOverview": {"enabled": False}},
        timeout=60,
    )
    if response.status_code >= 300:
        raise ApifyError(f"Google search failed ({response.status_code}): {response.text[:500]}")
    items = response.json()
    if not items:
        return []
    return items[0].get("organicResults") or []


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


# supreme_coder/linkedin-post -- Deepline's own currently-recommended actor for generic
# LinkedIn post scraping (per its provider playbook, 2026-08). Pay-per-event: ~$2/1000 posts
# scraped ($0.002/post), NOT billed per API call/profile checked -- confirmed via the actor's
# own Store listing. This matters for polling: a profile with no new posts since the last
# check should cost close to nothing, not a flat per-check fee, since `scrapeUntil` filters
# server-side before billing.
LINKEDIN_POST_ACTOR_ID = "supreme_coder~linkedin-post"
LINKEDIN_POST_COST_PER_POST_USD = 0.002


def search_linkedin_posts(api_key: str, profile_urls: list[str], scrape_until: str | None = None, limit_per_source: int = 5) -> list[dict]:
    """Returns recent posts for each given LinkedIn profile/company/post URL. scrape_until
    (ISO date string) filters to posts newer than that date -- pass the last time this profile
    was checked to avoid re-billing for posts already seen.

    Real bug fix (2026-08-24, confirmed live -- run 94/95): the previous 120s client timeout was
    too short for this actor's real synchronous run time when scraping real LinkedIn post pages
    -- confirmed via a real production error, "The read operation timed out," on a call that
    only requested 1 post. run-sync-get-dataset-items blocks until the actor run itself
    completes server-side; a client-side timeout shorter than that just gives up on a run that
    was still genuinely in progress, wasting the spend with nothing to show for it. Raised to
    240s -- still bounded, just realistic for this actor's real observed latency."""
    payload = {"urls": profile_urls, "limitPerSource": limit_per_source, "deepScrape": False}
    if scrape_until:
        payload["scrapeUntil"] = scrape_until
    response = _post(
        f"{BASE_URL}/acts/{LINKEDIN_POST_ACTOR_ID}/run-sync-get-dataset-items",
        params={"token": api_key},
        json=payload,
        timeout=240,
    )
    if response.status_code >= 300:
        raise ApifyError(f"LinkedIn post search failed ({response.status_code}): {response.text[:500]}")
    return response.json()


def search_linkedin_people(api_key: str, first_name: str, last_name: str, company: str, max_results: int = 3) -> list[dict]:
    payload = {
        "mode": "public",
        "firstName": first_name,
        "lastName": last_name,
        "company": company,
        "maxResults": max_results,
    }
    response = _post(
        f"{BASE_URL}/acts/{PEOPLE_SEARCH_ACTOR_ID}/run-sync-get-dataset-items",
        params={"token": api_key},
        json=payload,
        timeout=60,
    )
    if response.status_code >= 300:
        raise ApifyError(f"LinkedIn people search failed ({response.status_code}): {response.text[:500]}")
    return response.json()


# GET /v2/users/me/usage/monthly -- Phase S8 (Apify budget guard). Confirmed via Apify's own
# official docs (docs.apify.com/api/v2/users-me-usage-monthly-get, fetched 2026-08-22): returns
# the SAME data shown on the account's own Billing > Historical usage page -- explicitly NOT
# real-time ("the same information you will see on your account's Billing page"), so this is the
# best real spend signal available, not an instant balance. Real, literal response shape
# (confirmed from the docs' own example, not guessed):
#   {"data": {
#       "usageCycle": {"startAt": "...", "endAt": "..."},
#       "monthlyServiceUsage": {...per-service breakdown...},
#       "dailyServiceUsages": [{"date": "2022-10-02T00:00:00.000Z", "serviceUsage": {...},
#                                "totalUsageCreditsUsd": 0.047...}, ...],
#       "totalUsageCreditsUsdBeforeVolumeDiscount": 0.786...,
#       "totalUsageCreditsUsdAfterVolumeDiscount": 0.786...
#   }}
# There is NO pre-call balance/remaining-credit endpoint anywhere in Apify's real API -- actor
# runs only report their own usageTotalUsd AFTER completing (Apify's own docs call this
# "informational only, not exact figures"), and this monthly-usage endpoint is the only real,
# queryable "spend so far" figure that exists before making a new call. apify_budget_guard.py
# uses dailyServiceUsages' per-day totalUsageCreditsUsd and totalUsageCreditsUsdAfterVolumeDiscount
# as the real (if slightly lagged) spend figures -- never a locally-accumulated estimate.
def get_monthly_usage(api_key: str) -> dict:
    response = _get(f"{BASE_URL}/users/me/usage/monthly", params={"token": api_key}, timeout=30)
    if response.status_code >= 300:
        raise ApifyError(f"Apify monthly usage fetch failed ({response.status_code}): {response.text[:500]}")
    return response.json().get("data", {})
