"""Tests for the daily-gated engagement mining objective added to partner_daily_run.py,
2026-09-22 -- real correction after engagement mining was found firing every HOURLY tick with
no target ("i naver told that should run every hour it should run daily and with a target
set"). Pins: (1) both objectives are gated by the SAME schedule_hour_utc but tracked with
separate last_run_date markers, (2) engagement mining guarantees a PER-POST commenter floor by
calling the harvest actor once per selected post, never a single shared pool.
"""
import pytest

from app.db.models import Batch, Company, Contact, Parameter
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.orchestration.partner_daily_run import (
    DailyRunConfigError, _validate_config, run_partner_daily_tick_for_tenant, set_daily_run_config,
)
from datetime import datetime, timezone

TENANT = 15  # majji


@pytest.fixture
def db(db_factory):
    return db_factory([Parameter, Batch, Company, Contact, GtmSignal])


def _base_icp(db):
    db.add(Parameter(tenant_id=TENANT, key="partner_icp", value={"title_search": ["Head of Sales"]}))
    db.commit()


def _valid_engagement_config():
    return {
        "engagement_mining_enabled": True,
        "engagement_posts_per_day": 5,
        "engagement_commenters_per_post": 3,
        "schedule_hour_utc": 9,
    }


# --- validation ------------------------------------------------------------------------------

def test_engagement_mining_enabled_requires_posts_and_commenters_per_post():
    with pytest.raises(DailyRunConfigError, match="engagement_posts_per_day and engagement_commenters_per_post"):
        _validate_config({
            "enabled": False, "schedule_hour_utc": 9, "daily_target": None, "source": None, "pages": 2,
            "engagement_mining_enabled": True, "engagement_posts_per_day": None, "engagement_commenters_per_post": None,
            "last_run_date": None, "engagement_last_run_date": None,
        })


def test_engagement_mining_enabled_requires_a_schedule_hour():
    with pytest.raises(DailyRunConfigError, match="schedule_hour_utc"):
        _validate_config({
            "enabled": False, "schedule_hour_utc": None, "daily_target": None, "source": None, "pages": 2,
            "engagement_mining_enabled": True, "engagement_posts_per_day": 5, "engagement_commenters_per_post": 3,
            "last_run_date": None, "engagement_last_run_date": None,
        })


def test_a_complete_engagement_config_validates_cleanly():
    _validate_config({
        "enabled": False, "schedule_hour_utc": 9, "daily_target": None, "source": None, "pages": 2,
        "engagement_mining_enabled": True, "engagement_posts_per_day": 5, "engagement_commenters_per_post": 3,
        "last_run_date": None, "engagement_last_run_date": None,
    })


# --- run_partner_daily_tick_for_tenant: independent gating -----------------------------------

def test_engagement_fires_independently_of_firmographic_discovery(db, monkeypatch):
    """A tenant with ONLY engagement mining configured (firmographic still off) must still fire
    engagement at its scheduled hour -- the two objectives are independent."""
    set_daily_run_config(db, TENANT, _valid_engagement_config())

    called = {"n": 0}
    monkeypatch.setattr(
        "app.gtm_os.orchestration.partner_daily_run.run_partner_engagement_mining_now",
        lambda tenant_id, posts_per_day, commenters_per_post: called.update(n=called["n"] + 1, posts=posts_per_day, commenters=commenters_per_post),
    )

    now = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
    result = run_partner_daily_tick_for_tenant(db, TENANT, now=now)

    import time
    time.sleep(0.05)  # thread start is async; the assertion below is on config state, not the call itself
    assert result["engagement"]["status"] == "started"
    assert result["firmographic"] is None


def test_engagement_does_not_fire_twice_in_the_same_day(db, monkeypatch):
    set_daily_run_config(db, TENANT, _valid_engagement_config())
    monkeypatch.setattr("app.gtm_os.orchestration.partner_daily_run.run_partner_engagement_mining_now", lambda *a, **k: None)

    now = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
    first = run_partner_daily_tick_for_tenant(db, TENANT, now=now)
    second = run_partner_daily_tick_for_tenant(db, TENANT, now=now)

    assert first["engagement"]["status"] == "started"
    assert second["engagement"]["status"] == "skipped"
    assert second["engagement"]["reason"] == "already ran today"


