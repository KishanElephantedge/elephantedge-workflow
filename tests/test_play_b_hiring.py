"""Play B (hiring): one lead per company, nothing paid before the Qualifier says yes, a company
already in outreach is never picked up, and a refused paid lookup leaves the lead waiting.
No real provider or LLM call is made here."""
import copy
from datetime import datetime

import pytest

import app.llm_client as llm_client
import app.phases.decision_maker as decision_maker
from app.db.models import Batch, CampaignPush, Company, Contact, Parameter
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.orchestration.control import DEFAULT_GTM_OS_CONTROL_CONFIG, set_control_config
from app.gtm_os.plays import hiring as play
from app.gtm_os.plays.lead import GtmLead
from app.gtm_os.strategy.strategy import GtmStrategy
from app.spend_ledger import ProviderSpend, reserve_spend, spend_scope, total_spend_today

TENANT = 2


@pytest.fixture
def db(db_factory, monkeypatch):
    db = db_factory([Parameter, ProviderSpend, GtmSignal, GtmLead, Batch, Company, Contact, CampaignPush,
                     ProblemHypothesis, DemandHypothesis, Opportunity, GtmStrategy, MessageDraft])
    config = copy.deepcopy(DEFAULT_GTM_OS_CONTROL_CONFIG)
    config["spend"] = {"daily_cap_usd": 1.0, "run_cap_usd": 0.50}
    set_control_config(db, TENANT, config)
    monkeypatch.setattr(play, "_qualifier_context", lambda db, t: {
        "company_name": "Elephant Edge", "offerings": "- Sales OS: AI SDR", "icps": "- icp_3 (Needs Fractional Leadership)",
        "icp_ids": '"icp_3"', "offering_names": '"Sales OS"',
        "valid_icps": {"icp_3": "Needs Fractional Leadership"}, "valid_offerings": {"Sales OS"},
    })
    return db


def _company(db, name="Acme", domain="acme.io"):
    batch = db.query(Batch).first() or Batch(tenant_id=TENANT, name="b", source="play_b")
    db.add(batch)
    db.commit()
    company = Company(batch_id=batch.id, name=name, domain=domain, employee_count=180, industry="Software Development")
    db.add(company)
    db.commit()
    return company


def _posting(db, company, ref, title="VP of Sales"):
    db.add(GtmSignal(tenant_id=TENANT, source="linkedin_job", source_ref=ref, signal_type="job_posting",
                     company_id=company.id, extracted_info={"title": title, "description_text": "Build our first sales team."},
                     raw_evidence={}, captured_at=datetime.utcnow(), dedup_key=f"linkedin_job:{ref}"))
    db.commit()


def _verdict(qualified=True, score=85, icp="icp_3", offering="Sales OS"):
    return {"qualified": qualified, "icp_fit_score": score, "matched_icp_id": icp, "matched_offering": offering,
            "reason": "Hiring its first VP Sales.", "evidence_quote": "Build our first sales team.",
            "problem_statement": "No sales leadership.", "demand_statement": "Needs a sales leader.",
            "positioning_angle": "Ask about the VP Sales search.", "who_to_contact": "CEO"}


def test_one_lead_per_company_and_outreach_companies_skipped(db):
    acme = _company(db)
    _posting(db, acme, "1")
    _posting(db, acme, "2", title="Head of Sales")
    pushed = _company(db, "Pushed", "pushed.io")
    _posting(db, pushed, "3")
    contact = Contact(company_id=pushed.id, first_name="P")
    db.add(contact)
    db.commit()
    db.add(CampaignPush(contact_id=contact.id, status="pushed"))
    db.commit()

    assert play.ingest_new_signals(db, TENANT)["created"] == 1
    lead = db.query(GtmLead).one()
    assert (lead.lead_key, lead.company_id) == (f"company:{acme.id}", acme.id)
    assert "Head of Sales" in lead.evidence and "VP of Sales" in lead.evidence
    assert play.ingest_new_signals(db, TENANT)["created"] == 0


