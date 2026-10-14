"""Test for app/gtm_os/intelligence/sensing.py's _search_google_with_fallback Apify-fallback
budget check.

REAL BUG, found live 2026-10-14, same class as authentic_voice.py's own fix: check_apify_budget
was called with the PARTNER's own tenant_id for the Apify fallback path (triggered whenever
Google Custom Search's free daily quota is exhausted). A partner tenant has no control_config row
of its own, so this silently blocked every fallback call for every partner tenant, indistinguishable
from Google CSE and Apify both genuinely having zero results.
"""
import pytest

import app.apify_budget_guard as apify_budget_guard
from app.db.models import Credential, Parameter
from app.gtm_os.intelligence import sensing

TENANT = 16  # nora


@pytest.fixture
def db(db_factory):
    return db_factory([Credential, Parameter])


def test_apify_fallback_budget_is_checked_against_the_billing_tenant(db, monkeypatch):
    seen_tenant_ids = []

    def fake_search_google_custom(db_, tenant_id, query, num=10):
        raise sensing.GoogleCseError("free daily quota reached")

    def fake_check_budget(db_, tenant_id, cost, operation=None):
        seen_tenant_ids.append(tenant_id)
        return {"status": "blocked_budget_unknown"}  # doesn't matter, just capturing tenant_id

    monkeypatch.setattr(sensing, "search_google_custom", fake_search_google_custom)
    monkeypatch.setattr(apify_budget_guard, "check_apify_budget", fake_check_budget)

    sensing._search_google_with_fallback(db, TENANT, "some query")

    assert seen_tenant_ids == [sensing.ELEPHANT_EDGE_TENANT_ID]
    assert sensing.ELEPHANT_EDGE_TENANT_ID != TENANT
