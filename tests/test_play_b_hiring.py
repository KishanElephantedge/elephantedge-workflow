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

    def fake_find(company, db_, tenant_id, **kwargs):
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

    def fake_find(company, db_, tenant_id, **kwargs):
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


def test_company_with_an_earlier_opportunity_chain_is_reused_not_duplicated(db, monkeypatch):
    acme = _qualified(db, monkeypatch)
    db.add(Contact(company_id=acme.id, first_name="Jane", title="CEO", email="jane@acme.io"))
    old = ProblemHypothesis(tenant_id=TENANT, company_id=acme.id, company_name_raw="Acme", affected_function="sales",
                            problem_statement="old")
    db.add(old)
    db.commit()

    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        assert play.find_contacts(db, TENANT)["reused"] == 1
    assert db.query(GtmLead).one().state == "contact_found"
    assert db.query(ProblemHypothesis).count() == 1
    assert db.get(ProblemHypothesis, old.id).problem_statement == "No sales leadership."
    assert db.query(Opportunity).count() == 1


def test_linkedin_only_outreach_never_buys_an_email(db, monkeypatch):
    _qualified(db, monkeypatch)
    seen = {}

    def fake_find(company, db_, tenant_id, **kwargs):
        seen.update(kwargs)
        c = Contact(company_id=company.id, first_name="Sam", title="CEO", linkedin_url="https://linkedin.com/in/sam")
        db_.add(c)
        db_.commit()
        return [c], False

    monkeypatch.setattr(decision_maker, "find_decision_makers", fake_find)
    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        assert play.find_contacts(db, TENANT, channels=["linkedin"])["found"] == 1
    assert seen["resolve_email"] is False


def test_linkedin_only_skips_a_known_contact_with_no_linkedin(db, monkeypatch):
    acme = _qualified(db, monkeypatch)
    db.add(Contact(company_id=acme.id, first_name="Jane", email="jane@acme.io"))  # email only
    db.commit()
    monkeypatch.setattr(decision_maker, "find_decision_makers", lambda *a, **k: ([], False))
    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        assert play.find_contacts(db, TENANT, channels=["linkedin"])["missing"] == 1


# ------------------------------------------------------------------ HarvestAPI (LinkedIn) path

def _icp3(monkeypatch):
    import app.gtm_os.icp.icp_config as icp_config
    monkeypatch.setattr(icp_config, "get_icp_config", lambda db, t: [{
        "id": "icp_3", "name": "Needs Fractional Leadership", "revenue_min_usd": 20_000_000, "revenue_max_usd": 50_000_000,
        "employee_max": 300, "trigger_mode": "requires_presence", "trigger_hiring_roles": ["head_of_sales"], "enabled": True}])


def test_harvest_discovery_keeps_only_in_band_companies_and_never_rechecks(db, monkeypatch):
    import app.harvestapi as h
    _icp3(monkeypatch)
    jobs = [{"job_id": "1", "title": "VP Sales", "url": "u1", "posted_at": "2026-09-25T00:00:00Z", "company_name": "Good",
             "company_linkedin_url": "https://www.linkedin.com/company/good", "company_universal_name": "good", "location": "US"},
            {"job_id": "2", "title": "VP Sales", "url": "u2", "posted_at": None, "company_name": "Tiny",
             "company_linkedin_url": "https://www.linkedin.com/company/tiny", "company_universal_name": "tiny", "location": "US"}]
    monkeypatch.setattr(h, "search_jobs", lambda title, **k: jobs if title == "VP Sales" else [])
    lookups = []
    facts = {"good": {"name": "Good", "employee_count": 200, "industry": "Software Development", "hq_country": "US",
                      "hq_text": "Austin, TX, US", "website": "https://good.io", "description": "d", "linkedin_url": "https://www.linkedin.com/company/good"},
             "tiny": {"name": "Tiny", "employee_count": 12, "industry": "Software Development", "hq_country": "US",
                      "hq_text": "", "website": "https://tiny.io", "description": "", "linkedin_url": "https://www.linkedin.com/company/tiny"}}
    monkeypatch.setattr(h, "get_company", lambda u: lookups.append(u) or facts[u])
    monkeypatch.setattr(h, "get_job", lambda job_id: {"descriptionText": "Build our sales team."})

    result = play.sense_harvest(db, TENANT)
    assert (result["kept"], result["rejected"]) == (1, {"size_outside_icp": 1})
    company = db.query(Company).one()
    assert (company.name, company.domain, company.employee_count) == ("Good", "good.io", 200)
    signal = db.query(GtmSignal).one()
    assert signal.company_id == company.id and signal.extracted_info["description_text"] == "Build our sales team."

    play.sense_harvest(db, TENANT)
    assert sorted(lookups) == ["good", "tiny"], "a company already checked is never paid for again"


