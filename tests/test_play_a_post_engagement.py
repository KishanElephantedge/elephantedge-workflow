"""Play A (LinkedIn post engagement): each lead moves forward once, nothing is paid for before the
Qualifier says yes, and a budget refusal leaves leads waiting instead of changing their state.
No real provider or LLM call is made here."""
import copy
from datetime import datetime

import pytest

import app.deepline_client as deepline_client
import app.llm_client as llm_client
from app.db.models import Batch, Company, Contact, Parameter
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.orchestration.control import DEFAULT_GTM_OS_CONTROL_CONFIG, set_control_config
from app.gtm_os.plays import post_engagement as play
from app.gtm_os.plays.lead import GtmLead
from app.gtm_os.strategy.strategy import GtmStrategy
from app.spend_ledger import ProviderSpend, spend_scope, total_spend_today

TENANT = 2


@pytest.fixture
def db(db_factory, monkeypatch):
    db = db_factory([Parameter, ProviderSpend, GtmSignal, GtmLead, Batch, Company, Contact,
                     ProblemHypothesis, DemandHypothesis, Opportunity, GtmStrategy, MessageDraft])
    config = copy.deepcopy(DEFAULT_GTM_OS_CONTROL_CONFIG)
    config["spend"] = {"daily_cap_usd": 1.0, "run_cap_usd": 0.50}
    set_control_config(db, TENANT, config)
    monkeypatch.setattr(play, "_qualifier_context", lambda db, t: {
        "company_name": "Elephant Edge", "offerings": "- Sales OS: AI SDR", "icps": "- icp_2 (Upgrading With AI)",
        "icp_ids": '"icp_2"', "offering_names": '"Sales OS"',
        "valid_icps": {"icp_2": "Upgrading With AI"}, "valid_offerings": {"Sales OS"},
    })
    monkeypatch.setattr(deepline_client, "is_deepline_enabled", lambda: True)
    return db


def _signal(db, source, url, text, **info):
    extracted = {"author_profile_url": url, **info}
    if source == "linkedin_engagement":
        extracted.setdefault("comment_text", text)
        extracted.setdefault("post_text", "How do you scale outbound?")
    else:
        extracted.setdefault("text", text)
    s = GtmSignal(tenant_id=TENANT, source=source, source_ref=url, signal_type="x", person_name_raw="Jane Doe",
                  extracted_info=extracted, raw_evidence={}, captured_at=datetime.utcnow(), dedup_key=url)
    db.add(s)
    db.commit()
    return s


def _verdict(qualified=True, score=85, intent="pain", offering="Sales OS"):
    return {"qualified": qualified, "icp_fit_score": score, "intent": intent, "matched_icp_id": "icp_2",
            "matched_offering": offering, "reason": "Founder struggling to scale outbound.",
            "evidence_quote": "we can't keep up", "company_guess": "Acme",
            "problem_statement": "Outbound can't keep up.", "demand_statement": "Wants help scaling sales.",
            "positioning_angle": "Ask how they handle outbound today."}


PROSPEO_HIT = {"toolResponse": {"raw": {
    "person": {"first_name": "Jane", "last_name": "Doe", "current_job_title": "CEO",
               "email": {"email": "Jane@Acme.io", "status": "VERIFIED"}},
    "company": {"name": "Acme", "domain": "acme.io", "linkedin_url": "https://linkedin.com/company/acme"},
}}}


def test_ingest_skips_non_people_and_never_ingests_twice(db):
    _signal(db, "linkedin_engagement", "https://www.linkedin.com/in/Jane-Doe/?x=1", "we can't keep up with outbound")
    _signal(db, "linkedin_post", "https://www.linkedin.com/company/acme", "hi", author_type="Company")
    _signal(db, "linkedin_post", "https://www.linkedin.com/search/results/content/?keywords=x", "hi")
    _signal(db, "linkedin_post", "https://www.linkedin.com/in/recruiter", "We're hiring our next SDR. Apply now!")

    assert play.ingest_new_signals(db, TENANT)["created"] == 1
    lead = db.query(GtmLead).one()
    assert lead.person_linkedin_url == "linkedin.com/in/jane-doe"
    assert play.ingest_new_signals(db, TENANT)["created"] == 0  # same person, never again


