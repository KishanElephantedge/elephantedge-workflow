"""Tests for the "our key, our cost, not the partner's" budget fix.

THE GAP THIS PINS. A partner tenant (majji, id 15) has no gtm_os_control_config of its own --
checking Apify budget against ITS tenant_id would read daily_budget_usd=None,
monthly_budget_usd=None, which check_apify_budget correctly treats as blocked_budget_unknown
("None is never unlimited"). That fails CLOSED, not open -- never an accidental spend -- but it
also means engagement mining could never run at all for a partner tenant, silently, with no
error a human would notice. Every other partner-facing paid call in this codebase (
run_apify_discovery's budget_tenant_id, partner_pipeline.py's enrichment_tenant_id) already
solved this the same way: check against Elephant Edge's own budget, since Elephant Edge's Apify
account is what actually funds every partner run.
"""
import pytest

from app.db.models import Batch, Company, Parameter
from app.gtm_os.intelligence.signal import GtmSignal

PARTNER_TENANT = 15  # majji
ELEPHANT_EDGE = 2


@pytest.fixture
def db(db_factory):
    return db_factory([Parameter, GtmSignal, Company, Batch])


def _search_config(engagement_enabled=True):
    return {
        "phrases": ["looking for a fractional sales leader"], "max_phrases_per_cycle": 8,
        "posts_per_phrase": 10, "date_posted_filter": "past-week",
        "min_hours_between_searches": 6, "phrase_last_searched_at": {},
        "engagement_mining_enabled": engagement_enabled,
    }


def test_with_no_budget_override_a_partner_tenant_is_blocked_forever(db, monkeypatch):
    """THE BUG, reproduced. Confirms the failure mode this fix closes -- without
    budget_tenant_id, majji's own unconfigured control config blocks every call."""
    import app.gtm_os.orchestration.sweep as sweep

    monkeypatch.setattr(
        "app.gtm_os.intelligence.linkedin_search_config.get_linkedin_search_config",
        lambda d, t: _search_config(),
    )
    monkeypatch.setattr(sweep, "sense_linkedin_post_search", lambda d, t: [])

    with pytest.raises(sweep.SourceBudgetBlocked, match="budget"):
        sweep._run_linkedin_post_search(db, PARTNER_TENANT)  # no budget_tenant_id passed


def test_budget_tenant_id_checks_elephant_edges_budget_instead(db, monkeypatch):
    """THE FIX. Elephant Edge HAS a real configured budget; passing budget_tenant_id lets a
    partner tenant's call succeed against it."""
    import app.gtm_os.orchestration.sweep as sweep

    db.add(Parameter(tenant_id=ELEPHANT_EDGE, key="gtm_os_control_config", value={
        "state": "running", "apify": {"daily_budget_usd": 5.0, "monthly_budget_usd": 50.0},
    }))
    db.commit()

    monkeypatch.setattr(
        "app.gtm_os.intelligence.linkedin_search_config.get_linkedin_search_config",
        lambda d, t: _search_config(engagement_enabled=False),  # isolate to the post-search check
    )
    monkeypatch.setattr(sweep, "sense_linkedin_post_search", lambda d, t: [])
    import app.apify_budget_guard as guard
    monkeypatch.setattr(guard, "_get_apify_api_key", lambda db, tid: "key")
    monkeypatch.setattr(guard, "get_monthly_usage", lambda key: {
        "dailyServiceUsages": [], "totalUsageCreditsUsdAfterVolumeDiscount": 0.0,
    })

    # Must not raise -- budget_tenant_id=ELEPHANT_EDGE routes the check to a tenant with a real,
    # affordable budget even though PARTNER_TENANT itself has none configured.
    result = sweep._run_linkedin_post_search(db, PARTNER_TENANT, budget_tenant_id=ELEPHANT_EDGE)
    assert result == []


def test_signals_are_still_written_to_the_partner_tenant_not_elephant_edge(db, monkeypatch):
    """The budget is Elephant Edge's; the DATA belongs to the partner. This must never leak a
    partner's signals into Elephant Edge's own tenant, or vice versa."""
    import app.gtm_os.orchestration.sweep as sweep

    db.add(Parameter(tenant_id=ELEPHANT_EDGE, key="gtm_os_control_config", value={
        "state": "running", "apify": {"daily_budget_usd": 5.0, "monthly_budget_usd": 50.0},
    }))
    db.commit()

    monkeypatch.setattr(
        "app.gtm_os.intelligence.linkedin_search_config.get_linkedin_search_config",
        lambda d, t: _search_config(engagement_enabled=False),
    )

    written_tenant = {}

    def _fake_post_search(d, t):
        written_tenant["tenant_id"] = t
        return []

    monkeypatch.setattr(sweep, "sense_linkedin_post_search", _fake_post_search)
    import app.apify_budget_guard as guard
    monkeypatch.setattr(guard, "_get_apify_api_key", lambda db, tid: "key")
    monkeypatch.setattr(guard, "get_monthly_usage", lambda key: {
        "dailyServiceUsages": [], "totalUsageCreditsUsdAfterVolumeDiscount": 0.0,
    })

    sweep._run_linkedin_post_search(db, PARTNER_TENANT, budget_tenant_id=ELEPHANT_EDGE)
    assert written_tenant["tenant_id"] == PARTNER_TENANT


def test_elephant_edges_own_sweep_call_is_unaffected(db, monkeypatch):
    """No budget_tenant_id passed (the existing V2 sweep caller) -> tenant_id is used for both
    the check and the data, exactly as before this fix."""
    import app.gtm_os.orchestration.sweep as sweep

    db.add(Parameter(tenant_id=ELEPHANT_EDGE, key="gtm_os_control_config", value={
        "state": "running", "apify": {"daily_budget_usd": 5.0, "monthly_budget_usd": 50.0},
    }))
    db.commit()

    monkeypatch.setattr(
        "app.gtm_os.intelligence.linkedin_search_config.get_linkedin_search_config",
        lambda d, t: _search_config(engagement_enabled=False),
    )
    monkeypatch.setattr(sweep, "sense_linkedin_post_search", lambda d, t: [])
    import app.apify_budget_guard as guard
    monkeypatch.setattr(guard, "_get_apify_api_key", lambda db, tid: "key")
    monkeypatch.setattr(guard, "get_monthly_usage", lambda key: {
        "dailyServiceUsages": [], "totalUsageCreditsUsdAfterVolumeDiscount": 0.0,
    })

    assert sweep._run_linkedin_post_search(db, ELEPHANT_EDGE) == []
