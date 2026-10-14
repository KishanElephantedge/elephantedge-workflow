"""Tests for app/gtm_os/content/competitors.py's new self-discovery (2026-10-14).

REAL GAP, found via a direct question from the user: "why should competitors be something a
human has to type in, shouldn't the system find its own competitors from the partner's own
context?" -- competitors.py had no derivation at all, unlike topics.py's own
derive_topics_from_content_business_context. Can't ground purely in the partner's own positioning
text the way topics can (a positioning paragraph essentially never names real competitor
companies), so this derives from a real Google search instead, then only keeps an LLM-named
competitor whose domain is ACTUALLY PRESENT in those real results.
"""
import pytest

import app.llm_client as llm_client
from app.db.models import Parameter
from app.gtm_os.content import competitors as comp

TENANT = 16


@pytest.fixture
def db(db_factory):
    return db_factory([Parameter])


def _set_business_context(db):
    db.add(Parameter(tenant_id=TENANT, key="partner_content_context", value={
        "business_name": "Uptake GTM",
        "positioning": "I help life-science technology companies commercialize innovations.",
        "audience": "B2B life-science and health technology companies.",
    }))
    db.commit()


REAL_RESULTS = [
    {"url": "https://www.realrival.com/about", "title": "RealRival GTM Consulting", "description": "Commercial narrative consulting for life-science companies"},
    {"url": "https://randomblog.example/best-gtm-firms", "title": "10 Best GTM Firms 2026", "description": "A listicle, not a company's own site"},
]


def test_derive_competitors_only_keeps_a_company_actually_present_in_real_search_results(db, monkeypatch):
    import app.gtm_os.intelligence.sensing as sensing

    monkeypatch.setattr(sensing, "_search_google_with_fallback", lambda *a, **k: REAL_RESULTS)
    monkeypatch.setattr(llm_client, "generate_json", lambda *a, **k: {"competitors": [
        {"name": "RealRival", "domain": "realrival.com"},
        {"name": "Invented Co", "domain": "totallymadeup.com"},  # not in REAL_RESULTS -- must be dropped
    ]})

    from app.gtm_os.content.content_business_context import get_content_business_context
    business_context = get_content_business_context(db, TENANT)

    derived = comp.derive_competitors_from_content_business_context(db, TENANT, business_context)

    assert derived == [{"name": "RealRival", "domain": "realrival.com", "enabled": True}]


def test_get_content_competitors_derives_once_then_caches(db, monkeypatch):
    import app.gtm_os.intelligence.sensing as sensing

    _set_business_context(db)
    monkeypatch.setattr(sensing, "_search_google_with_fallback", lambda *a, **k: REAL_RESULTS)
    calls = {"n": 0}

    def fake_llm(*a, **k):
        calls["n"] += 1
        return {"competitors": [{"name": "RealRival", "domain": "realrival.com"}]}

    monkeypatch.setattr(llm_client, "generate_json", fake_llm)

    first = comp.get_content_competitors(db, TENANT)
    second = comp.get_content_competitors(db, TENANT)

    assert first == [{"name": "RealRival", "domain": "realrival.com", "enabled": True}]
    assert second == first
    assert calls["n"] == 1  # second call hit the saved Parameter, never re-derived


def test_no_business_context_returns_an_honest_empty_list_not_elephant_edges_own(db):
    assert comp.get_content_competitors(db, TENANT) == []


def test_elephant_edge_itself_still_gets_its_own_default_list(db):
    assert comp.get_content_competitors(db, comp.ELEPHANT_EDGE_TENANT_ID) == comp.DEFAULT_COMPETITORS


def test_derive_competitors_falls_back_to_industry_search_when_name_search_finds_nothing(db, monkeypatch):
    """REAL GAP, found live 2026-10-15: a name-based search ("X competitors alternatives")
    returns nothing for a small, solo fractional consultant -- nobody publishes comparison
    content about them. Falls back to searching by the partner's own real stated industry
    instead, never an invented one."""
    import app.gtm_os.intelligence.sensing as sensing

    db.add(Parameter(tenant_id=TENANT, key="partner_icp", value={"industries": ["life-science technology"]}))
    db.commit()

    queries_seen = []

    def fake_search(db_, tenant_id, query, num=10):
        queries_seen.append(query)
        if "Uptake GTM" in query:
            return []  # name search finds nothing
        return REAL_RESULTS  # industry search finds real peers

    monkeypatch.setattr(sensing, "_search_google_with_fallback", fake_search)
    monkeypatch.setattr(llm_client, "generate_json", lambda *a, **k: {"competitors": [
        {"name": "RealRival", "domain": "realrival.com"},
    ]})

    business_context = {"business_name": "Uptake GTM", "positioning": "x", "audience": "y"}

    derived = comp.derive_competitors_from_content_business_context(db, TENANT, business_context)

    assert len(queries_seen) == 2
    assert "Uptake GTM" in queries_seen[0]
    assert "life-science technology" in queries_seen[1]
    assert derived == [{"name": "RealRival", "domain": "realrival.com", "enabled": True}]
