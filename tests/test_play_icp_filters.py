"""Play F (ICP filters, for partners): search built from the partner ICP, exact company size
checked before anything else, rows land on the partner's tenant while spend lands on the billing
tenant, and a qualified lead is outreach-ready with the searched person as its contact.
No real provider or LLM call is made here -- fetch_public_company_profile (a real httpx call to
LinkedIn's public page) is mocked in every test that reaches it, same discipline as every paid
provider call."""
import copy

import pytest

import app.harvestapi as h
import app.llm_client as llm_client
import app.phases.company_profile_check as cpc
from app.db.models import Batch, CampaignPush, Company, Contact, Parameter
from app.gtm_os.orchestration.control import DEFAULT_GTM_OS_CONTROL_CONFIG, set_control_config
from app.gtm_os.plays import icp_filters as play
from app.gtm_os.plays.lead import GtmLead
from app.spend_ledger import ProviderSpend, reserve_spend, spend_scope

PARTNER, BILLING = 15, 2
ICP = {"employee_min": 30, "employee_max": 100, "decision_maker_titles": ["Owner", "Founder", "CEO"], "geographies": [],
       "notes": "Sell to SMBs that already have a small sales team."}


@pytest.fixture
def db(db_factory, monkeypatch):
    from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
    from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
    from app.gtm_os.intelligence.signal import GtmSignal
    from app.gtm_os.learning.message_draft import MessageDraft
    from app.gtm_os.opportunity.opportunity import Opportunity
    from app.gtm_os.strategy.strategy import GtmStrategy

    db = db_factory([Parameter, ProviderSpend, GtmLead, Batch, Company, Contact, CampaignPush, GtmSignal,
                     ProblemHypothesis, DemandHypothesis, Opportunity, GtmStrategy, MessageDraft])
    config = copy.deepcopy(DEFAULT_GTM_OS_CONTROL_CONFIG)
    config["spend"] = {"daily_cap_usd": 1.0, "run_cap_usd": 0.5}
    set_control_config(db, BILLING, config)
    # Default: the free public-page check finds nothing usable, so every existing test's
    # behaviour (always falls through to the paid lookup) is unchanged unless a test overrides
    # this to prove the free path itself works.
    monkeypatch.setattr(cpc, "fetch_public_company_profile", lambda url: None)
    return db


def _person(n, company):
    return {"first_name": f"P{n}", "last_name": "X", "linkedin_url": f"https://www.linkedin.com/in/p{n}", "title": "Founder",
            "company_name": company, "company_linkedin_url": f"https://www.linkedin.com/company/{company.lower()}", "headline": ""}


def test_headcount_band_maps_to_linkedin_buckets():
    assert play.headcount_ranges(30, 100) == "11-50,51-200"
    assert play.search_filters(ICP)["locations"] == "United States"


def test_search_verifies_size_and_writes_to_the_partner_tenant(db, monkeypatch):
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_person(1, "Good"), _person(2, "Good"), _person(3, "Huge")])
    lookups = []

    def fake_company(u):
        lookups.append(u)
        reserve_spend(db, BILLING, "deepline", 0.003, operation="get_company")
        return {"name": u.title(), "employee_count": 60 if u == "good" else 900, "industry": "Manufacturing",
                "hq_text": "Ohio, US", "website": f"https://{u}.com", "description": "", "linkedin_url": f"https://www.linkedin.com/company/{u}"}

    monkeypatch.setattr(h, "get_company", fake_company)
    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search(db, PARTNER, ICP)

    assert result["outcomes"] == {"created": 1, "known": 1, "size_outside_icp": 1}
    assert sorted(lookups) == ["good", "huge"]
    good = db.query(GtmLead).filter(GtmLead.state == "signal").one()
    assert db.query(Batch).get(db.get(Company, good.company_id).batch_id).tenant_id == PARTNER
    assert db.get(Contact, good.contact_id).linkedin_url == "https://www.linkedin.com/in/p1"
    assert {r.tenant_id for r in db.query(ProviderSpend)} == {BILLING}
    assert db.query(Parameter).filter(Parameter.tenant_id == PARTNER, Parameter.key == play.CURSOR_KEY).one().value["page"] == 2


def test_qualified_lead_is_outreach_ready(db, monkeypatch):
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_person(1, "Good")])
    monkeypatch.setattr(h, "get_company", lambda u: {"name": "Good", "employee_count": 60, "industry": "Manufacturing",
                                                     "hq_text": "", "website": "https://good.com", "description": "", "linkedin_url": None})
    monkeypatch.setattr(llm_client, "generate_json", lambda prompt, db, t, max_tokens=0: {"qualified": True, "icp_fit_score": 80, "reason": "fits"})
    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        play.search(db, PARTNER, ICP)
    assert play.qualify(db, PARTNER, ICP)["qualified"] == 1
    assert db.query(GtmLead).one().state == "contact_found"


def test_run_respects_paused_control_plane(db):
    config = copy.deepcopy(DEFAULT_GTM_OS_CONTROL_CONFIG)
    config["state"] = "paused"
    config["spend"] = {"daily_cap_usd": 1.0, "run_cap_usd": 0.5}
    set_control_config(db, BILLING, config)
    assert play.run_icp_filters(db, PARTNER)["status"] == "skipped"


# ------------------------------------------------------------------ free-first rejection (2026-09-28 fix)

