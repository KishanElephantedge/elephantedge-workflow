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


# ------------------------------------------------------------------ search_icypeas (2026-09-28 default)

def _icypeas_company(n, name=None, industry="Manufacturing", employees=60):
    return {"name": name or f"Co{n}", "url": f"https://www.linkedin.com/company/co{n}/", "industry": industry,
            "numberOfEmployees": employees, "address": "Ohio, United States", "website": f"https://co{n}.com",
            "description": "A real company.", "specialties": [{"value": "widgets"}]}


def _jobo_person(name="Jane Doe", title="CEO", linkedin=True):
    return {"name": name, "title": title, "linkedin_url": "https://www.linkedin.com/in/jane-doe" if linkedin else "https://www.crunchbase.com/person/jane-doe"}


def test_icypeas_filters_use_exact_headcount_and_exclude_vendors_and_non_companies():
    filters = play.icypeas_filters_for_icp(ICP)
    assert filters["headcount"] == {">=": 30, "<=": 100}
    assert "Staffing and Recruiting" in filters["industry"]["exclude"]
    assert "Educational Institution" in filters["type"]["exclude"]
    assert filters["location"] == {"include": ["United States"]}


def test_search_icypeas_creates_a_lead_with_the_free_jobo_decision_maker(db, monkeypatch):
    import app.deepline_client as dc
    import app.phases.decision_maker_reasoning as dmr
    import app.phases.free_decision_maker as fdm

    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"leads": [_icypeas_company(1)], "pagination": {"token": None}}}})
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: [_jobo_person()])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, company, cands, n, offering_name=None: [{"name": "Jane Doe", "thread_role": "founder_ceo", "reasoning": "CEO"}])

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search_icypeas(db, PARTNER, ICP)

    assert result["outcomes"] == {"created": 1}
    lead = db.query(GtmLead).one()
    assert lead.state == "signal" and lead.person_name == "Jane Doe"
    company = db.get(Company, lead.company_id)
    assert db.query(Batch).get(company.batch_id).tenant_id == PARTNER
    contact = db.get(Contact, lead.contact_id)
    assert contact.linkedin_url == "https://www.linkedin.com/in/jane-doe"
    assert {r.tenant_id for r in db.query(ProviderSpend)} == {BILLING}


def test_search_icypeas_skips_a_company_when_both_free_and_paid_resolution_miss(db, monkeypatch):
    import app.deepline_client as dc
    import app.phases.free_decision_maker as fdm

    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"leads": [_icypeas_company(1)], "pagination": {"token": None}}}})
    # Jobo has a leader, but only a Crunchbase URL -- not usable for real LinkedIn outreach.
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: [_jobo_person(linkedin=False)])
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [])  # the paid fallback also finds nobody

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search_icypeas(db, PARTNER, ICP)

    assert result["outcomes"] == {"no_decision_maker": 1}
    assert db.query(GtmLead).count() == 0


def test_search_icypeas_falls_back_to_the_paid_batched_resolver_when_jobo_misses(db, monkeypatch):
    """Real bug found live 2026-09-28: the free-only design found a usable decision maker for
    0 of 25 real companies in the first live test. Now falls back to one batched, paid
    HarvestAPI search covering every company that missed free resolution."""
    import app.deepline_client as dc
    import app.phases.decision_maker_reasoning as dmr
    import app.phases.free_decision_maker as fdm

    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"leads": [_icypeas_company(1, name="Widgetco")], "pagination": {"token": None}}}})
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: [])  # nothing free at all
    calls = []
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: calls.append(f) or ([
        {"first_name": "Sam", "last_name": "Lee", "linkedin_url": "https://linkedin.com/in/sam-lee", "title": "CEO",
         "company_name": "Widgetco", "company_linkedin_url": "https://www.linkedin.com/company/co1/"}] if page == 1 else []))
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, company, cands, n: [{"name": cands[0]["name"], "thread_role": "founder_ceo", "reasoning": "CEO"}])

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search_icypeas(db, PARTNER, ICP)

    assert result["outcomes"] == {"created": 1}
    assert "currentCompanies" in calls[0] and "Widgetco" in calls[0]["currentCompanies"]
    lead = db.query(GtmLead).one()
    assert lead.person_name == "Sam Lee"
    contact = db.get(Contact, lead.contact_id)
    assert contact.linkedin_url == "https://linkedin.com/in/sam-lee"


