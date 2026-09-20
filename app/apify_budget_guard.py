"""Real Apify budget guard -- Autonomous Sensing Phase S8.

PROVIDER CAPABILITY (confirmed against Apify's own official docs, 2026-08-22, not assumed):
Apify has NO pre-call balance/remaining-credit endpoint anywhere in its real API. An actor run
only reports its own `usageTotalUsd` AFTER completing (eventually consistent within ~10s, and
Apify's own docs call this figure "informational only, not exact"). The only real, queryable
"spend so far" signal that exists BEFORE making a new call is `GET /v2/users/me/usage/monthly`
(app.apify_client.get_monthly_usage()) -- the same data shown on the account's own Billing >
Historical usage page, explicitly NOT real-time (Apify's own docs say so). This guard is built
honestly around that real constraint: it is NOT a live balance check the way
app.budget_guard.BudgetGuard is for Deepline (that provider DOES expose a real live balance) --
it is the closest real, non-fabricated substitute, and every design choice below follows from
that difference, never pretends otherwise.

DISCIPLINE, same as app.budget_guard.BudgetGuard: fail SAFE, not fail silent/open. If the real
Apify usage data cannot be obtained or parsed, this returns `provider_unavailable` -- it never
falls back to a locally-accumulated estimate, and never treats missing/None config as unlimited.

Deliberately narrower than a full "risk score" (explicitly out of scope) -- four states only:
    allowed              -- real usage data obtained, estimated cost fits within configured budget(s)
    blocked_budget        -- real usage data obtained, but budget(s) would be exceeded
    blocked_budget_unknown -- no real budget number is configured at all (None is not unlimited)
    provider_unavailable  -- Apify's usage API could not be reached/parsed; fails closed"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.apify_client import ApifyError
from app.apify_client import _get_api_key as _get_apify_api_key
from app.apify_client import get_monthly_usage
from app.gtm_os.orchestration.control import get_control_config

STATUS_ALLOWED = "allowed"
STATUS_BLOCKED_BUDGET = "blocked_budget"
STATUS_BLOCKED_BUDGET_UNKNOWN = "blocked_budget_unknown"
STATUS_PROVIDER_UNAVAILABLE = "provider_unavailable"


def _today_spend_usd(usage_data: dict, now: datetime | None = None) -> float | None:
    """Real per-day spend for today (UTC), from dailyServiceUsages[].totalUsageCreditsUsd --
    exact field names confirmed against Apify's own documented example response. Returns 0.0 if
    the array is present but has no entry for today (a real, valid "nothing spent yet today"
    state, not a parsing failure) -- returns None only if the array itself is missing/malformed,
    which IS treated as a real failure by the caller (fail closed, never assume zero blindly)."""
    daily = usage_data.get("dailyServiceUsages")
    if not isinstance(daily, list):
        return None
    now = now or datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    for entry in daily:
        date_str = (entry or {}).get("date") or ""
        if date_str.startswith(today_str):
            total = entry.get("totalUsageCreditsUsd")
            return float(total) if isinstance(total, (int, float)) else None
    return 0.0


def _monthly_spend_usd(usage_data: dict) -> float | None:
    """Real month-to-date spend, from totalUsageCreditsUsdAfterVolumeDiscount -- the actual
    billed-after-discount figure, per Apify's own documented response shape."""
    total = usage_data.get("totalUsageCreditsUsdAfterVolumeDiscount")
    return float(total) if isinstance(total, (int, float)) else None


