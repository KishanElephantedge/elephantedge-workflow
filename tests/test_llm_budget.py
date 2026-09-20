"""Tests for the durable, shared LLM call budget.

The failure this prevents: a single sweep consuming the whole 500/model/day free tier, after
which every remaining call walks the fallback list collecting 429s -- each burning seconds --
until stages blow their timeouts and the run reads as hung rather than as out of quota.

Two properties are load-bearing and neither could hold in the previous in-process dict:
survives a redeploy, and is shared between the three deployed instances.
"""
import pytest

from app.db.models import Parameter
from app.llm_budget import (
    DEFAULT_DAILY_LLM_CALL_BUDGET, LlmBudgetExceeded, LlmDailyUsage, calls_today,
    check_daily_llm_budget, get_daily_llm_call_budget, is_exhausted, mark_exhausted,
    record_call, usage_summary,
)

TENANT = 2
MODEL = "gemini-3.1-flash-lite"


def test_records_attempts_per_model(db_factory):
    db = db_factory([LlmDailyUsage, Parameter])
    for _ in range(3):
        record_call(db, TENANT, MODEL)
    record_call(db, TENANT, "gemini-3.6-flash")

    assert calls_today(db, TENANT) == 4
    assert usage_summary(db, TENANT)["by_model"][MODEL]["calls"] == 3


def test_budget_blocks_once_the_cap_is_reached(db_factory):
    db = db_factory([LlmDailyUsage, Parameter])
    db.add(Parameter(tenant_id=TENANT, key="llm_daily_call_budget", value={"calls": 3}))
    db.commit()

    for _ in range(2):
        record_call(db, TENANT, MODEL)
    check_daily_llm_budget(db, TENANT)  # still under

    record_call(db, TENANT, MODEL)
    with pytest.raises(LlmBudgetExceeded) as exc:
        check_daily_llm_budget(db, TENANT)
    assert "3" in str(exc.value)


def test_budget_is_configurable_per_tenant_with_a_safe_default(db_factory):
    db = db_factory([LlmDailyUsage, Parameter])
    assert get_daily_llm_call_budget(db, TENANT) == DEFAULT_DAILY_LLM_CALL_BUDGET

    db.add(Parameter(tenant_id=TENANT, key="llm_daily_call_budget", value={"calls": 40}))
    db.commit()
    assert get_daily_llm_call_budget(db, TENANT) == 40


def test_default_budget_leaves_headroom_below_the_free_tier(db_factory):
    """250 of 500/model/day -- a cap that consumed the whole allowance would leave a run no
    margin to finish, and would ignore the second deployed instance sharing the same project."""
    assert DEFAULT_DAILY_LLM_CALL_BUDGET < 500


def test_exhaustion_is_remembered_across_processes(db_factory):
    """THE REDEPLOY FIX. llm_client._EXHAUSTED is a plain dict cleared on every restart; with
    ~40 redeploys in 3 days each deploy erased which models were dead, and the next run spent
    its allowance rediscovering them."""
    db = db_factory([LlmDailyUsage, Parameter])
    assert is_exhausted(db, TENANT, MODEL) is False

    mark_exhausted(db, TENANT, MODEL)
    assert is_exhausted(db, TENANT, MODEL) is True
    # A different model is unaffected -- Gemini's daily quota is per-model, so an exhausted
    # model is not an exhausted provider.
    assert is_exhausted(db, TENANT, "gemini-3.6-flash") is False


def test_exhaustion_does_not_leak_between_tenants(db_factory):
    db = db_factory([LlmDailyUsage, Parameter])
    mark_exhausted(db, TENANT, MODEL)
    assert is_exhausted(db, 12, MODEL) is False


def test_counts_are_scoped_per_tenant(db_factory):
    db = db_factory([LlmDailyUsage, Parameter])
    record_call(db, TENANT, MODEL)
    record_call(db, 12, MODEL)
    assert calls_today(db, TENANT) == 1
    assert calls_today(db, 12) == 1


def test_yesterdays_exhaustion_does_not_suppress_today(db_factory):
    """Gemini's free quota resets at UTC midnight. The date is part of the row key, so this
    expires naturally rather than needing a cleanup job."""
    from datetime import date, timedelta

    db = db_factory([LlmDailyUsage, Parameter])
    db.add(LlmDailyUsage(
        tenant_id=TENANT, usage_date=date.today() - timedelta(days=1), model=MODEL,
        call_count=500, quota_exhausted_at=None,
    ))
    db.commit()

    assert is_exhausted(db, TENANT, MODEL) is False
    assert calls_today(db, TENANT) == 0, "yesterday's spend must not count against today"


def test_budget_check_fails_open_if_accounting_is_broken(db_factory, monkeypatch):
    """A bookkeeping outage must not become a total outage -- the provider's own quota is still
    the hard wall behind this."""
    db = db_factory([LlmDailyUsage, Parameter])

    import app.llm_budget as budget

    def _boom(*a, **k):
        raise RuntimeError("table missing")

    monkeypatch.setattr(budget, "calls_today", _boom)
    check_daily_llm_budget(db, TENANT)  # must not raise