def test_engagement_only_fires_at_the_shared_scheduled_hour(db, monkeypatch):
    set_daily_run_config(db, TENANT, _valid_engagement_config())
    monkeypatch.setattr("app.gtm_os.orchestration.partner_daily_run.run_partner_engagement_mining_now", lambda *a, **k: None)

    wrong_hour = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
    result = run_partner_daily_tick_for_tenant(db, TENANT, now=wrong_hour)

    assert result["status"] == "skipped"
    assert "scheduled hour" in result["reason"]


def test_neither_objective_enabled_is_a_clean_skip(db):
    result = run_partner_daily_tick_for_tenant(db, TENANT, now=datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc))
    assert result == {"status": "skipped", "reason": "daily run disabled for this tenant"}


def test_both_objectives_fire_together_on_the_same_tick(db, monkeypatch):
    _base_icp(db)
    config = {
        **_valid_engagement_config(),
        "enabled": True, "daily_target": 5, "source": "apify",
    }
    set_daily_run_config(db, TENANT, config)

    monkeypatch.setattr("app.gtm_os.orchestration.partner_daily_run.run_partner_discovery_now", lambda *a, **k: None)
    monkeypatch.setattr("app.gtm_os.orchestration.partner_daily_run.run_partner_engagement_mining_now", lambda *a, **k: None)

    result = run_partner_daily_tick_for_tenant(db, TENANT, now=datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc))

    assert result["firmographic"]["status"] == "started"
    assert result["engagement"]["status"] == "started"
    assert result["status"] == "started"


# --- run_partner_engagement_mining_now: per-post floor ----------------------------------------

def test_harvest_is_called_once_per_selected_post_not_once_for_all(db, monkeypatch):
    """THE POINT of this whole fix. 5 selected posts, commenters_per_post=3 -- must call the
    harvest actor 5 SEPARATE times (each capped at 3), never once with maxResults=15 shared
    across all 5 (which lets one popular post starve the rest)."""
    from app.gtm_os.orchestration.partner_daily_run import run_partner_engagement_mining_now

    monkeypatch.setattr("app.db.session.SessionLocal", lambda: db)
    monkeypatch.setattr(
        "app.gtm_os.intelligence.sensing.sense_linkedin_post_search", lambda d, t: [],
    )
    monkeypatch.setattr(
        "app.gtm_os.intelligence.engagement_intent.select_relevant_post_urls",
        lambda signals, limit: [f"https://x/{i}" for i in range(5)],
    )
    monkeypatch.setattr(
        "app.apify_budget_guard.check_apify_budget",
        lambda d, tid, est, operation=None: {"status": "allowed", "reason": "ok"},
    )
    calls = []
    monkeypatch.setattr(
        "app.gtm_os.intelligence.sensing.sense_linkedin_post_engagement",
        lambda d, t, post_urls, max_results=None, budget_tenant_id=None: calls.append((post_urls, max_results)) or [],
    )

    run_partner_engagement_mining_now(TENANT, posts_per_day=5, commenters_per_post=3)

    assert len(calls) == 5, "must call the harvest actor once per post, not once for all posts"
    for post_urls, max_results in calls:
        assert len(post_urls) == 1, "each call must target exactly ONE post -- a per-post floor, not a shared pool"
        assert max_results == 3


def test_stops_early_when_budget_is_exhausted_never_raises(db, monkeypatch):
    from app.gtm_os.orchestration.partner_daily_run import run_partner_engagement_mining_now

    monkeypatch.setattr("app.db.session.SessionLocal", lambda: db)
    monkeypatch.setattr("app.gtm_os.intelligence.sensing.sense_linkedin_post_search", lambda d, t: [])
    monkeypatch.setattr(
        "app.gtm_os.intelligence.engagement_intent.select_relevant_post_urls",
        lambda signals, limit: [f"https://x/{i}" for i in range(5)],
    )
    seen = {"n": 0}

    def _budget(d, tid, est, operation=None):
        seen["n"] += 1
        return {"status": "allowed", "reason": "ok"} if seen["n"] <= 2 else {"status": "blocked", "reason": "daily cap reached"}

    monkeypatch.setattr("app.apify_budget_guard.check_apify_budget", _budget)
    calls = []
    monkeypatch.setattr(
        "app.gtm_os.intelligence.sensing.sense_linkedin_post_engagement",
        lambda d, t, post_urls, max_results=None, budget_tenant_id=None: calls.append(post_urls) or [],
    )

    run_partner_engagement_mining_now(TENANT, posts_per_day=5, commenters_per_post=3)

    assert len(calls) == 2, "must stop harvesting the moment budget is exhausted, not raise or skip silently past it"