def check_apify_budget(db: Session, tenant_id: int, estimated_cost_usd: float,
                       operation: str | None = None, entity_key: str | None = None) -> dict:
    """Reserve-and-check. Returns {"status": ..., "reason": "..."}. Called immediately before any
    Apify-backed provider call. Never raises -- a real failure here must block the caller, not
    crash it.

    RESERVES ON ALLOW (2026-09-19). Apify's `dailyServiceUsages` is real but is an aggregated
    BILLING figure and it lags. Inside one sweep, many calls in a row therefore read the SAME
    stale "spent today" value, each concludes it has room, and the daily budget is passed several
    times over before the provider's number catches up. The cap was not being ignored -- it was
    measured against a number that could not move fast enough to enforce it, which is a large
    part of why spend was both high and unpredictable.

    So an allowed call is written to the local ledger here, and the effective spend is
    max(provider, ledger): the provider stays authoritative for settled spend, while the ledger
    covers the window between committing money and the provider admitting it. The very next
    check in the same sweep then sees this call.

    Reserving here rather than at the call sites is deliberate -- this function is already called
    before every guarded paid Apify call and already receives the estimate, so no call site has
    to remember to record anything. "Someone added a call site and nobody wrapped it" is exactly
    how the unguarded paid paths in this codebase got that way.

    A reservation can over-count if the call is then not made. That is the safe direction: erring
    high costs a little unused allowance, erring low produced a $10 day on a $5 account."""
    from app.spend_ledger import PROVIDER_APIFY, effective_spend_today, record_spend

    config = get_control_config(db, tenant_id)
    apify_config = config.get("apify") or {}
    daily_budget = apify_config.get("daily_budget_usd")
    monthly_budget = apify_config.get("monthly_budget_usd")

    if daily_budget is None and monthly_budget is None:
        return {
            "status": STATUS_BLOCKED_BUDGET_UNKNOWN,
            "reason": "no Apify budget configured (apify.daily_budget_usd and apify.monthly_budget_usd are both unconfigured) -- None is never treated as unlimited",
        }

    try:
        api_key = _get_apify_api_key(db, tenant_id)
        usage_data = get_monthly_usage(api_key)
    except ApifyError as e:
        return {"status": STATUS_PROVIDER_UNAVAILABLE, "reason": f"could not fetch real Apify usage data: {e}"}
    except Exception as e:  # noqa: BLE001 -- any unexpected failure here must fail closed, not crash the caller
        return {"status": STATUS_PROVIDER_UNAVAILABLE, "reason": f"unexpected error fetching Apify usage data: {e}"}

    if daily_budget is not None:
        provider_today = _today_spend_usd(usage_data)
        if provider_today is None:
            return {"status": STATUS_PROVIDER_UNAVAILABLE, "reason": "Apify usage response did not include a parseable daily breakdown for today"}
        # max(provider, ledger) -- see this function's docstring. The ledger is what makes a
        # burst of calls inside one sweep visible before Apify's billing aggregation catches up.
        today_spent = effective_spend_today(db, tenant_id, PROVIDER_APIFY, provider_today)
        if today_spent + estimated_cost_usd > daily_budget:
            return {
                "status": STATUS_BLOCKED_BUDGET,
                "reason": (
                    f"today's Apify spend ${today_spent:.4f} (provider ${provider_today:.4f}, "
                    f"ledger-adjusted) + estimated ${estimated_cost_usd:.4f} would exceed daily "
                    f"budget ${daily_budget:.2f}"
                ),
            }

    if monthly_budget is not None:
        monthly_spent = _monthly_spend_usd(usage_data)
        if monthly_spent is None:
            return {"status": STATUS_PROVIDER_UNAVAILABLE, "reason": "Apify usage response did not include a parseable monthly total"}
        if monthly_spent + estimated_cost_usd > monthly_budget:
            return {
                "status": STATUS_BLOCKED_BUDGET,
                "reason": f"month-to-date real Apify spend ${monthly_spent:.4f} + estimated ${estimated_cost_usd:.4f} would exceed monthly budget ${monthly_budget:.2f}",
            }

    # Allowed -> the money is committed as far as this system is concerned. Recorded now, so the
    # next check in this same sweep counts it even though Apify's own figure will not for a
    # while yet. `operation` is what makes per-job cost attribution possible afterwards, instead
    # of averaging unrelated jobs into "cost per company".
    record_spend(
        db, tenant_id, PROVIDER_APIFY, estimated_cost_usd,
        operation=operation, entity_key=entity_key,
    )
    return {"status": STATUS_ALLOWED, "reason": f"estimated ${estimated_cost_usd:.4f} fits within configured Apify budget(s)"}
