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
    if response.status_code != 200:
        raise ApifyError(f"LinkedIn jobs search failed ({response.status_code}): {response.text[:500]}")
    return response.json()


def estimate_cost_usd(job_count: int) -> float:
    return job_count * COST_PER_JOB_USD + COST_PER_RUN_USD
