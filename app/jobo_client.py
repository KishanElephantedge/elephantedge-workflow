"""Jobo API client -- its own separate credit system from Deepline (X-Credits-Balance
response header, no separate balance-check call needed), so this mirrors deepline_client.py's
shape but is deliberately its own module, not a Deepline provider."""
import threading
import time

import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

BASE_URL = "https://connect.jobo.world"
USD_PER_CREDIT = 0.001

# Process-wide minimum spacing between real calls to /api/jobs/search -- the endpoint both
# find_company_id_by_name and search_jobs share, and Jobo rate-limits it (confirmed live
# 2026-09-11: a real leadership call for Kiteworks, a company with 15 real leaders on file,
# returned 0 candidates when made as the 5th+ call in a tight per-company loop with no delay,
# then returned all 15 correctly when the SAME call was made in isolation seconds later).
#
# find_decision_makers() calls this once per company with no pacing of its own in
# autonomous_orchestrator.py's two decision-maker loops (production, not just test scripts) --
# a batch of even 5 companies was enough to trigger it. free_decision_maker.py's own 429
# retry/backoff (2026-09-10) only helps AFTER a call already failed; it does not stop the burst
# from happening in the first place, and a burst tight enough can exhaust several retries in a
# row too. A proactive minimum interval at the one shared endpoint fixes every caller at once --
# scripts, the daily engine, both -- rather than requiring each call site to remember to pace
# itself.
_last_jobs_search_call: list[float] = [0.0]
_jobs_search_lock = threading.Lock()
JOBS_SEARCH_MIN_INTERVAL_SECONDS = 2.0


def _throttle_jobs_search() -> None:
    with _jobs_search_lock:
        elapsed = time.monotonic() - _last_jobs_search_call[0]
        if elapsed < JOBS_SEARCH_MIN_INTERVAL_SECONDS:
            time.sleep(JOBS_SEARCH_MIN_INTERVAL_SECONDS - elapsed)
        _last_jobs_search_call[0] = time.monotonic()


class JoboError(Exception):
    pass


ELEPHANT_EDGE_TENANT_ID = 2


def _get_api_key(db: Session, tenant_id: int) -> str:
    """Same shared-key-with-fallback pattern as claude_client.py/gemini_client.py/
    apify_client.py: a tenant with its own jobo_api_key uses it; otherwise falls back to
    Elephant Edge's, since the underlying Jobo account is genuinely shared, not separable
    per tenant.

    Real, confirmed root cause of a wider bug (2026-09-11): no partner tenant has ever had its
    own jobo_api_key, and this function had no fallback -- every partner decision-maker lookup
    hit JoboError immediately and was silently swallowed by
    free_decision_maker._jobo_leadership_candidates() into "0 candidates", indistinguishable
    from a genuine miss. Confirmed live: Kiteworks (matched to Jeff Ballard, tenant 5) returned 0
    candidates via the real code path, then 15 real ones (CEO, CRO, CMO, board) when queried with
    Elephant Edge's own key instead -- Jobo had the data the entire time."""
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == "jobo_api_key")
        .first()
    )
    if cred and cred.value:
        return cred.value
    if tenant_id != ELEPHANT_EDGE_TENANT_ID:
        ee_cred = (
            db.query(Credential)
            .filter(Credential.tenant_id == ELEPHANT_EDGE_TENANT_ID)
            .filter(Credential.name == "jobo_api_key")
            .first()
        )
        if ee_cred and ee_cred.value:
            return ee_cred.value
    raise JoboError("jobo_api_key credential is not set")


class JoboCreditGuard:
    """Fail-safe, checked-after-every-company credit cap -- mirrors app/budget_guard.py's
    pattern, but reads Jobo's own real-time X-Credits-Balance response header instead of a
    separate balance-check call."""

    def __init__(self, cap_usd: float):
        self.cap_usd = cap_usd
        self.start_balance_credits: int | None = None
        self.latest_balance_credits: int | None = None

    def record(self, balance_credits: int) -> None:
        if self.start_balance_credits is None:
            self.start_balance_credits = balance_credits
        self.latest_balance_credits = balance_credits

    def spent_usd(self) -> float:
        if self.start_balance_credits is None or self.latest_balance_credits is None:
            return 0.0
        return (self.start_balance_credits - self.latest_balance_credits) * USD_PER_CREDIT

    def check(self) -> None:
        if self.spent_usd() >= self.cap_usd:
            raise JoboError(f"JoboCreditGuard cap reached: spent ${self.spent_usd():.3f} of ${self.cap_usd:.2f}")


def search_jobs(client: httpx.Client, api_key: str, queries: list[str], page: int, page_size: int,
                 locations: list[str] | None = None, include_facets: list[str] | None = None) -> tuple[dict, int]:
    """locations: real, documented server-side filter (POST body field `locations`, a plain
    string array) -- found live (2026-08-07) that the original integration never passed this
    at all, so every search paid to fetch and then locally reject non-US postings (86 of 349
    in one real test run) instead of excluding them for free at the query level.

    include_facets: bundled into the same metered call at no extra cost per Jobo's docs --
    used here to see the real industries composition of results without a separate paid
    call or guessing at canonical industry name strings before filtering by them."""
    body = {"queries": queries, "page": page, "page_size": page_size, "include_fields": ["description"]}
    if locations:
        body["locations"] = locations
    if include_facets:
        body["include_facets"] = include_facets
    _throttle_jobs_search()
    response = client.post(
        f"{BASE_URL}/api/jobs/search",
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        json=body,
        timeout=120,
    )
    response.raise_for_status()
    balance = int(response.headers.get("x-credits-balance", 0))
    return response.json(), balance


def get_company_profile(client: httpx.Client, company_id: str) -> dict | None:
    """Free, unmetered lookup -- no API key required per Jobo's own docs."""
    response = client.get(f"{BASE_URL}/api/companies/{company_id}", timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def find_company_id_by_name(client: httpx.Client, api_key: str, company_name: str) -> str | None:
    """Jobo has no company search/filter endpoint at all (confirmed against their own docs
    2026-08-08) -- the only way to resolve a company's internal ID is via a job-search hit
    whose embedded company summary happens to match. Small, cheap, bounded (page_size=3);
    costs $0 if there's no matching posting (metering is per delivered job, not per request).
    Returns None on no match -- caller falls through to whatever's next in the chain."""
    _throttle_jobs_search()
    response = client.post(
        f"{BASE_URL}/api/jobs/search",
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        json={"queries": [company_name], "page": 1, "page_size": 3, "include_fields": []},
        timeout=60,
    )
    response.raise_for_status()
    jobs = response.json().get("jobs", [])
    for job in jobs:
        company = job.get("company") or {}
        if company_name.strip().lower() in (company.get("name") or "").lower():
            return company.get("id")
    return None
