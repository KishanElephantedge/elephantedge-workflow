"""Play F (ICP filters, for partners): search built from the partner ICP, exact company size
checked before anything else, rows land on the partner's tenant while spend lands on the billing
tenant, and a qualified lead is outreach-ready with the searched person as its contact.
No real provider or LLM call is made here."""
import copy

import pytest

import app.harvestapi as h
import app.llm_client as llm_client
from app.db.models import Batch, CampaignPush, Company, Contact, Parameter
from app.gtm_os.orchestration.control import DEFAULT_GTM_OS_CONTROL_CONFIG, set_control_config
from app.gtm_os.plays import icp_filters as play
from app.gtm_os.plays.lead import GtmLead
from app.spend_ledger import ProviderSpend, reserve_spend, spend_scope

PARTNER, BILLING = 15, 2
ICP = {"employee_min": 30, "employee_max": 100, "decision_maker_titles": ["Owner", "Founder", "CEO"], "geographies": [],
       "notes": "Sell to SMBs that already have a small sales team."}


@pytest.fixture
def db(db_factory):
    from app.gtm_os.intelligence.signal import GtmSignal
    from app.gtm_os.learning.message_draft import MessageDraft
    from app.gtm_os.opportunity.opportunity import Opportunity

    db = db_factory([Parameter, ProviderSpend, GtmLead, Batch, Company, Contact, CampaignPush, GtmSignal, Opportunity, MessageDraft])
    config = copy.deepcopy(DEFAULT_GTM_OS_CONTROL_CONFIG)
    config["spend"] = {"daily_cap_usd": 1.0, "run_cap_usd": 0.5}
    set_control_config(db, BILLING, config)
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
    import copy

    from app.gtm_os.orchestration.control import DEFAULT_GTM_OS_CONTROL_CONFIG, set_control_config

    config = copy.deepcopy(DEFAULT_GTM_OS_CONTROL_CONFIG)
    config["state"] = "paused"
    config["spend"] = {"daily_cap_usd": 1.0, "run_cap_usd": 0.5}
    set_control_config(db, BILLING, config)
    assert play.run_icp_filters(db, PARTNER)["status"] == "skipped"
