"""Phase 5: a company bought for one partner is free for the next one whose ICP it also matches.

The elevator-pitch test is `test_a_pool_hit_reduces_what_the_next_partner_needs_to_buy` -- a
company recorded from partner A's real paid run is delivered to partner B for $0, through the
exact same per-company processing (vendor/government checks, free decision-maker attempt) a
freshly bought company would go through.
"""
from datetime import datetime, timedelta

import pytest

from app.db.models import Batch, CampaignPush, Company, Contact, Parameter, Tenant
from app.gtm_os.features import config as feature_config
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.orchestration.control import DEFAULT_GTM_OS_CONTROL_CONFIG, set_control_config
from app.gtm_os.plays.lead import GtmLead
from app.gtm_os.sourcing import pool as POOL
from app.gtm_os.sourcing.models import IcpExclusion, IcpTermResolution, ProviderTaxonomyValue
from app.gtm_os.strategy.strategy import GtmStrategy
from app.spend_ledger import ProviderSpend

PARTNER_A, PARTNER_B, BILLING = 15, 77, 2
PLAY = "icp_filters"
ICP = {"employee_min": 11, "employee_max": 50, "geographies": ["United States"]}


@pytest.fixture
def db(db_factory):
    db = db_factory([Tenant, Parameter, ProviderSpend, GtmLead, Batch, Company, Contact,
                     CampaignPush, ProblemHypothesis, DemandHypothesis, Opportunity, GtmStrategy,
                     MessageDraft, POOL.CompanyPool, POOL.PoolDelivery, IcpExclusion,
                     IcpTermResolution, ProviderTaxonomyValue])
    for tid, name in ((BILLING, "Elephant Edge"), (PARTNER_A, "A"), (PARTNER_B, "B")):
        db.add(Tenant(id=tid, name=name, slug=name.lower()))
    config = DEFAULT_GTM_OS_CONTROL_CONFIG.copy()
    config["spend"] = {"daily_cap_usd": 1.0, "run_cap_usd": 0.5}
    set_control_config(db, BILLING, config)
    db.commit()
    return db


def _real_company(**over):
    row = dict(linkedin_url="https://www.linkedin.com/company/acme", domain=None, name="Acme Co",
              industry="Professional Services", headcount=25, revenue_low_usd=None,
              revenue_high_usd=None, location="United States", country="US",
              source_provider="icypeas", source_endpoint="find-companies", cost_usd=0.175)
    row.update(over)
    return row


# ---- identity ----

def test_linkedin_url_is_the_preferred_identity():
    assert POOL.identity_key(linkedin_url="https://www.linkedin.com/company/acme/",
                             domain="acme.com", name="Acme") == "li:acme"


def test_falls_back_to_domain_then_name():
    assert POOL.identity_key(domain="acme.com", name="Acme") == "domain:acme.com"
    assert POOL.identity_key(name="Acme Co") == "name:acme co"


def test_a_shortener_domain_is_not_a_real_identity():
    # The exact Asseta/hubs.li bug this codebase already fixed once for the per-tenant table.
    assert POOL.identity_key(domain="hubs.li", name="Asseta") == "name:asseta"


def test_nothing_identifiable_returns_no_key():
    assert POOL.identity_key() is None


# ---- recording ----

def test_recording_a_company_twice_refreshes_rather_than_duplicates(db):
    POOL.record(db, **_real_company(headcount=25))
    POOL.record(db, **_real_company(headcount=31))     # grew; same company
    rows = db.query(POOL.CompanyPool).all()
    assert len(rows) == 1
    assert rows[0].headcount == 31


def test_recording_never_raises_even_with_bad_input(db):
    POOL.record(db, linkedin_url=None, domain=None, name="", industry=None, headcount=None,
               revenue_low_usd=None, revenue_high_usd=None, location=None, country=None,
               source_provider="icypeas", source_endpoint=None, cost_usd=None)
    assert db.query(POOL.CompanyPool).count() == 0


# ---- matching ----

def test_a_match_requires_positive_evidence_not_merely_absence(db):
    POOL.record(db, **_real_company(headcount=None))
    matches = POOL.find_matches(db, PARTNER_B, PLAY, ICP, limit=10)
    assert matches == []


def test_headcount_outside_the_band_does_not_match(db):
    POOL.record(db, **_real_company(headcount=900))
    assert POOL.find_matches(db, PARTNER_B, PLAY, ICP, limit=10) == []


def test_a_genuine_match_is_found(db):
    POOL.record(db, **_real_company(headcount=25))
    matches = POOL.find_matches(db, PARTNER_B, PLAY, ICP, limit=10)
    assert len(matches) == 1
    assert matches[0].row.name == "Acme Co"


def test_a_company_already_delivered_to_this_tenant_is_not_matched_again(db):
    POOL.record(db, **_real_company())
    row = db.query(POOL.CompanyPool).one()
    POOL.mark_delivered(db, PARTNER_B, row.id, PLAY)
    assert POOL.find_matches(db, PARTNER_B, PLAY, ICP, limit=10) == []


