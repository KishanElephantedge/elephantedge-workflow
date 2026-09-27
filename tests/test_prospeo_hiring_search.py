"""Play B's Prospeo search: filters come from the ICP config, one search page is one reserved
credit, no-results costs nothing, a budget refusal sends no request, each company becomes one lead
with its decision maker already attached, and the next run continues from the next page.
No real HTTP call is made here."""
import copy

import pytest

import app.prospeo_client as prospeo
from app.db.models import Batch, CampaignPush, Company, Contact, Credential, Parameter
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.orchestration.control import DEFAULT_GTM_OS_CONTROL_CONFIG, set_control_config
from app.gtm_os.plays import hiring as play
from app.gtm_os.plays.lead import GtmLead
from app.spend_ledger import ProviderSpend, spend_scope, total_spend_today

TENANT = 2
ICP = {"id": "icp_3", "name": "Needs Fractional Leadership", "revenue_min_usd": 20_000_000, "revenue_max_usd": 50_000_000,
       "employee_max": 300, "trigger_mode": "requires_presence", "trigger_hiring_roles": ["head_of_sales"], "enabled": True}


@pytest.fixture
def db(db_factory, monkeypatch):
    db = db_factory([Parameter, Credential, ProviderSpend, GtmSignal, GtmLead, Batch, Company, Contact, CampaignPush,
                     ProblemHypothesis, DemandHypothesis, Opportunity, MessageDraft])
    config = copy.deepcopy(DEFAULT_GTM_OS_CONTROL_CONFIG)
    config["spend"] = {"daily_cap_usd": 1.0, "run_cap_usd": 0.5}
    set_control_config(db, TENANT, config)
    db.add(Credential(tenant_id=TENANT, name="prospeo_api_key", value="test-key"))
    db.commit()
    import app.gtm_os.icp.icp_config as icp_config
    monkeypatch.setattr(icp_config, "get_icp_config", lambda db, t: [ICP])
    return db


def _hit(n, company="Acme", domain="acme.io"):
    return {"person": {"person_id": f"p{n}", "first_name": f"P{n}", "last_name": "X", "full_name": f"P{n} X",
                       "linkedin_url": f"https://www.linkedin.com/in/p{n}", "current_job_title": "CEO"},
            "company": {"name": company, "domain": domain, "industry": "Software Development", "employee_count": 200,
                        "revenue_range": "25M-50M"}}


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def _serve(monkeypatch, *bodies):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        status, body = bodies[min(len(calls), len(bodies)) - 1]
        return FakeResponse(status, body)

    monkeypatch.setattr(prospeo.httpx, "post", post)
    return calls


def test_filters_come_from_the_icp():
    f = play.prospeo_filters_for_icp(ICP)
    assert f["company_revenue"] == {"include_unknown_revenue": False, "min": "10M", "max": "50M"}
    assert "VP Sales" in f["company_job_posting_hiring_for"]["include"]
    assert f["company_location_search"] == {"include": ["United States"]}
    assert "IT Services and IT Consulting" not in f["company_industry"]["include"]


def test_revenue_steps_widen_outward():
    assert prospeo.revenue_filter(10_000_000, 20_000_000) == {"include_unknown_revenue": False, "min": "10M", "max": "25M"}


def test_one_lead_per_company_with_its_decision_maker(db, monkeypatch):
    calls = _serve(monkeypatch, (200, {"error": False, "results": [_hit(1), _hit(2), _hit(3, "Beta", "beta.io")],
                                       "pagination": {"current_page": 1, "total_page": 4}}))
    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        result = play.sense_prospeo(db, TENANT)

    assert len(calls) == 1 and calls[0]["page"] == 1
    assert result["outcomes"] == {"created": 2, "known": 1}
    leads = db.query(GtmLead).all()
    assert len(leads) == 2 and all(l.contact_id and l.state == "signal" for l in leads)
    assert db.query(Contact).count() == 3
    assert total_spend_today(db, TENANT) == pytest.approx(prospeo.USD_PER_CREDIT)
    assert db.query(Parameter).filter(Parameter.key == play.PROSPEO_CURSOR_KEY).one().value["icp_3"]["next_page"] == 2


def test_next_run_continues_from_the_next_page(db, monkeypatch):
    calls = _serve(monkeypatch, (200, {"error": False, "results": [_hit(1)], "pagination": {"total_page": 4}}),
                   (200, {"error": False, "results": [_hit(9, "Gamma", "gamma.io")], "pagination": {"total_page": 4}}))
    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        play.sense_prospeo(db, TENANT)
        play.sense_prospeo(db, TENANT)
    assert [c["page"] for c in calls] == [1, 2]


def test_no_results_costs_nothing(db, monkeypatch):
    _serve(monkeypatch, (400, {"error": True, "error_code": "NO_RESULTS"}))
    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        assert play.sense_prospeo(db, TENANT)["people"] == 0
    assert total_spend_today(db, TENANT) == 0.0


def test_plan_required_is_reported_and_free(db, monkeypatch):
    _serve(monkeypatch, (400, {"error": True, "error_code": "PLAN_REQUIRED"}))
    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        result = play.sense_prospeo(db, TENANT)
    assert result["icps"]["icp_3"]["code"] == "PLAN_REQUIRED"
    assert total_spend_today(db, TENANT) == 0.0


def test_budget_refusal_sends_no_request(db, monkeypatch):
    calls = _serve(monkeypatch, (200, {"error": False, "results": [_hit(1)]}))
    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.001):
        assert play.sense_prospeo(db, TENANT)["stopped"].startswith("budget")
    assert calls == []


def test_company_already_in_outreach_is_skipped(db, monkeypatch):
    batch = Batch(tenant_id=TENANT, name="old")
    db.add(batch)
    db.commit()
    company = Company(batch_id=batch.id, name="Acme", domain="acme.io")
    db.add(company)
    db.commit()
    c = Contact(company_id=company.id, first_name="Old")
    db.add(c)
    db.commit()
    db.add(CampaignPush(contact_id=c.id, status="pushed"))
    db.commit()
    _serve(monkeypatch, (200, {"error": False, "results": [_hit(1)]}))
    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        assert play.sense_prospeo(db, TENANT)["outcomes"] == {"in_outreach": 1}
    assert db.query(GtmLead).count() == 0
