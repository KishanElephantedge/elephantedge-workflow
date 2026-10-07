"""Phase 3: buy the partner's shortfall and nothing more.

Icypeas bills per REQUESTED result, so page size is the spend decision. The old code asked for a
fixed 25 regardless of whether the partner needed 25 more accounts or none at all.
"""
from datetime import datetime, timedelta

import pytest

from app.db.models import Credential, Parameter, Tenant
from app.gtm_os.features import config as feature_config
from app.gtm_os.plays.lead import GtmLead
from app.gtm_os.sourcing import quota as Q

PARTNER_A, PARTNER_B = 15, 77
PLAY = "icp_filters"


@pytest.fixture
def db(db_factory):
    # GtmLead carries FKs into the message/opportunity chain, so those tables have to exist even
    # though this module only ever counts leads.
    from app.db.models import Batch, CampaignPush, Company, Contact
    from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
    from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
    from app.gtm_os.learning.message_draft import MessageDraft
    from app.gtm_os.opportunity.opportunity import Opportunity
    from app.gtm_os.strategy.strategy import GtmStrategy

    db = db_factory([Tenant, Parameter, Credential, GtmLead, Batch, Company, Contact, CampaignPush,
                     ProblemHypothesis, DemandHypothesis, Opportunity, GtmStrategy, MessageDraft])
    db.add(Tenant(id=PARTNER_A, name="Partner A", slug="a"))
    db.add(Tenant(id=PARTNER_B, name="Partner B", slug="b"))
    db.commit()
    return db


def _deliver(db, tenant_id, n, when=None):
    for i in range(n):
        lead = GtmLead(tenant_id=tenant_id, play=PLAY, lead_key=f"company:{tenant_id}-{i}-{when}",
                       state="signal")
        if when is not None:
            lead.created_at = when
        db.add(lead)
    db.commit()


def test_each_partner_has_their_own_target(db):
    feature_config.set_config(db, PARTNER_A, "accounts", {"daily_account_target": 25})
    feature_config.set_config(db, PARTNER_B, "accounts", {"daily_account_target": 3})
    assert Q.daily_target(db, PARTNER_A) == 25
    assert Q.daily_target(db, PARTNER_B) == 3


def test_an_unconfigured_partner_falls_back_to_the_default(db):
    assert Q.daily_target(db, PARTNER_B) == Q.DEFAULT_DAILY_TARGET


def test_a_nonsense_target_falls_back_rather_than_buying_nothing_or_everything(db):
    for bad in ("", "abc", 0, -5, None):
        feature_config.set_config(db, PARTNER_A, "accounts", {"daily_account_target": bad})
        assert Q.daily_target(db, PARTNER_A) == Q.DEFAULT_DAILY_TARGET


def test_only_todays_leads_count_against_todays_target(db):
    _deliver(db, PARTNER_A, 3)
    _deliver(db, PARTNER_A, 5, when=datetime.utcnow() - timedelta(days=1))
    assert Q.delivered_today(db, PARTNER_A, PLAY) == 3


def test_one_partners_deliveries_do_not_count_against_another(db):
    _deliver(db, PARTNER_A, 4)
    assert Q.delivered_today(db, PARTNER_B, PLAY) == 0


def test_when_the_target_is_met_the_run_buys_nothing(db):
    """The cheapest outcome in the system: a run that costs $0."""
    feature_config.set_config(db, PARTNER_A, "accounts", {"daily_account_target": 5})
    _deliver(db, PARTNER_A, 5)

    plan = Q.plan(db, PARTNER_A, PLAY, provider_page_size_max=200)
    assert plan.satisfied is True
    assert plan.page_size == 0
    assert "already met" in plan.reason


def test_page_size_is_the_shortfall_plus_attrition_not_a_fixed_25(db):
    feature_config.set_config(db, PARTNER_A, "accounts", {"daily_account_target": 10})
    _deliver(db, PARTNER_A, 2)

    plan = Q.plan(db, PARTNER_A, PLAY, provider_page_size_max=200, multiplier=1.4)
    assert plan.remaining == 8
    assert plan.page_size == 12          # ceil(8 * 1.4) -- cheaper than the old fixed 25
    assert plan.satisfied is False


def test_page_size_never_exceeds_the_providers_own_maximum(db):
    feature_config.set_config(db, PARTNER_A, "accounts", {"daily_account_target": 5000})
    plan = Q.plan(db, PARTNER_A, PLAY, provider_page_size_max=200)
    assert plan.page_size == 200


def test_a_tiny_shortfall_still_buys_a_sane_minimum(db):
    feature_config.set_config(db, PARTNER_A, "accounts", {"daily_account_target": 10})
    _deliver(db, PARTNER_A, 9)
    plan = Q.plan(db, PARTNER_A, PLAY, provider_page_size_max=200)
    assert plan.remaining == 1
    assert plan.page_size == Q.MIN_PAGE_SIZE


def test_search_spends_nothing_once_the_partner_has_what_they_need(db, monkeypatch):
    """End to end: the provider is never called at all when the quota is already satisfied."""
    import app.deepline_client as dc
    from app.gtm_os.plays import icp_filters as play

    calls = []
    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: calls.append(tool) or {})

    feature_config.set_config(db, PARTNER_A, "accounts", {"daily_account_target": 2})
    _deliver(db, PARTNER_A, 2)

    result = play.search_icypeas(db, PARTNER_A, {"employee_min": 11, "employee_max": 50})

    assert calls == []
    assert result["companies"] == 0
    assert result["quota"]["remaining"] == 0
    assert "already met" in result["stopped"]


def test_a_wrong_fit_page_stops_the_run_instead_of_buying_more(db, monkeypatch):
    """Phase 4, end to end: 200 OK with rows that do not match the ICP must stop the run.

    This is the 2026-10-03 batch -- every company far outside the 11-50 headcount band. Before
    verification the run processed all of them and would have paged on for more.
    """
    import app.deepline_client as dc
    from app.gtm_os.plays import icp_filters as play

    pages = []

    def fake_cli(tool, payload):
        pages.append(tool)
        if tool == "icypeas_count_companies":
            return {"toolResponse": {"raw": {"count": 500}}}
        rows = [{"url": f"https://www.linkedin.com/company/wrong{i}", "name": f"Wrong {i}",
                 "numberOfEmployees": 400} for i in range(8)]
        return {"toolResponse": {"raw": {"leads": rows, "pagination": {"token": "next"}}}}

    monkeypatch.setattr(dc, "_call_deepline_cli", fake_cli)
    feature_config.set_config(db, PARTNER_A, "accounts", {"daily_account_target": 20})

    result = play.search_icypeas(db, PARTNER_A, {"employee_min": 11, "employee_max": 50}, pages=5)

    assert result["verification"]["match_rate"] == 0.0
    assert "quality" in (result["stopped"] or "")
    # Only ONE search page was bought, not the five it was asked for.
    assert pages.count("icypeas_find_companies") == 1