def test_obvious_vendor_name_is_rejected_free_no_lookup_spent(db, monkeypatch):
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_person(1, "Acme Recruiting Agency")])
    monkeypatch.setattr(h, "get_company", lambda u: pytest.fail("a vendor name match must never reach the paid lookup"))
    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search(db, PARTNER, ICP)
    assert result["outcomes"] == {"vendor_name_match": 1}
    lead = db.query(GtmLead).one()
    assert lead.state == "rejected" and "vendor/agency/recruiter" in lead.qualifier_reason
    assert db.query(ProviderSpend).count() == 0


def test_looks_like_a_vendor_matches_real_and_avoids_false_positives():
    for name in ("Acme Recruiting Agency", "Bright Path Consulting", "Talent Acquisition Partners",
                 "Smith & Co Staffing", "Growth Coaches LLC"):
        assert play._looks_like_a_vendor(name), name
    for name in ("Acme Manufacturing", "Brightline Software", "Consultative Sales Inc", "Recon Robotics"):
        assert not play._looks_like_a_vendor(name), name


def test_free_size_band_rejects_only_when_confirmed_too_big(db, monkeypatch):
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_person(1, "Huge")])
    monkeypatch.setattr(cpc, "fetch_public_company_profile", lambda url: {"size_band": (501, 1000), "country": "US", "industry": None, "about": None})
    monkeypatch.setattr(h, "get_company", lambda u: pytest.fail("a confirmed too-big free band must never reach the paid lookup"))
    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search(db, PARTNER, ICP)
    assert result["outcomes"] == {"size_outside_icp_free": 1}
    lead = db.query(GtmLead).one()
    assert "free public LinkedIn page" in lead.qualifier_reason
    assert db.query(ProviderSpend).count() == 0


def test_free_size_band_that_looks_too_small_still_falls_through_to_paid(db, monkeypatch):
    """Real bug found live 2026-09-28: BePresent's public page declares "2-10 employees" while
    its real, paid-lookup count is 31 -- genuinely inside a 30-100 ICP. A "too small" free band
    must never reject on its own (LinkedIn's self-declared band understates a company that has
    grown since it was last updated); only the paid, exact-count lookup may decide that."""
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_person(1, "BePresent")])
    monkeypatch.setattr(cpc, "fetch_public_company_profile", lambda url: {"size_band": (2, 10), "country": "US", "industry": None, "about": None})
    called = []
    monkeypatch.setattr(h, "get_company", lambda u: called.append(u) or {
        "name": "BePresent", "employee_count": 31, "industry": "Wellness", "hq_text": "", "website": "https://bepresent.app",
        "description": "", "linkedin_url": None})
    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search(db, PARTNER, ICP)
    assert called == ["bepresent"]
    assert result["outcomes"] == {"created": 1}


def test_free_size_band_that_fits_still_uses_the_paid_lookup_for_full_facts(db, monkeypatch):
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_person(1, "Good")])
    monkeypatch.setattr(cpc, "fetch_public_company_profile", lambda url: {"size_band": (50, 100), "country": "US", "industry": None, "about": None})
    called = []
    monkeypatch.setattr(h, "get_company", lambda u: called.append(u) or {
        "name": "Good", "employee_count": 60, "industry": "Manufacturing", "hq_text": "", "website": "https://good.com",
        "description": "", "linkedin_url": None})
    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search(db, PARTNER, ICP)
    assert called == ["good"]
    assert result["outcomes"] == {"created": 1}


def test_free_size_band_inconclusive_falls_through_to_paid_lookup(db, monkeypatch):
    # No declared band at all (e.g. an unreadable page) -- must not be treated as a reject.
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_person(1, "Good")])
    monkeypatch.setattr(cpc, "fetch_public_company_profile", lambda url: {"size_band": None, "country": None, "industry": None, "about": None})
    called = []
    monkeypatch.setattr(h, "get_company", lambda u: called.append(u) or {
        "name": "Good", "employee_count": 60, "industry": "Manufacturing", "hq_text": "", "website": "https://good.com",
        "description": "", "linkedin_url": None})
    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        play.search(db, PARTNER, ICP)
    assert called == ["good"]


def test_qualified_lead_gets_a_real_opportunity_visible_in_the_pipeline(db, monkeypatch):
    """Real gap fixed 2026-09-28: a qualified lead used to just flip state, with no
    Opportunity/Strategy ever written -- invisible to the Pipeline/Accounts dashboard."""
    from app.gtm_os.opportunity.opportunity import Opportunity
    from app.gtm_os.strategy.strategy import GtmStrategy

    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_person(1, "Good")])
    monkeypatch.setattr(h, "get_company", lambda u: {"name": "Good", "employee_count": 60, "industry": "Manufacturing",
                                                     "hq_text": "", "website": "https://good.com", "description": "", "linkedin_url": None})
    monkeypatch.setattr(llm_client, "generate_json", lambda prompt, db, t, max_tokens=0: {
        "qualified": True, "icp_fit_score": 80, "reason": "fits", "problem_statement": "No dedicated sales leader.",
        "demand_statement": "Wants help scaling sales.", "positioning_angle": "Ask about their sales process."})
    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        play.search(db, PARTNER, ICP)
    play.qualify(db, PARTNER, ICP)

    lead = db.query(GtmLead).one()
    assert lead.state == "contact_found" and lead.opportunity_id is not None
    opportunity = db.get(Opportunity, lead.opportunity_id)
    assert opportunity.tenant_id == PARTNER and opportunity.status == "qualified"
    strategy = db.query(GtmStrategy).filter(GtmStrategy.opportunity_id == opportunity.id).one()
    assert strategy.positioning_angle == "Ask about their sales process."
