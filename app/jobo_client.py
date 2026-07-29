"""Jobo API client -- its own separate credit system from Deepline (X-Credits-Balance
response header, no separate balance-check call needed), so this mirrors deepline_client.py's
shape but is deliberately its own module, not a Deepline provider."""
import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

BASE_URL = "https://connect.jobo.world"
USD_PER_CREDIT = 0.001


class JoboError(Exception):
    pass


def _get_api_key(db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == "jobo_api_key")
        .first()
    )
    if not cred or not cred.value:
        raise JoboError("jobo_api_key credential is not set")
    return cred.value


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


def search_jobs(client: httpx.Client, api_key: str, queries: list[str], page: int, page_size: int) -> tuple[dict, int]:
    body = {"queries": queries, "page": page, "page_size": page_size, "include_fields": ["description"]}
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
