"""Direct Prospeo Search Person API (https://prospeo.io/api-docs/search-person).

Why direct and not through Deepline: Prospeo bills 1 credit per search PAGE of up to 25 people
(0 on no results, and a repeat of the same request within 30 days is free), while Deepline's
prospeo_search_person bills $0.055 per PERSON returned -- $1.375 for the same page. For bulk
search that is a 100x+ difference for identical data (checked 2026-09-27).

One search already carries the hiring signal (company_job_posting_hiring_for), the revenue band
(company_revenue), size, industry, HQ and the decision maker with their LinkedIn URL -- everything
a LinkedIn campaign needs. company_revenue and company_job_posting_hiring_for need Prospeo's
Starter plan or above; without it the API answers PLAN_REQUIRED.

Every request is reserved against the tenant's combined spend ledger before it is sent.
"""
from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

SEARCH_PERSON_URL = "https://api.prospeo.io/search-person"
PROVIDER_PROSPEO = "prospeo"
# Add-on credits are $10 per 1,000 (prospeo.io/pricing); plan credits are cheaper. Used as the
# ledger estimate for one search request, so it errs on the high side.
USD_PER_CREDIT = 0.01
RESULTS_PER_PAGE = 25

# Prospeo's revenue filter takes fixed steps, not free numbers.
REVENUE_STEPS = [
    ("100K", 100_000), ("500K", 500_000), ("1M", 1_000_000), ("5M", 5_000_000), ("10M", 10_000_000),
    ("25M", 25_000_000), ("50M", 50_000_000), ("100M", 100_000_000), ("250M", 250_000_000),
    ("500M", 500_000_000), ("1B", 1_000_000_000), ("5B", 5_000_000_000),
]


class ProspeoError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def get_api_key(db: Session, tenant_id: int) -> str | None:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id, Credential.name == "prospeo_api_key")
        .first()
    )
    return cred.value if cred and cred.value else None


def revenue_filter(min_usd: int | None, max_usd: int | None) -> dict | None:
    """ICP revenue band -> Prospeo's steps, widened outward to the nearest step (a $10-20M ICP
    becomes 10M-25M). The Qualifier still judges the exact band per company."""
    if min_usd is None and max_usd is None:
        return None
    f: dict = {"include_unknown_revenue": False}
    if min_usd is not None:
        f["min"] = next((label for label, v in reversed(REVENUE_STEPS) if v <= min_usd), REVENUE_STEPS[0][0])
    if max_usd is not None:
        f["max"] = next((label for label, v in REVENUE_STEPS if v >= max_usd), "10B+")
    return f


def search_person(db: Session, tenant_id: int, filters: dict, page: int = 1, timeout: int = 30) -> dict:
    """One search page. Returns {"results": [...], "pagination": {...}}; an empty result set is
    {"results": []} (Prospeo reports it as NO_RESULTS and charges nothing). Raises ProspeoError
    on any other failure, and SpendBlocked when the ledger refuses the request."""
    from app.spend_ledger import reserve_spend, settle_spend

    api_key = get_api_key(db, tenant_id)
    if not api_key:
        raise ProspeoError("prospeo_api_key credential is not set", code="NO_API_KEY")

    ledger_id = reserve_spend(db, tenant_id, PROVIDER_PROSPEO, USD_PER_CREDIT, operation="search_person")
    try:
        response = httpx.post(
            SEARCH_PERSON_URL, json={"page": page, "filters": filters},
            headers={"X-KEY": api_key, "Content-Type": "application/json"}, timeout=timeout,
        )
    except httpx.HTTPError as e:
        # The request may or may not have reached Prospeo -- keep the reservation (safe direction).
        raise ProspeoError(f"search request failed: {e}", code="NETWORK") from e

    try:
        body = response.json()
    except ValueError:
        body = {}
    code = body.get("error_code") or (body.get("message") if body.get("error") else None)
    if response.status_code == 200 and not body.get("error"):
        if body.get("free"):
            settle_spend(db, ledger_id, 0.0)  # repeat of a request made in the last 30 days
        return {"results": body.get("results") or [], "pagination": body.get("pagination") or {}}

    settle_spend(db, ledger_id, 0.0)  # every error, NO_RESULTS included, is not billed
    if code == "NO_RESULTS":
        return {"results": [], "pagination": {}}
    raise ProspeoError(f"search failed ({response.status_code}): {code or response.text[:300]}", code=code)
