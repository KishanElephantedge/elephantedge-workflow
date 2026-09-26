"""Pins the one combined spend budget (app/spend_ledger.reserve_spend) that every paid provider
call inside a governed run goes through. No test here makes a real provider call."""
import copy

import pytest

import app.apify_budget_guard as apify_budget_guard
import app.deepline_client as deepline_client
from app.db.models import Parameter
from app.gtm_os.orchestration.control import DEFAULT_GTM_OS_CONTROL_CONFIG, set_control_config
from app.llm_client import _reserve_claude
from app.spend_ledger import (
    PROVIDER_APIFY, PROVIDER_DEEPLINE, ProviderSpend, SpendBlocked, reserve_spend, settle_spend,
    spend_scope, total_spend_today,
)

TENANT = 2


def _configure(db, daily_cap=None, run_cap=None, apify_daily=None):
    config = copy.deepcopy(DEFAULT_GTM_OS_CONTROL_CONFIG)
    config["spend"] = {"daily_cap_usd": daily_cap, "run_cap_usd": run_cap}
    config["apify"] = {"daily_budget_usd": apify_daily, "monthly_budget_usd": None}
    set_control_config(db, TENANT, config)


@pytest.fixture
def db(db_factory):
    return db_factory([Parameter, ProviderSpend])


def test_no_configured_cap_means_no_spend(db):
    _configure(db, daily_cap=None)
    with pytest.raises(SpendBlocked):
        reserve_spend(db, TENANT, PROVIDER_DEEPLINE, 0.01, operation="t")


def test_cap_is_combined_across_providers(db):
    _configure(db, daily_cap=1.0)
    reserve_spend(db, TENANT, PROVIDER_APIFY, 0.60, operation="t")
    reserve_spend(db, TENANT, PROVIDER_DEEPLINE, 0.30, operation="t")
    assert total_spend_today(db, TENANT) == pytest.approx(0.90)
    with pytest.raises(SpendBlocked):
        reserve_spend(db, TENANT, PROVIDER_DEEPLINE, 0.20, operation="t")  # 1.10 > 1.00


def test_run_cap_blocks_inside_scope(db):
    _configure(db, daily_cap=1.0)
    with spend_scope(db, TENANT, "play_a", run_cap_usd=0.50) as scope:
        reserve_spend(db, TENANT, PROVIDER_DEEPLINE, 0.40, operation="t")
        with pytest.raises(SpendBlocked):
            reserve_spend(db, TENANT, PROVIDER_DEEPLINE, 0.20, operation="t")  # run 0.60 > 0.50
        assert scope.spent_usd == pytest.approx(0.40)


def test_settling_a_miss_frees_the_allowance(db):
    _configure(db, daily_cap=0.10)
    with spend_scope(db, TENANT, "play_a", run_cap_usd=0.10) as scope:
        row_id = reserve_spend(db, TENANT, PROVIDER_DEEPLINE, 0.08, operation="t")
        settle_spend(db, row_id, 0.0)  # pay-per-result provider found nothing
        assert scope.spent_usd == pytest.approx(0.0)
        reserve_spend(db, TENANT, PROVIDER_DEEPLINE, 0.08, operation="t")  # fits again
    assert total_spend_today(db, TENANT) == pytest.approx(0.08)


def test_deepline_reserves_inside_scope_and_not_outside(db, monkeypatch):
    _configure(db, daily_cap=1.0)
    monkeypatch.setattr(deepline_client, "is_deepline_enabled", lambda: True)
    monkeypatch.setattr(deepline_client, "_call_deepline_cli", lambda tool_id, payload: {"ok": True})

    deepline_client.execute_tool("prospeo_enrich_person", {"linkedin_url": "x"})
    assert total_spend_today(db, TENANT) == 0.0  # outside a scope: legacy, unrecorded

    with spend_scope(db, TENANT, "play_a"):
        response = deepline_client.execute_tool("prospeo_enrich_person", {"linkedin_url": "x"})
    assert response["_spend_ledger_id"] is not None
    assert total_spend_today(db, TENANT) == pytest.approx(0.055)


def test_deepline_refuses_unpriced_tool_and_over_cap(db, monkeypatch):
    _configure(db, daily_cap=0.10)
    monkeypatch.setattr(deepline_client, "is_deepline_enabled", lambda: True)
    called = []
    monkeypatch.setattr(deepline_client, "_call_deepline_cli", lambda t, p: called.append(t) or {})

    with spend_scope(db, TENANT, "play_a"):
        with pytest.raises(deepline_client.DeeplineSpendBlocked):
            deepline_client.execute_tool("some_unpriced_tool", {})
        deepline_client.execute_tool("prospeo_enrich_person", {})
        with pytest.raises(deepline_client.DeeplineSpendBlocked):
            deepline_client.execute_tool("prospeo_enrich_person", {})  # 0.11 > 0.10
    assert called == ["prospeo_enrich_person"]  # refused calls never reached the provider


def test_failed_deepline_call_releases_its_reservation(db, monkeypatch):
    _configure(db, daily_cap=1.0)
    monkeypatch.setattr(deepline_client, "is_deepline_enabled", lambda: True)

    def boom(tool_id, payload):
        raise deepline_client.DeeplineError("provider said no")

    monkeypatch.setattr(deepline_client, "_call_deepline_cli", boom)
    with spend_scope(db, TENANT, "play_a"):
        with pytest.raises(deepline_client.DeeplineError):
            deepline_client.execute_tool("prospeo_enrich_person", {})
    assert total_spend_today(db, TENANT) == 0.0


def test_apify_counts_against_the_combined_cap(db, monkeypatch):
    _configure(db, daily_cap=0.10, apify_daily=5.0)
    monkeypatch.setattr(apify_budget_guard, "_get_apify_api_key", lambda db, tenant_id: "k")
    monkeypatch.setattr(apify_budget_guard, "get_monthly_usage", lambda key: {"dailyServiceUsages": []})

    reserve_spend(db, TENANT, PROVIDER_DEEPLINE, 0.08, operation="t")
    result = apify_budget_guard.check_apify_budget(db, TENANT, 0.05, operation="post_search")
    assert result["status"] == apify_budget_guard.STATUS_BLOCKED_BUDGET  # 0.13 > 0.10 combined
    ok = apify_budget_guard.check_apify_budget(db, TENANT, 0.01, operation="post_search")
    assert ok["status"] == apify_budget_guard.STATUS_ALLOWED


def test_claude_fallback_is_reserved_inside_scope(db):
    _configure(db, daily_cap=0.001)
    with spend_scope(db, TENANT, "play_a"):
        with pytest.raises(SpendBlocked):
            _reserve_claude("x" * 4000, max_tokens=2000)  # ~$0.011 > $0.001
