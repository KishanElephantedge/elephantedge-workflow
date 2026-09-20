"""The two paid Apify paths that had NO budget check at all are now gated.

Both spent against the single shared Apify account ($5/month cap) while every other source
believed the daily cap was intact -- which is a large part of why spend was unpredictable:

  * sense_linkedin_post_search (sweep.py) -- the only paid SENSING source without a check,
    while _run_linkedin_jobs, _run_web_search_trends and _run_competitor_content all had one.
  * free_decision_maker.find_free_decision_makers -- "free" names the Jobo leadership LOOKUP,
    not the function: resolving those names calls Apify people-search and Google AI Overview.
    It runs for EVERY company before any paid-fallback gate is consulted.
"""
import pytest

from app.db.models import Batch, Company, Parameter
from app.spend_ledger import ProviderSpend

TENANT = 2


@pytest.fixture
def db(db_factory):
    return db_factory([ProviderSpend, Parameter, Company, Batch])


def _blocked_guard(monkeypatch, module):
    """Force check_apify_budget to refuse, without reaching Apify."""
    from app.apify_budget_guard import STATUS_BLOCKED_BUDGET

    monkeypatch.setattr(
        module, "check_apify_budget",
        lambda db, tid, est, operation=None, entity_key=None: {
            "status": STATUS_BLOCKED_BUDGET, "reason": "daily cap reached (test)",
        },
        raising=False,
    )


def test_post_search_sensing_is_blocked_when_the_budget_is_gone(db, monkeypatch):
    import app.apify_budget_guard as guard
    import app.gtm_os.orchestration.sweep as sweep

    _blocked_guard(monkeypatch, guard)

    called = {"n": 0}
    monkeypatch.setattr(
        sweep, "sense_linkedin_post_search",
        lambda d, t: called.__setitem__("n", called["n"] + 1),
    )
    monkeypatch.setattr(
        "app.gtm_os.intelligence.linkedin_search_config.get_linkedin_search_config",
        lambda d, t: {"max_phrases_per_cycle": 8, "posts_per_phrase": 10},
    )

    with pytest.raises(sweep.SourceBudgetBlocked):
        sweep._run_linkedin_post_search(db, TENANT)

    assert called["n"] == 0, "the paid sensing call must not happen once the budget is gone"


def test_post_search_sensing_runs_when_the_budget_allows(db, monkeypatch):
    import app.apify_budget_guard as guard
    import app.gtm_os.orchestration.sweep as sweep
    from app.apify_budget_guard import STATUS_ALLOWED

    monkeypatch.setattr(
        guard, "check_apify_budget",
        lambda db, tid, est, operation=None, entity_key=None: {"status": STATUS_ALLOWED, "reason": "ok"},
        raising=False,
    )
    monkeypatch.setattr(sweep, "sense_linkedin_post_search", lambda d, t: ["signal"])
    monkeypatch.setattr(
        "app.gtm_os.intelligence.linkedin_search_config.get_linkedin_search_config",
        lambda d, t: {"max_phrases_per_cycle": 8, "posts_per_phrase": 10},
    )

    assert sweep._run_linkedin_post_search(db, TENANT) == ["signal"]


def test_free_decision_maker_is_blocked_when_the_budget_is_gone(db, monkeypatch):
    """A block is a normal empty result -- exactly like finding nobody -- which the caller
    already handles. It must NOT half-resolve a contact and keep nothing."""
    import app.apify_budget_guard as guard
    import app.phases.free_decision_maker as fdm

    _blocked_guard(monkeypatch, guard)

    reached = {"n": 0}
    monkeypatch.setattr(
        fdm, "_jobo_leadership_candidates",
        lambda d, t, c: reached.__setitem__("n", reached["n"] + 1) or [],
    )

    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    company = Company(batch_id=batch.id, name="Acme", domain="acme.com")
    db.add(company)
    db.commit()

    assert fdm.find_free_decision_makers(db, TENANT, company) == []
    assert reached["n"] == 0, "no paid resolution work may start once the budget is gone"


def test_the_estimate_is_sized_to_what_will_actually_be_bought(db, monkeypatch):
    """The reservation must reflect the real worst case, or the ledger under-counts and the cap
    stops binding -- which is the failure this whole mechanism exists to prevent."""
    import app.apify_budget_guard as guard
    import app.phases.free_decision_maker as fdm
    from app.apify_budget_guard import STATUS_ALLOWED
    from app.apify_client import (
        GOOGLE_SEARCH_COST_PER_QUERY_USD, PEOPLE_SEARCH_COST_PER_PROFILE_USD,
        PEOPLE_SEARCH_COST_PER_START_USD,
    )

    seen = {}
    monkeypatch.setattr(
        guard, "check_apify_budget",
        lambda db, tid, est, operation=None, entity_key=None: (
            seen.update({"est": est, "operation": operation, "entity_key": entity_key})
            or {"status": STATUS_ALLOWED, "reason": "ok"}
        ),
        raising=False,
    )
    # Stub BOTH resolution layers -- this test is about the size of the reservation taken at
    # the gate, not about the waterfall behind it.
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda d, t, c: [])
    monkeypatch.setattr(fdm, "_find_via_google_search_candidates", lambda d, t, c, n: [])

    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    company = Company(batch_id=batch.id, name="Acme", domain="acme.com")
    db.add(company)
    db.commit()

    fdm.find_free_decision_makers(db, TENANT, company, max_contacts=3)

    expected = 3 * (
        PEOPLE_SEARCH_COST_PER_START_USD
        + fdm.PEOPLE_SEARCH_MAX_RESULTS * PEOPLE_SEARCH_COST_PER_PROFILE_USD
    ) + 2 * GOOGLE_SEARCH_COST_PER_QUERY_USD

    assert seen["est"] == pytest.approx(expected)
    assert seen["operation"] == "free_decision_maker"
    assert seen["entity_key"] == "acme.com", "spend must be attributable to the company that incurred it"