def test_search_icypeas_rejects_a_vendor_name_before_any_jobo_lookup(db, monkeypatch):
    import app.deepline_client as dc
    import app.phases.free_decision_maker as fdm

    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"leads": [_icypeas_company(1, name="Acme Staffing Agency")], "pagination": {"token": None}}}})
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: pytest.fail("must not look up a vendor"))

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search_icypeas(db, PARTNER, ICP)

    assert result["outcomes"] == {"vendor_name_match": 1}
    assert db.query(GtmLead).one().state == "rejected"


def test_search_icypeas_stores_the_real_revenue_estimate_for_free(db, monkeypatch):
    import app.deepline_client as dc
    import app.phases.decision_maker_reasoning as dmr
    import app.phases.free_decision_maker as fdm

    co = _icypeas_company(1)
    co["estimatedRevenuRange"] = {"estimatedMinRevenue": {"amount": 10, "unit": "MILLION", "currency": "USD"},
                                  "estimatedMaxRevenue": {"amount": 20, "unit": "MILLION", "currency": "USD"}}
    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"leads": [co], "pagination": {"token": None}}}})
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: [_jobo_person()])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, company, cands, n, offering_name=None: [{"name": "Jane Doe", "thread_role": "founder_ceo", "reasoning": "CEO"}])

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        play.search_icypeas(db, PARTNER, ICP)

    company = db.query(Company).one()
    assert (company.estimated_revenue_lower_usd, company.estimated_revenue_higher_usd) == (10_000_000, 20_000_000)


def test_search_icypeas_never_rechecks_a_known_company_within_one_page(db, monkeypatch):
    """A company that reappears (e.g. two pages overlapping) is skipped, not re-processed --
    but the exhaustion cooldown below is the real protection across separate runs."""
    import app.deepline_client as dc
    import app.phases.decision_maker_reasoning as dmr
    import app.phases.free_decision_maker as fdm

    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"leads": [_icypeas_company(1), _icypeas_company(1)], "pagination": {"token": "next"}}}})
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: [_jobo_person()])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, company, cands, n, offering_name=None: [{"name": "Jane Doe", "thread_role": "founder_ceo", "reasoning": "CEO"}])

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search_icypeas(db, PARTNER, ICP)

    assert result["outcomes"] == {"created": 1, "known": 1}
    assert db.query(GtmLead).count() == 1


def test_search_icypeas_stops_paying_once_the_pool_is_genuinely_exhausted(db, monkeypatch):
    """Real gap found live 2026-09-28 (Majji: 'what if these runs out after a few days'): once
    Icypeas itself says there is no next page, the run must not restart from page 1 next time
    and re-pay to re-see the same companies for zero new leads. It should cool down instead."""
    import app.deepline_client as dc
    import app.phases.decision_maker_reasoning as dmr
    import app.phases.free_decision_maker as fdm

    calls = []
    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: calls.append(1) or {
        "toolResponse": {"raw": {"leads": [_icypeas_company(1)], "pagination": {"token": None}}}})
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: [_jobo_person()])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, company, cands, n, offering_name=None: [{"name": "Jane Doe", "thread_role": "founder_ceo", "reasoning": "CEO"}])

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        first = play.search_icypeas(db, PARTNER, ICP)
        second = play.search_icypeas(db, PARTNER, ICP)

    assert first["exhausted"] is True
    assert len(calls) == 1, "the second run must not spend anything re-fetching an exhausted pool"
    assert second["stopped"] and "exhausted" in second["stopped"]
    cursor = db.query(Parameter).filter(Parameter.tenant_id == PARTNER, Parameter.key == play.CURSOR_KEY).one()
    assert "exhausted_at" in cursor.value


# ------------------------------------------------------------------ loosened Qualifier (2026-09-28)