def test_qualifier_decides_and_nothing_is_paid(db, monkeypatch):
    _signal(db, "linkedin_engagement", "https://linkedin.com/in/a", "we can't keep up")
    _signal(db, "linkedin_engagement", "https://linkedin.com/in/b", "great post!")
    play.ingest_new_signals(db, TENANT)
    verdicts = {"linkedin.com/in/a": _verdict(), "linkedin.com/in/b": _verdict(qualified=False, score=20, intent="sharing_opinion")}
    monkeypatch.setattr(llm_client, "generate_json", lambda prompt, db, t, max_tokens=0: next(
        v for url, v in verdicts.items() if url in prompt))

    result = play.qualify_leads(db, TENANT)
    assert (result["qualified"], result["rejected"]) == (1, 1)
    states = {l.person_linkedin_url: l.state for l in db.query(GtmLead)}
    assert states == {"linkedin.com/in/a": "qualified", "linkedin.com/in/b": "rejected"}
    assert total_spend_today(db, TENANT) == 0.0


def test_low_score_or_unknown_offering_is_rejected_even_if_llm_says_yes(db, monkeypatch):
    _signal(db, "linkedin_engagement", "https://linkedin.com/in/a", "x")
    _signal(db, "linkedin_engagement", "https://linkedin.com/in/b", "y")
    play.ingest_new_signals(db, TENANT)
    verdicts = {"linkedin.com/in/a": _verdict(score=50), "linkedin.com/in/b": _verdict(offering="Made Up")}
    monkeypatch.setattr(llm_client, "generate_json", lambda prompt, db, t, max_tokens=0: next(
        v for url, v in verdicts.items() if url in prompt))
    play.qualify_leads(db, TENANT)
    assert {l.state for l in db.query(GtmLead)} == {"rejected"}


def _qualified_lead(db, monkeypatch, url="https://linkedin.com/in/jane"):
    _signal(db, "linkedin_engagement", url, "we can't keep up")
    play.ingest_new_signals(db, TENANT)
    monkeypatch.setattr(llm_client, "generate_json", lambda *a, **k: _verdict())
    play.qualify_leads(db, TENANT)


def test_contact_found_writes_company_contact_and_opportunity(db, monkeypatch):
    _qualified_lead(db, monkeypatch)
    monkeypatch.setattr(deepline_client, "_call_deepline_cli", lambda tool, payload: copy.deepcopy(PROSPEO_HIT))

    with spend_scope(db, TENANT, "play_a", run_cap_usd=0.50):
        assert play.find_contacts(db, TENANT)["found"] == 1
    lead = db.query(GtmLead).one()
    assert lead.state == "contact_found"
    contact = db.get(Contact, lead.contact_id)
    assert (contact.email, contact.email_source) == ("jane@acme.io", "prospeo")
    assert db.get(Company, lead.company_id).domain == "acme.io"
    opp = db.get(Opportunity, lead.opportunity_id)
    strategy = db.query(GtmStrategy).filter(GtmStrategy.opportunity_id == opp.id).one()
    assert (opp.status, strategy.offering_fit_status, strategy.matched_offering_name) == ("qualified", "candidate_match", "Sales OS")
    assert total_spend_today(db, TENANT) == pytest.approx(0.055)


def test_miss_costs_nothing_and_is_terminal(db, monkeypatch):
    _qualified_lead(db, monkeypatch)
    monkeypatch.setattr(deepline_client, "_call_deepline_cli", lambda tool, payload: {"toolResponse": {"raw": {}}})
    with spend_scope(db, TENANT, "play_a"):
        assert play.find_contacts(db, TENANT)["missing"] == 1
    assert db.query(GtmLead).one().state == "contact_missing"
    assert total_spend_today(db, TENANT) == 0.0


