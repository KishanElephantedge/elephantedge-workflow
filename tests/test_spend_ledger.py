"""Tests for the local spend ledger and the reserve-and-check budget guard.

THE BUG THESE PIN. Apify's dailyServiceUsages is real but is an aggregated BILLING figure that
lags. Inside one sweep, many calls in a row read the SAME stale "spent today" value, each
concludes it has room, and the daily cap is passed several times over before the provider's
number catches up. The cap was never ignored -- it was measured against a number that could not
move fast enough to enforce it.
"""
import pytest

from app.apify_budget_guard import (
    STATUS_ALLOWED, STATUS_BLOCKED_BUDGET, STATUS_BLOCKED_BUDGET_UNKNOWN, check_apify_budget,
)
from app.db.models import Parameter
from app.spend_ledger import (
    PROVIDER_APIFY, ProviderSpend, effective_spend_today, reconcile_drift, record_spend,
    spend_today, spend_today_by_operation,
)

TENANT = 2


@pytest.fixture
def db(db_factory):
    return db_factory([ProviderSpend, Parameter])


def test_records_and_totals_todays_spend(db):
    record_spend(db, TENANT, PROVIDER_APIFY, 0.25, operation="discovery")
    record_spend(db, TENANT, PROVIDER_APIFY, 0.10, operation="discovery")
    assert spend_today(db, TENANT, PROVIDER_APIFY) == pytest.approx(0.35)


def test_spend_is_scoped_per_provider_and_tenant(db):
    record_spend(db, TENANT, PROVIDER_APIFY, 1.0)
    record_spend(db, TENANT, "deepline", 2.0)
    record_spend(db, 12, PROVIDER_APIFY, 5.0)

    assert spend_today(db, TENANT, PROVIDER_APIFY) == pytest.approx(1.0)
    assert spend_today(db, TENANT, "deepline") == pytest.approx(2.0)
    assert spend_today(db, 12, PROVIDER_APIFY) == pytest.approx(5.0)


def test_effective_spend_takes_the_higher_of_provider_and_ledger(db):
    """The provider is authoritative for SETTLED spend; the ledger covers the lag window.
    Erring high costs a little unused allowance; erring low produced a $10 day on a $5 account."""
    record_spend(db, TENANT, PROVIDER_APIFY, 0.80)

    # Provider has not caught up yet -> the ledger governs.
    assert effective_spend_today(db, TENANT, PROVIDER_APIFY, 0.10) == pytest.approx(0.80)
    # Provider has settled higher than our estimates -> the truth governs.
    assert effective_spend_today(db, TENANT, PROVIDER_APIFY, 1.50) == pytest.approx(1.50)
    # No provider figure at all -> the ledger is all there is.
    assert effective_spend_today(db, TENANT, PROVIDER_APIFY, None) == pytest.approx(0.80)


def test_attribution_by_operation(db):
    """So cost-per-company is judged against what was bought. A real instance of this confusion:
    38% of one day's spend was a weekly content-marketing job that produced zero companies and
    zero contacts, and it was averaged into cost-per-company anyway."""
    record_spend(db, TENANT, PROVIDER_APIFY, 0.30, operation="discovery")
    record_spend(db, TENANT, PROVIDER_APIFY, 0.43, operation="content_sensing")
    record_spend(db, TENANT, PROVIDER_APIFY, 0.20, operation="discovery")

    by_op = spend_today_by_operation(db, TENANT, PROVIDER_APIFY)
    assert by_op["discovery"] == {"calls": 2, "usd": pytest.approx(0.50)}
    assert by_op["content_sensing"]["usd"] == pytest.approx(0.43)


def test_reconcile_reports_drift_without_correcting_it(db):
    """Drift is the signal that a per-unit cost constant is wrong -- a real defect to go fix,
    not a number to paper over."""
    record_spend(db, TENANT, PROVIDER_APIFY, 1.00)
    drift = reconcile_drift(db, TENANT, PROVIDER_APIFY, 1.50)

    assert drift["ledger_usd"] == pytest.approx(1.00)
    assert drift["provider_usd"] == pytest.approx(1.50)
    assert drift["drift_usd"] == pytest.approx(0.50)
    # The ledger is NOT silently rewritten.
    assert spend_today(db, TENANT, PROVIDER_APIFY) == pytest.approx(1.00)


# --- reserve-and-check ------------------------------------------------------------------------

def _configure(db, daily=None, monthly=None):
    db.add(Parameter(tenant_id=TENANT, key="gtm_os_control_config", value={
        "state": "running",
        "apify": {"daily_budget_usd": daily, "monthly_budget_usd": monthly},
    }))
    db.commit()


@pytest.fixture
def stub_apify(monkeypatch):
    """Apify reports a flat $0 all day -- the exact lag condition that let a sweep overspend."""
    import app.apify_budget_guard as guard
    monkeypatch.setattr(guard, "_get_apify_api_key", lambda db, tid: "key")
    monkeypatch.setattr(guard, "get_monthly_usage", lambda key: {
        "dailyServiceUsages": [], "totalUsageCreditsUsdAfterVolumeDiscount": 0.0,
    })
    return guard


def test_repeated_calls_accumulate_even_while_the_provider_reports_zero(db, stub_apify):
    """THE FIX. Before the ledger, all four of these read provider=$0.00 and every one was
    allowed, so a $1.00 cap authorised well over $1.00 of spend in a single sweep."""
    _configure(db, daily=1.00)

    assert check_apify_budget(db, TENANT, 0.40, operation="discovery")["status"] == STATUS_ALLOWED
    assert check_apify_budget(db, TENANT, 0.40, operation="discovery")["status"] == STATUS_ALLOWED
    # 0.80 committed; a third 0.40 would reach 1.20 > 1.00.
    third = check_apify_budget(db, TENANT, 0.40, operation="discovery")
    assert third["status"] == STATUS_BLOCKED_BUDGET
    assert "ledger-adjusted" in third["reason"]

    assert spend_today(db, TENANT, PROVIDER_APIFY) == pytest.approx(0.80)


def test_a_blocked_call_is_not_recorded_as_spend(db, stub_apify):
    """A refused call costs nothing and must not consume allowance."""
    _configure(db, daily=0.50)
    check_apify_budget(db, TENANT, 0.90, operation="discovery")
    assert spend_today(db, TENANT, PROVIDER_APIFY) == 0.0


def test_unconfigured_budget_blocks_and_records_nothing(db, stub_apify):
    """None is never treated as unlimited -- the existing discipline, preserved."""
    _configure(db, daily=None, monthly=None)
    assert check_apify_budget(db, TENANT, 0.10)["status"] == STATUS_BLOCKED_BUDGET_UNKNOWN
    assert spend_today(db, TENANT, PROVIDER_APIFY) == 0.0


def test_operation_is_attributed_through_the_guard(db, stub_apify):
    _configure(db, daily=5.00)
    check_apify_budget(db, TENANT, 0.30, operation="discovery", entity_key="acme.com")
    check_apify_budget(db, TENANT, 0.43, operation="content_sensing")

    by_op = spend_today_by_operation(db, TENANT, PROVIDER_APIFY)
    assert by_op["discovery"]["usd"] == pytest.approx(0.30)
    assert by_op["content_sensing"]["usd"] == pytest.approx(0.43)