def test_qualifier_gates_on_score_icp_and_offering(db, monkeypatch):
    companies = [_company(db, n, f"{n}.io") for n in ("good", "low", "noicp")]
    for i, c in enumerate(companies):
        _posting(db, c, str(i))
    play.ingest_new_signals(db, TENANT)
    verdicts = {"good": _verdict(), "low": _verdict(score=40), "noicp": _verdict(icp="icp_1")}
    monkeypatch.setattr(llm_client, "generate_json", lambda prompt, db, t, max_tokens=0: next(
        v for name, v in verdicts.items() if f"Company: {name} " in prompt))

    assert play.qualify_leads(db, TENANT)["qualified"] == 1
    states = {db.get(Company, l.company_id).name: l.state for l in db.query(GtmLead)}
    assert states == {"good": "qualified", "low": "rejected", "noicp": "rejected"}
    assert total_spend_today(db, TENANT) == 0.0


def _qualified(db, monkeypatch):
    acme = _company(db)
    _posting(db, acme, "1")
    play.ingest_new_signals(db, TENANT)
    monkeypatch.setattr(llm_client, "generate_json", lambda *a, **k: _verdict())
    play.qualify_leads(db, TENANT)
    return acme


def test_known_contact_is_reused_without_spend(db, monkeypatch):
    acme = _qualified(db, monkeypatch)
    db.add(Contact(company_id=acme.id, first_name="Jane", title="CEO", email="jane@acme.io"))
    db.commit()
    monkeypatch.setattr(decision_maker, "find_decision_makers", lambda *a, **k: pytest.fail("must not look up"))

    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        assert play.find_contacts(db, TENANT)["reused"] == 1
    lead = db.query(GtmLead).one()
    assert lead.state == "contact_found" and lead.person_name == "Jane"
    strategy = db.query(GtmStrategy).filter(GtmStrategy.opportunity_id == lead.opportunity_id).one()
    assert strategy.matched_offering_name == "Sales OS"
    assert total_spend_today(db, TENANT) == 0.0


def test_found_decision_maker_moves_lead_forward(db, monkeypatch):
    acme = _qualified(db, monkeypatch)

    def fake_find(company, db_, tenant_id, allow_paid_fallback=True, max_contacts=3):
        reserve_spend(db_, tenant_id, "deepline", 0.168, operation="search_contact")
        c = Contact(company_id=company.id, first_name="Sam", title="CEO", linkedin_url="https://linkedin.com/in/sam")
        db_.add(c)
        db_.commit()
        return [c], True

    monkeypatch.setattr(decision_maker, "find_decision_makers", fake_find)
    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        assert play.find_contacts(db, TENANT)["found"] == 1
    lead = db.query(GtmLead).one()
    assert lead.state == "contact_found" and lead.spend_usd == pytest.approx(0.168)
    assert db.get(Opportunity, lead.opportunity_id).company_id == acme.id


def test_refused_paid_lookup_leaves_lead_waiting(db, monkeypatch):
    _qualified(db, monkeypatch)

    def fake_find(company, db_, tenant_id, allow_paid_fallback=True, max_contacts=3):
        try:  # the real finder swallows the refusal and reports "nobody found"
            reserve_spend(db_, tenant_id, "deepline", 5.0, operation="search_contact")
        except Exception:
            pass
        return [], True

    monkeypatch.setattr(decision_maker, "find_decision_makers", fake_find)
    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        result = play.find_contacts(db, TENANT)
    assert result["stopped"].startswith("budget")
    assert db.query(GtmLead).one().state == "qualified"


def test_nobody_found_is_terminal(db, monkeypatch):
    _qualified(db, monkeypatch)
    monkeypatch.setattr(decision_maker, "find_decision_makers", lambda *a, **k: ([], False))
    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        assert play.find_contacts(db, TENANT)["missing"] == 1
    assert db.query(GtmLead).one().state == "contact_missing"


def test_run_respects_paused_control_plane(db):
    config = copy.deepcopy(DEFAULT_GTM_OS_CONTROL_CONFIG)
    config["state"] = "paused"
    config["spend"] = {"daily_cap_usd": 1.0, "run_cap_usd": 0.5}
    set_control_config(db, TENANT, config)
    assert play.run_play_b(db, TENANT)["status"] == "skipped"