def test_budget_refusal_leaves_lead_waiting(db, monkeypatch):
    _qualified_lead(db, monkeypatch)
    called = []
    monkeypatch.setattr(deepline_client, "_call_deepline_cli", lambda t, p: called.append(t) or copy.deepcopy(PROSPEO_HIT))
    with spend_scope(db, TENANT, "play_a", run_cap_usd=0.01):  # below one lookup
        result = play.find_contacts(db, TENANT)
    assert result["stopped"].startswith("budget")
    assert called == []
    assert db.query(GtmLead).one().state == "qualified"


def test_draft_targets_the_engager(db, monkeypatch):
    _qualified_lead(db, monkeypatch)
    monkeypatch.setattr(deepline_client, "_call_deepline_cli", lambda tool, payload: copy.deepcopy(PROSPEO_HIT))
    with spend_scope(db, TENANT, "play_a"):
        play.find_contacts(db, TENANT)
    lead = db.query(GtmLead).one()
    other = Contact(company_id=lead.company_id, first_name="Other", title="CFO")
    db.add(other)
    db.commit()

    seen = {}

    class FakeDraft:
        id = None
        message_text = "Hi Jane"
        status = "ready_for_review"
        missing_information = []

    def fake_generate(db_, tenant_id, opportunity, strategy, exclude_contact_ids=None):
        seen["excluded"] = exclude_contact_ids
        return FakeDraft()

    import app.gtm_os.learning.message_draft as md
    monkeypatch.setattr(md, "generate_message_draft", fake_generate)
    assert play.draft_messages(db, TENANT)["drafted"] == 1
    assert seen["excluded"] == [other.id]
    assert db.query(GtmLead).one().state == "drafted"


def test_run_respects_paused_control_plane(db):
    config = copy.deepcopy(DEFAULT_GTM_OS_CONTROL_CONFIG)
    config["state"] = "paused"
    config["spend"] = {"daily_cap_usd": 1.0, "run_cap_usd": 0.5}
    set_control_config(db, TENANT, config)
    assert play.run_play_a(db, TENANT)["status"] == "skipped"


def test_linkedin_only_contact_is_free(db, monkeypatch):
    _qualified_lead(db, monkeypatch)
    monkeypatch.setattr(deepline_client, "_call_deepline_cli", lambda t, p: pytest.fail("must not buy an email"))
    with spend_scope(db, TENANT, "play_a"):
        assert play.find_contacts(db, TENANT, channels=["linkedin"])["found"] == 1
    lead = db.query(GtmLead).one()
    assert lead.state == "contact_found"
    assert db.get(Contact, lead.contact_id).linkedin_url == "https://www.linkedin.com/in/jane"
    assert db.get(Company, lead.company_id).name == "Acme"
    assert total_spend_today(db, TENANT) == 0.0


def test_harvest_takes_commenters_not_the_author_and_never_reharvests(db, monkeypatch):
    import app.harvestapi as h
    import app.gtm_os.intelligence.linkedin_search_config as lsc
    monkeypatch.setattr(lsc, "get_linkedin_search_config", lambda db, t: {"phrases": ["scale outbound"]})
    post = {"id": "p1", "linkedinUrl": "https://www.linkedin.com/posts/x", "content": "How do you scale outbound?",
            "author": {"name": "Guru", "linkedinUrl": "https://www.linkedin.com/in/guru"}, "engagement": {"comments": 12}}
    monkeypatch.setattr(h, "search_posts", lambda q, **k: [post, {**post, "id": "p2", "engagement": {"comments": 1}}])
    harvested = []
    monkeypatch.setattr(h, "get_post_comments", lambda url, **k: harvested.append(url) or [
        {"commentary": "We can't keep up", "actor": {"name": "Jane Doe", "linkedinUrl": "https://www.linkedin.com/in/jane", "position": "CEO at Acme"}},
        {"commentary": "Thanks all", "actor": {"name": "Guru", "linkedinUrl": "https://www.linkedin.com/in/guru"}}])

    result = play.sense_harvest(db, TENANT)
    assert (result["posts_harvested"], result["new_signals"]) == (1, 1)
    assert play.ingest_new_signals(db, TENANT)["created"] == 1
    assert "CEO at Acme" in db.query(GtmLead).one().evidence
    play.sense_harvest(db, TENANT)
    assert len(harvested) == 1, "a post whose commenters were already bought is never bought again"