def test_harvest_contacts_one_search_for_many_companies_agent_picks(db, monkeypatch):
    import app.harvestapi as h
    import app.phases.decision_maker_reasoning as dmr
    for name in ("alpha", "beta"):
        c = _company(db, name.title(), f"{name}.io")
        c.linkedin_url = f"https://www.linkedin.com/company/{name}"
        db.commit()
        _posting(db, c, name)
    play.ingest_new_signals(db, TENANT)
    monkeypatch.setattr(llm_client, "generate_json", lambda *a, **k: _verdict())
    play.qualify_leads(db, TENANT)

    calls = []

    def fake_leads(page=1, **f):
        calls.append(f)
        return [{"first_name": "Ann", "last_name": "A", "linkedin_url": "https://linkedin.com/in/ann", "title": "CEO",
                 "company_name": "Alpha", "company_linkedin_url": "https://www.linkedin.com/company/alpha"},
                {"first_name": "Ned", "last_name": "N", "linkedin_url": "https://linkedin.com/in/ned", "title": "CEO",
                 "company_name": "Alpha Robotics", "company_linkedin_url": "https://www.linkedin.com/company/alpha-robotics"}]

    monkeypatch.setattr(h, "search_leads", fake_leads)
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db, t, company, cands, n, offering_name=None: [{"name": cands[0]["name"], "thread_role": "founder_ceo", "reasoning": "CEO"}])
    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        result = play.find_contacts(db, TENANT, channels=["linkedin"])

    # Phase 9, 2026-10-07: batched by company NAME now, not URL -- a confirmed live bug where
    # batching multiple company LinkedIn URLs together in one currentCompanies request silently
    # returned zero people. "Alpha,Beta" (names), not "alpha,beta" (URL slugs).
    assert len(calls) == 1 and "Alpha" in calls[0]["currentCompanies"] and "Beta" in calls[0]["currentCompanies"]
    assert (result["found"], result["missing"]) == (1, 1)
    alpha_lead = next(l for l in db.query(GtmLead) if db.get(Company, l.company_id).name == "Alpha")
    assert db.get(Contact, alpha_lead.contact_id).linkedin_url == "https://linkedin.com/in/ann"  # never Ned at a look-alike company


def test_harvest_discovery_survives_a_write_failure_mid_run_and_retries_that_company(db, monkeypatch):
    """A dropped DB connection while writing one company's Company/signal rows must not lose
    the run: the checked-companies state already committed for earlier companies is kept, the
    failing company is NOT marked checked (so it's retried next run), and later companies still
    get written."""
    import app.harvestapi as h
    from sqlalchemy.exc import OperationalError

    _icp3(monkeypatch)
    jobs = [{"job_id": str(n), "title": "VP Sales", "url": f"u{n}", "posted_at": "2026-09-25T00:00:00Z",
             "company_name": name, "company_linkedin_url": f"https://www.linkedin.com/company/{name.lower()}",
             "company_universal_name": name.lower(), "location": "US"}
            for n, name in ((1, "First"), (2, "Second"), (3, "Third"))]
    monkeypatch.setattr(h, "search_jobs", lambda title, **k: jobs if title == "VP Sales" else [])
    facts = {name.lower(): {"name": name, "employee_count": 200, "industry": "Software Development",
                            "hq_country": "US", "hq_text": "Austin, TX, US", "website": f"https://{name.lower()}.io",
                            "description": "d", "linkedin_url": f"https://www.linkedin.com/company/{name.lower()}"}
             for name in ("First", "Second", "Third")}
    monkeypatch.setattr(h, "get_company", lambda u: facts[u])
    monkeypatch.setattr(h, "get_job", lambda job_id: {"descriptionText": "Build our sales team."})

    real_keep_company = play._keep_company
    def flaky_keep_company(db_, tenant_id, u, cand, facts_, size, result):
        if u == "second":
            raise OperationalError("INSERT", {}, Exception("SSL SYSCALL error: Operation timed out"))
        return real_keep_company(db_, tenant_id, u, cand, facts_, size, result)
    monkeypatch.setattr(play, "_keep_company", flaky_keep_company)

    result = play.sense_harvest(db, TENANT)
    assert result["kept"] == 2
    assert result["write_errors"][0].startswith("second:")
    kept_names = {c.name for c in db.query(Company)}
    assert kept_names == {"First", "Third"}

    seen = db.query(Parameter).filter(Parameter.key == play.HARVEST_SEEN_KEY).one()
    assert seen.value["first"] == "kept" and seen.value["third"] == "kept"
    assert "second" not in seen.value, "the failed company is not marked checked, so it's retried"