def test_the_same_company_can_be_delivered_to_a_different_tenant(db):
    """The whole point: delivered-to-A does not block delivery to B."""
    POOL.record(db, **_real_company())
    row = db.query(POOL.CompanyPool).one()
    POOL.mark_delivered(db, PARTNER_A, row.id, PLAY)
    matches = POOL.find_matches(db, PARTNER_B, PLAY, ICP, limit=10)
    assert len(matches) == 1


def test_a_stale_row_is_not_delivered_as_fresh(db):
    """The BePresent lesson again: an old self-reported value is not trusted as current."""
    POOL.record(db, **_real_company())
    row = db.query(POOL.CompanyPool).one()
    row.last_verified_at = datetime.utcnow() - POOL.STALE_AFTER - timedelta(days=1)
    db.commit()
    matches = POOL.find_matches(db, PARTNER_B, PLAY, ICP, limit=10)
    assert len(matches) == 1
    assert matches[0].fresh is False


def test_revenue_matching_only_requires_overlap(db):
    icp = {**ICP, "revenue_min_usd": 2_500_000, "revenue_max_usd": 5_000_000}
    POOL.record(db, **_real_company(revenue_low_usd=1_000_000, revenue_high_usd=4_000_000))
    assert len(POOL.find_matches(db, PARTNER_B, PLAY, icp, limit=10)) == 1


def test_a_row_with_no_data_for_the_only_checkable_atom_is_not_matched(db):
    # decompose_icp() always implies a geography atom (defaults to United States), so an empty
    # ICP is not "nothing checkable" -- a row with no location data for it is the real case.
    POOL.record(db, **_real_company(location=None, country=None))
    assert POOL.find_matches(db, PARTNER_B, PLAY, {}, limit=10) == []


def test_to_search_row_round_trips_into_the_shape_the_processor_expects(db):
    POOL.record(db, **_real_company(headcount=25, revenue_low_usd=1_000_000, revenue_high_usd=4_000_000))
    row = db.query(POOL.CompanyPool).one()
    search_row = POOL.to_search_row(row)
    assert search_row["url"] == "https://www.linkedin.com/company/acme"
    assert search_row["numberOfEmployees"] == 25
    assert search_row["estimatedRevenuRange"]["estimatedMinRevenue"]["amount"] == 1_000_000


# ---- end to end: the real saving ----

def test_a_pool_hit_reduces_what_the_next_partner_needs_to_buy(db, monkeypatch):
    import app.deepline_client as dc
    import app.phases.decision_maker_reasoning as dmr
    import app.phases.free_decision_maker as fdm
    from app.gtm_os.plays import icp_filters as play

    # Seed the pool as if partner A's own real run had already paid for this company.
    POOL.record(db, **_real_company(headcount=25))

    calls = []
    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: calls.append(tool) or {
        "toolResponse": {"raw": {"leads": [], "pagination": {"token": None}}}})
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: [
        {"name": "Jane Doe", "title": "CEO", "linkedin_url": "https://www.linkedin.com/in/jane-doe"}])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, company, cands, n, offering_name=None: [
                            {"name": "Jane Doe", "thread_role": "founder_ceo", "reasoning": "CEO"}])

    feature_config.set_config(db, PARTNER_B, "accounts", {"daily_account_target": 1})

    result = play.search_icypeas(db, PARTNER_B, ICP)

    assert result["quota"]["delivered_from_pool"] == 1
    assert result["created"] == 1
    # The daily target (1) was met entirely from the pool -- NO paid search call was made.
    assert "icypeas_find_companies" not in calls
    assert db.query(GtmLead).filter(GtmLead.tenant_id == PARTNER_B).count() == 1


def test_a_partial_pool_hit_still_shrinks_the_paid_page_size(db, monkeypatch):
    import app.deepline_client as dc
    import app.phases.decision_maker_reasoning as dmr
    import app.phases.free_decision_maker as fdm
    from app.gtm_os.plays import icp_filters as play

    POOL.record(db, **_real_company(headcount=25))

    calls = []

    def fake_cli(tool, payload):
        calls.append((tool, payload))
        if tool == "icypeas_find_companies":
            return {"toolResponse": {"raw": {"leads": [], "pagination": {"token": None}}}}
        return {}

    monkeypatch.setattr(dc, "_call_deepline_cli", fake_cli)
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: [
        {"name": "Jane Doe", "title": "CEO", "linkedin_url": "https://www.linkedin.com/in/jane-doe"}])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, company, cands, n, offering_name=None: [
                            {"name": "Jane Doe", "thread_role": "founder_ceo", "reasoning": "CEO"}])

    feature_config.set_config(db, PARTNER_B, "accounts", {"daily_account_target": 10})

    result = play.search_icypeas(db, PARTNER_B, ICP)

    assert result["quota"]["delivered_from_pool"] == 1
    paid_call = next(p for t, p in calls if t == "icypeas_find_companies")
    # 10 needed, 1 delivered free -> remaining 9, not the original 10-derived size.
    assert paid_call["pagination"]["size"] < 14   # the pre-pool page size for a target of 10