def test_qualifier_passes_on_qualified_true_with_no_score_gate(db, monkeypatch):
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_person(1, "Good")])
    monkeypatch.setattr(h, "get_company", lambda u: {"name": "Good", "employee_count": 60, "industry": "Manufacturing",
                                                     "hq_text": "", "website": "https://good.com", "description": "", "linkedin_url": None})
    # No icp_fit_score at all in the verdict -- must still pass on qualified=True alone.
    monkeypatch.setattr(llm_client, "generate_json", lambda prompt, db, t, max_tokens=0: {"qualified": True, "reason": "clearly fits"})
    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        play.search(db, PARTNER, ICP)
    assert play.qualify(db, PARTNER, ICP)["qualified"] == 1
    assert db.query(GtmLead).one().state == "contact_found"


def test_search_icypeas_survives_a_dropped_connection_mid_page_and_saves_the_cursor_first(db, monkeypatch):
    """Real bug found live 2026-09-28: a mid-run Neon connection drop crashed the per-company
    loop after the page was already paid for, and the cursor only saved at the very end -- so
    the next run re-paid to re-fetch the exact same page. Now the cursor saves as soon as the
    paid page is in hand, and one company's DB error doesn't lose the rest of the page."""
    import app.deepline_client as dc
    import app.gtm_os.plays.icp_filters as icp_filters_module
    import app.phases.decision_maker_reasoning as dmr
    import app.phases.free_decision_maker as fdm
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"leads": [_icypeas_company(1), _icypeas_company(2)], "pagination": {"token": "next-page"}}}})
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: [_jobo_person()])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, company, cands, n, offering_name=None: [{"name": "Jane Doe", "thread_role": "founder_ceo", "reasoning": "CEO"}])

    real = icp_filters_module._process_icypeas_company
    calls = {"n": 0}

    def flaky(db_, tenant_id, co, known_leads, pending):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OperationalError("SELECT", {}, Exception("SSL SYSCALL error: Operation timed out"))
        return real(db_, tenant_id, co, known_leads, pending)

    monkeypatch.setattr(icp_filters_module, "_process_icypeas_company", flaky)

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search_icypeas(db, PARTNER, ICP)

    assert result["outcomes"]["created"] == 1
    assert result["outcomes"]["db_error_retry_later"] == 1
    cursor = db.query(Parameter).filter(Parameter.tenant_id == PARTNER, Parameter.key == play.CURSOR_KEY).one()
    assert cursor.value["token"] == "next-page", "the page's token must be saved even though a company in it failed"


def test_resolve_decision_makers_batch_keeps_earlier_pages_when_a_later_page_is_budget_blocked(db, monkeypatch):
    """Real bug found live 2026-09-28: a budget refusal on page 3 crashed the whole function,
    discarding page 1's and page 2's already-paid-for results ($0.14 spent, found nothing kept).
    Now the earlier pages' real matches are still used."""
    import app.deepline_client as dc
    import app.phases.decision_maker_reasoning as dmr

    from app.gtm_os.plays.icp_filters import _batch
    batch = _batch(db, PARTNER)
    company = Company(batch_id=batch.id, name="Good", linkedin_url="https://www.linkedin.com/company/good/")
    db.add(company)
    db.commit()

    call_n = {"n": 0}

    def flaky_search(page=1, **f):
        call_n["n"] += 1
        if page == 1:
            return [{"first_name": "Sam", "last_name": "Lee", "linkedin_url": "https://linkedin.com/in/sam-lee", "title": "CEO",
                     "company_name": "Good", "company_linkedin_url": "https://www.linkedin.com/company/good/"}] + \
                   [{"first_name": f"Noise{i}", "last_name": "X", "linkedin_url": f"https://linkedin.com/in/n{i}", "title": "CEO",
                     "company_name": "Other", "company_linkedin_url": "https://www.linkedin.com/company/other/"} for i in range(24)]
        raise dc.DeeplineSpendBlocked("run cap reached")

    monkeypatch.setattr(h, "search_leads", flaky_search)
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, comp, cands, n: [{"name": cands[0]["name"], "thread_role": "founder_ceo", "reasoning": "CEO"}])

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        resolved = play._resolve_decision_makers_batch(db, PARTNER, [company], ["CEO"])

    assert resolved[company.id] is not None
    assert resolved[company.id].first_name == "Sam"