def test_harvest_decision_makers_keeps_earlier_pages_when_a_later_page_is_budget_blocked(db, monkeypatch):
    """Phase 9, 2026-10-07: this play's own decision-maker resolver never had the fix
    icp_filters.py's got on 2026-09-28 -- a budget refusal on a later page used to crash the whole
    batch here, discarding whatever earlier pages had already been paid for and found. Now routed
    through the shared resolver (app/gtm_os/sourcing/decision_maker.py), which already keeps them."""
    import app.deepline_client as dc
    import app.harvestapi as h
    import app.phases.decision_maker_reasoning as dmr

    company = _company(db, "Good", "good.io")
    company.linkedin_url = "https://www.linkedin.com/company/good/"
    db.commit()
    lead = GtmLead(tenant_id=TENANT, play="hiring", lead_key=f"company:{company.id}",
                   state="qualified", company_id=company.id, qualifier_output={"matched_offering": "Sales OS"})
    db.add(lead)
    db.commit()

    def flaky_search(page=1, **f):
        if page == 1:
            return [{"first_name": "Sam", "last_name": "Lee", "linkedin_url": "https://linkedin.com/in/sam-lee",
                     "title": "CEO", "company_name": "Good", "company_linkedin_url": "https://www.linkedin.com/company/good/"}]
        raise dc.DeeplineSpendBlocked("run cap reached")

    monkeypatch.setattr(h, "search_leads", flaky_search)
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, comp, cands, n, offering_name=None: [
                            {"name": cands[0]["name"], "thread_role": "founder_ceo", "reasoning": "CEO"}])

    # A second company in the SAME batch (both well under NAME_BATCH_SIZE) with no candidates on
    # page 1. Both companies are still in the one chunk the page-2 failure interrupted, so the
    # shared resolver processes both with whatever partial data page 1 already gave it -- no
    # company is skipped entirely, so this must NOT raise. (A genuinely skipped LATER CHUNK, which
    # this function's own docstring says re-raises to preserve find_contacts' existing contract,
    # would need more than NAME_BATCH_SIZE companies to reproduce -- a different scenario.)
    other = _company(db, "Other", "other.io")
    other.linkedin_url = "https://www.linkedin.com/company/other-co/"
    db.commit()
    other_lead = GtmLead(tenant_id=TENANT, play="hiring", lead_key=f"company:{other.id}", state="qualified",
                         company_id=other.id)
    db.add(other_lead)
    db.commit()

    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        result = play._harvest_decision_makers(db, TENANT, [(lead, company), (other_lead, other)])

    # The page-1 result for "Good" was already committed inside the shared resolver before the
    # page-2 failure -- this is the actual bug: it used to be discarded by the crash that followed.
    contact = db.query(Contact).filter(Contact.company_id == company.id).one()
    assert contact.linkedin_url == "https://linkedin.com/in/sam-lee"
    assert result[company.id].id == contact.id
    assert result[other.id] is None   # genuinely attempted (same chunk), genuinely found nothing


def test_harvest_decision_makers_reraises_when_a_later_chunk_is_never_attempted(db, monkeypatch):
    """A genuinely skipped LATER CHUNK (beyond NAME_BATCH_SIZE=15 companies, so budget_stopped
    breaks the outer loop before that chunk is ever sliced/processed) still surfaces as
    DeeplineSpendBlocked -- find_contacts already has a tested, working contract for that."""
    import app.deepline_client as dc
    import app.harvestapi as h
    import app.phases.decision_maker_reasoning as dmr

    pairs = []
    for i in range(16):   # one more than NAME_BATCH_SIZE -- guarantees a second chunk
        c = _company(db, f"Co{i}", f"co{i}.io")
        c.linkedin_url = f"https://www.linkedin.com/company/co{i}/"
        db.commit()
        lead = GtmLead(tenant_id=TENANT, play="hiring", lead_key=f"company:{c.id}", state="qualified",
                       company_id=c.id)
        db.add(lead)
        db.commit()
        pairs.append((lead, c))

    def always_blocked(page=1, **f):
        raise dc.DeeplineSpendBlocked("run cap reached")

    monkeypatch.setattr(h, "search_leads", always_blocked)
    monkeypatch.setattr(dmr, "select_best_decision_makers", lambda *a, **k: [])

    with spend_scope(db, TENANT, "hiring", run_cap_usd=0.5):
        with pytest.raises(dc.DeeplineSpendBlocked):
            play._harvest_decision_makers(db, TENANT, pairs)
