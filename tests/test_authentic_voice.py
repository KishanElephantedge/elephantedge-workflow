"""Tests for app/gtm_os/content/authentic_voice.py's real sync-error surfacing bug.

REAL BUG, found live 2026-10-14: get_partner_recent_posts() called sync_own_linkedin_posts() and
threw away its return value entirely -- a genuine Apify failure (bad actor response, malformed
profile URL, rate limit) fell straight through to reading whatever's already on file (nothing, on
a first sync), producing the misleading "found no posts yet" message for every partner tried
(Majji, Nora, Angela all hit this live), masking whatever the real sync error actually was.
"""
import pytest

from app.db.models import OwnLinkedinPost, Parameter
from app.gtm_os.content import authentic_voice as av


@pytest.fixture
def db(db_factory):
    return db_factory([Parameter, OwnLinkedinPost])


TENANT = 16


def _set_profile_url(db):
    db.add(Parameter(tenant_id=TENANT, key="own_linkedin_profile_url", value="https://www.linkedin.com/in/someone/"))
    db.commit()


def test_a_real_apify_sync_error_is_surfaced_not_silently_swallowed(db, monkeypatch):
    _set_profile_url(db)
    monkeypatch.setattr(av, "check_apify_budget", lambda *a, **k: {"status": av.STATUS_ALLOWED})
    monkeypatch.setattr(av, "sync_own_linkedin_posts",
                        lambda *a, **k: {"status": "apify_error", "new_posts": 0, "error": "actor timed out"})

    posts, sync_error = av.get_partner_recent_posts(db, TENANT)

    assert posts == []
    assert sync_error == "actor timed out"


def test_generate_authentic_voice_opportunity_reports_the_real_sync_error(db, monkeypatch):
    _set_profile_url(db)
    monkeypatch.setattr(av, "check_apify_budget", lambda *a, **k: {"status": av.STATUS_ALLOWED})
    monkeypatch.setattr(av, "sync_own_linkedin_posts",
                        lambda *a, **k: {"status": "apify_error", "new_posts": 0, "error": "actor timed out"})

    result = av.generate_authentic_voice_opportunity(db, TENANT)

    assert result["status"] == "apify_error"
    assert "actor timed out" in result["reason"]


def test_a_clean_sync_with_genuinely_zero_posts_still_reports_insufficient_evidence(db, monkeypatch):
    _set_profile_url(db)
    monkeypatch.setattr(av, "check_apify_budget", lambda *a, **k: {"status": av.STATUS_ALLOWED})
    monkeypatch.setattr(av, "sync_own_linkedin_posts", lambda *a, **k: {"status": "ok", "new_posts": 0, "error": None})

    result = av.generate_authentic_voice_opportunity(db, TENANT)

    assert result["status"] == "insufficient_evidence"


def test_budget_is_checked_against_the_billing_tenant_not_the_partners_own(db, monkeypatch):
    """REAL BUG, found live 2026-10-14 (Nora Volger): check_apify_budget was called with the
    PARTNER's own tenant_id -- a partner tenant has no control_config row of its own, so the
    default (apify budget both None) made every single call silently BLOCKED, degrading to
    "found no posts yet" even though she had real posts (confirmed via a direct manual Apify run
    and a raw curl against our exact payload, both returning real data). Every other partner-spend
    path in this codebase already checks ELEPHANT_EDGE_TENANT_ID specifically."""
    _set_profile_url(db)
    seen_tenant_ids = []

    def fake_check_budget(db_, tenant_id, cost, operation=None):
        seen_tenant_ids.append(tenant_id)
        return {"status": av.STATUS_ALLOWED}

    monkeypatch.setattr(av, "check_apify_budget", fake_check_budget)
    monkeypatch.setattr(av, "sync_own_linkedin_posts", lambda *a, **k: {"status": "ok", "new_posts": 0, "error": None})

    av.get_partner_recent_posts(db, TENANT)

    assert seen_tenant_ids == [av.ELEPHANT_EDGE_TENANT_ID]
    assert av.ELEPHANT_EDGE_TENANT_ID != TENANT
