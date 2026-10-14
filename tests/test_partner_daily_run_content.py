"""Tests for the THIRD daily-run objective, content intelligence, added to partner_daily_run.py
2026-10-14 after a direct question surfaced the gap: "does that work, does it add topics, or
still do I have to run it" -- the real admin-route pipeline (trend/competitor sensing,
authentic-voice sync) existed and was proven live, but nothing scheduled it. Pins: (1) content
fires on its OWN schedule_hour_utc, independent of firmographic/engagement -- a tenant with no
account-discovery config at all must still be able to run content, (2) it re-checks `content` is
actually in enabled_features at run time, not just trusted from config-save time, (3) it never
fires twice the same day.
"""
import pytest

from app.db.models import Batch, Company, Contact, Parameter, Tenant
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.orchestration.partner_daily_run import (
    DailyRunConfigError, _validate_config, run_partner_daily_tick_for_tenant, set_daily_run_config,
)
from datetime import datetime, timezone

TENANT = 15  # majji


@pytest.fixture
def db(db_factory):
    return db_factory([Parameter, Batch, Company, Contact, GtmSignal, Tenant])


def _valid_content_config():
    return {"content_intelligence_enabled": True, "content_schedule_hour_utc": 14}


# --- validation ------------------------------------------------------------------------------

def test_content_intelligence_enabled_requires_a_schedule_hour():
    with pytest.raises(DailyRunConfigError, match="content_schedule_hour_utc"):
        _validate_config({
            "enabled": False, "schedule_hour_utc": None, "daily_target": None, "source": None, "pages": 2,
            "engagement_mining_enabled": False, "engagement_posts_per_day": None, "engagement_commenters_per_post": None,
            "last_run_date": None, "engagement_last_run_date": None,
            "content_intelligence_enabled": True, "content_schedule_hour_utc": None, "content_last_run_date": None,
        })


def test_a_complete_content_only_config_validates_cleanly():
    _validate_config({
        "enabled": False, "schedule_hour_utc": None, "daily_target": None, "source": None, "pages": 2,
        "engagement_mining_enabled": False, "engagement_posts_per_day": None, "engagement_commenters_per_post": None,
        "last_run_date": None, "engagement_last_run_date": None,
        "content_intelligence_enabled": True, "content_schedule_hour_utc": 14, "content_last_run_date": None,
    })


# --- run_partner_daily_tick_for_tenant: independent gating ------------------------------------

def test_content_fires_with_no_account_discovery_configured_at_all(db, monkeypatch):
    """The real case found live: Majji/Nora/Angela all had enabled=False (no account discovery
    configured) but still need content to fire on its own."""
    tenant = Tenant(id=TENANT, name="Majji", slug="partner:majji", enabled_features=["content"])
    db.merge(tenant)
    db.commit()
    set_daily_run_config(db, TENANT, _valid_content_config())

    called = {"n": 0}
    monkeypatch.setattr(
        "app.gtm_os.orchestration.partner_daily_run.run_partner_content_intelligence_now",
        lambda tenant_id: called.__setitem__("n", called["n"] + 1),
    )

    result = run_partner_daily_tick_for_tenant(db, TENANT, now=datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc))

    assert result["content"]["status"] == "started"
    assert result["firmographic"] is None
    assert result["engagement"] is None


def test_content_does_not_fire_outside_its_own_scheduled_hour(db):
    tenant = Tenant(id=TENANT, name="Majji", slug="partner:majji", enabled_features=["content"])
    db.merge(tenant)
    db.commit()
    set_daily_run_config(db, TENANT, _valid_content_config())

    result = run_partner_daily_tick_for_tenant(db, TENANT, now=datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc))

    assert result["status"] == "skipped"


def test_content_does_not_fire_twice_in_one_day(db, monkeypatch):
    tenant = Tenant(id=TENANT, name="Majji", slug="partner:majji", enabled_features=["content"])
    db.merge(tenant)
    db.commit()
    set_daily_run_config(db, TENANT, _valid_content_config())

    called = {"n": 0}
    monkeypatch.setattr(
        "app.gtm_os.orchestration.partner_daily_run.run_partner_content_intelligence_now",
        lambda tenant_id: called.__setitem__("n", called["n"] + 1),
    )

    at_1400 = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)
    first = run_partner_daily_tick_for_tenant(db, TENANT, now=at_1400)
    second = run_partner_daily_tick_for_tenant(db, TENANT, now=at_1400)

    assert first["content"]["status"] == "started"
    assert second["content"]["status"] == "skipped"
    assert second["content"]["reason"] == "already ran today"


def test_firmographic_and_content_fire_independently_at_different_hours(db, monkeypatch):
    """A tenant with BOTH configured, on different hours, must fire each only at its own hour --
    never both, never neither."""
    tenant = Tenant(id=TENANT, name="Majji", slug="partner:majji", enabled_features=["content"])
    db.merge(tenant)
    db.commit()
    db.add(Parameter(tenant_id=TENANT, key="partner_icp", value={"title_search": ["Head of Sales"]}))
    db.commit()
    set_daily_run_config(db, TENANT, {
        "enabled": True, "schedule_hour_utc": 9, "daily_target": 10, "source": "apify",
        **_valid_content_config(),
    })

    monkeypatch.setattr("app.gtm_os.orchestration.partner_daily_run.run_partner_content_intelligence_now", lambda tenant_id: None)
    monkeypatch.setattr("app.gtm_os.orchestration.partner_daily_run.run_partner_discovery_now", lambda *a, **k: None)

    at_content_hour = run_partner_daily_tick_for_tenant(db, TENANT, now=datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc))
    assert at_content_hour["content"]["status"] == "started"
    assert at_content_hour["firmographic"] is None

    at_firmographic_hour = run_partner_daily_tick_for_tenant(db, TENANT, now=datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc))
    assert at_firmographic_hour["firmographic"]["status"] == "started"


# --- run_partner_content_intelligence_now: real re-check at run time --------------------------

def test_content_runner_skips_if_feature_disabled_since_scheduling(monkeypatch):
    """A feature later disabled must stop firing, not keep running on a stale config -- the
    daily tick only checks the config's own enabled flag, so the runner itself re-checks
    enabled_features for real."""
    from app.gtm_os.orchestration import partner_daily_run as pdr

    class FakeSession:
        def close(self):
            pass

    import app.db.session as db_session
    monkeypatch.setattr(db_session, "SessionLocal", lambda: FakeSession())

    import app.gtm_os.features.config as feature_config
    monkeypatch.setattr(feature_config, "enabled_features", lambda db, tenant_id: ["accounts"])  # no "content"

    sensing_called = {"n": 0}
    import app.gtm_os.intelligence.sensing as sensing
    monkeypatch.setattr(sensing, "sense_google_news_rss", lambda *a, **k: sensing_called.__setitem__("n", sensing_called["n"] + 1))

    pdr.run_partner_content_intelligence_now(TENANT)

    assert sensing_called["n"] == 0
