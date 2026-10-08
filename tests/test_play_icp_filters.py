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
    from app.gtm_os.sourcing.models import IcpExclusion, IcpTermResolution, ProviderTaxonomyValue
    from app.gtm_os.strategy.strategy import GtmStrategy

    db = db_factory([Parameter, ProviderSpend, GtmLead, Batch, Company, Contact, CampaignPush, GtmSignal,
                     ProblemHypothesis, DemandHypothesis, Opportunity, GtmStrategy, MessageDraft,
                     ProviderTaxonomyValue, IcpTermResolution, IcpExclusion])
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
    # ICP fixture has no 'industries' set -- no include key should be added.
    assert "include" not in filters["industry"]


def test_icypeas_filters_include_the_partners_own_industries_when_set():
    # Real bug, 2026-10-04: a partner's stated industries was silently dropped from the real
    # search -- found live when Majji's "Professional Services" ICP returned hospitals, law
    # firms, construction, and manufacturing because nothing told Icypeas to only include it.
    icp = {**ICP, "industries": ["Professional Services"]}
    filters = play.icypeas_filters_for_icp(icp)
    assert filters["industry"]["include"] == ["Professional Services"]
    # The vendor-exclude safety net must still apply underneath a partner's own industry list.
    assert "Staffing and Recruiting" in filters["industry"]["exclude"]


def test_looks_like_government_or_education_catches_real_leaked_examples():
    # Real leak, found live 2026-10-03: these three slipped past Icypeas' own type.exclude
    # despite Government Agency/Educational Institution being excluded server-side.
    assert play._looks_like_government_or_education("Town of Rockport", None) is True
    assert play._looks_like_government_or_education("Longboat Key Fire Rescue", "Public Safety") is True
    assert play._looks_like_government_or_education("Lakeland Elementary Schools", "Education Management") is True
    # A real target company must not get caught by either signal.
    assert play._looks_like_government_or_education("Acme Consulting Group", "Law Practice") is False
    assert play._looks_like_government_or_education("Westfield Partners LLC", None) is False


def test_search_icypeas_rejects_government_and_education_bodies_before_any_paid_lookup(db, monkeypatch):
    import app.deepline_client as dc

    page = {"toolResponse": {"raw": {"leads": [
        {"url": "https://www.linkedin.com/company/townofrockport", "name": "Town of Rockport"},
        {"url": "https://www.linkedin.com/company/lakelandschools", "name": "Lakeland Elementary Schools",
         "industry": "Education Management"},
    ], "pagination": {}}}}
    monkeypatch.setattr(dc, "execute_tool", lambda tool, payload: page)

    result = play.search_icypeas(db, PARTNER, ICP, pages=1)

    assert result["outcomes"].get("government_or_education") == 2
    assert db.query(Company).count() == 0
    rejected = db.query(GtmLead).filter(GtmLead.state == play.STATE_REJECTED).all()
    assert len(rejected) == 2
    assert all("government/education" in (r.qualifier_reason or "") for r in rejected)



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


# ------------------------------------------------------------------ search_crustdata (2026-10-08, 2nd adapter)

def _crustdata_company(n, name=None, industry="Medical Device", employees=60,
                       revenue_lo=10_000_000, revenue_hi=50_000_000):
    """A realistic row in CRUSTDATA'S OWN field shape (basic_info/headcount/revenue/locations
    nesting), confirmed live 2026-10-08 -- deliberately NOT the Icypeas row shape, so this test
    exercises _normalize_crustdata_row's real translation rather than assuming it away."""
    return {
        "basic_info": {"name": name or f"Co{n}", "professional_network_url": f"https://www.linkedin.com/company/co{n}/",
                       "industries": [industry], "website": f"https://co{n}.com"},
        "headcount": {"total": employees},
        "revenue": {"estimated": {"lower_bound_usd": revenue_lo, "upper_bound_usd": revenue_hi}},
        "locations": {"country": "United States", "headquarters": "Ohio, United States"},
    }


NORA_ICP = {"employee_min": 11, "employee_max": 500, "industries": ["medical devices"],
           "geographies": ["United States"], "revenue_min_usd": 10_000_000, "revenue_max_usd": 100_000_000}


def test_search_crustdata_resolves_industry_then_creates_a_lead(db, monkeypatch):
    """End-to-end simulation BEFORE any further live spend, per explicit instruction: resolve
    industry via the mocked free autocomplete, search via the mocked paid endpoint, create a
    company and lead via the SAME shared helpers search_icypeas uses.

    The autocomplete mock returns "Medical Device" (singular, the REAL confirmed Crustdata
    value) for the partner's "medical devices" (plural) -- the exact mismatch found live. That
    requires the LLM-expansion step to bridge it, same as it would in production, so the LLM is
    mocked here too rather than simplified away -- this test would otherwise pass for the wrong
    reason."""
    import app.deepline_client as dc
    import app.llm_client as llm
    import app.phases.decision_maker_reasoning as dmr
    import app.phases.free_decision_maker as fdm

    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {"values": ["Medical Device"]})
    autocomplete_calls = []

    def fake_cli(tool, payload):
        if tool == "crustdata_v3_company_search_autocomplete":
            autocomplete_calls.append(payload["query"])
            return {"toolResponse": {"raw": {"suggestions": [{"value": "Medical Device"}]}}}
        if tool == "crustdata_v3_company_search":
            # The resolved condition must carry the REAL taxonomy value, never the partner's
            # own raw wording -- assert on the actual payload sent, not just the final count.
            conditions = payload["filters"]["conditions"]
            industry_cond = next(c for c in conditions if c["field"] == "basic_info.industries")
            assert industry_cond["value"] == ["Medical Device"]
            return {"toolResponse": {"raw": {"companies": [_crustdata_company(1)], "next_cursor": None}}}
        raise AssertionError(f"unexpected tool: {tool}")

    monkeypatch.setattr(dc, "_call_deepline_cli", fake_cli)
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: [_jobo_person()])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, company, cands, n, offering_name=None: [
                            {"name": "Jane Doe", "thread_role": "founder_ceo", "reasoning": "CEO"}])

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search_crustdata(db, PARTNER, NORA_ICP)

    assert autocomplete_calls == ["medical devices"]
    assert result["outcomes"] == {"created": 1}
    lead = db.query(GtmLead).one()
    assert lead.state == "signal" and lead.person_name == "Jane Doe"
    company = db.get(Company, lead.company_id)
    assert company.name == "Co1"
    assert company.source == "crustdata-v3:company_search"   # never mislabeled as icypeas
    assert company.employee_count == 60
    assert company.estimated_revenue_lower_usd == 10_000_000  # raw USD, unit multiplier stays 1x
    assert db.query(Batch).get(company.batch_id).tenant_id == PARTNER


def test_search_crustdata_drops_an_unresolved_industry_term_rather_than_sending_it_raw(db, monkeypatch):
    """The exact bug found live: a term the autocomplete can't match (e.g. 'life science')
    must never be sent to the real search as a literal -- it should be dropped, broadening the
    search instead of guaranteeing a zero-match filter."""
    import app.deepline_client as dc

    def fake_cli(tool, payload):
        if tool == "crustdata_v3_company_search_autocomplete":
            return {"toolResponse": {"raw": {"suggestions": []}}}  # confirmed real: no match
        if tool == "crustdata_v3_company_search":
            fields = [c["field"] for c in payload["filters"]["conditions"]]
            assert "basic_info.industries" not in fields
            return {"toolResponse": {"raw": {"companies": [], "next_cursor": None}}}
        raise AssertionError(f"unexpected tool: {tool}")

    monkeypatch.setattr(dc, "_call_deepline_cli", fake_cli)
    icp = {**NORA_ICP, "industries": ["life science"]}

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search_crustdata(db, PARTNER, icp)

    assert result["companies"] == 0
    assert result["exhausted"] is True


def test_search_crustdata_pagination_uses_next_cursor_not_a_token(db, monkeypatch):
    # Crustdata's own pagination field is `next_cursor`, a different shape from Icypeas'
    # `pagination.token` -- pinned so a future edit can't silently merge the two cursor shapes.
    import app.deepline_client as dc

    calls = []

    def fake_cli(tool, payload):
        if tool == "crustdata_v3_company_search_autocomplete":
            return {"toolResponse": {"raw": {"suggestions": [{"value": "Medical Device"}]}}}
        calls.append(payload)
        if len(calls) == 1:
            return {"toolResponse": {"raw": {"companies": [_crustdata_company(1)], "next_cursor": "abc123"}}}
        return {"toolResponse": {"raw": {"companies": [_crustdata_company(2)], "next_cursor": None}}}

    monkeypatch.setattr(dc, "_call_deepline_cli", fake_cli)
    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        play.search_crustdata(db, PARTNER, NORA_ICP, pages=2)

    assert calls[1]["cursor"] == "abc123"
    cursor_param = db.query(Parameter).filter(
        Parameter.tenant_id == PARTNER, Parameter.key == f"{play.CURSOR_KEY}:crustdata-v3").one()
    assert cursor_param.value["next_cursor"] is None  # exhausted on page 2


def test_search_crustdata_cursor_is_independent_of_icypeas_cursor(db, monkeypatch):
    """The real clobbering risk this fixed: before _cursor() took a provider param, a second
    adapter's resume state would have overwritten Icypeas' live one under the same key."""
    import app.deepline_client as dc

    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"leads": [], "pagination": {"token": "icypeas-token"}}}})
    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        play.search_icypeas(db, PARTNER, ICP)

    def fake_cli(tool, payload):
        if tool == "crustdata_v3_company_search_autocomplete":
            return {"toolResponse": {"raw": {"suggestions": []}}}
        return {"toolResponse": {"raw": {"companies": [], "next_cursor": None}}}

    monkeypatch.setattr(dc, "_call_deepline_cli", fake_cli)
    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        play.search_crustdata(db, PARTNER, NORA_ICP)

    icypeas_cursor = db.query(Parameter).filter(
        Parameter.tenant_id == PARTNER, Parameter.key == play.CURSOR_KEY).one()
    assert icypeas_cursor.value["token"] == "icypeas-token"  # untouched by the Crustdata run


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
                        lambda db_, t, company, cands, n, offering_name=None: [{"name": cands[0]["name"], "thread_role": "founder_ceo", "reasoning": "CEO"}])

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


def test_search_icypeas_free_count_check_stops_before_any_paid_page_on_a_confirmed_zero(db, monkeypatch):
    # Real gap, found live 2026-10-04: a filter set that doesn't actually match Icypeas' real
    # taxonomy (e.g. a partner's own wording for an industry) was only ever discovered by paying
    # $0.175 for an empty page. icypeas_count_companies is priced free for exactly this check.
    import app.deepline_client as dc

    calls = []

    def fake_cli(tool, payload):
        calls.append(tool)
        if tool == "icypeas_count_companies":
            return {"toolResponse": {"raw": {"count": 0}}}
        raise AssertionError(f"must not call a paid tool after a confirmed-zero free count: {tool}")

    monkeypatch.setattr(dc, "_call_deepline_cli", fake_cli)

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search_icypeas(db, PARTNER, ICP)

    assert calls == ["icypeas_count_companies"]
    assert result["exhausted"] is True
    assert result["free_count_checked"] == 0
    assert db.query(Company).count() == 0


def test_search_icypeas_free_count_check_never_blocks_on_an_unparseable_response(db, monkeypatch):
    """An unexpected response SHAPE from the free count check must never be read as zero and
    silently stop a real search -- it falls through to the real, paid page exactly as before."""
    import app.deepline_client as dc
    import app.phases.decision_maker_reasoning as dmr
    import app.phases.free_decision_maker as fdm

    def fake_cli(tool, payload):
        if tool == "icypeas_count_companies":
            return {"toolResponse": {"raw": {"somethingElse": 42}}}
        return {"toolResponse": {"raw": {"leads": [_icypeas_company(1)], "pagination": {"token": None}}}}

    monkeypatch.setattr(dc, "_call_deepline_cli", fake_cli)
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: [_jobo_person()])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, company, cands, n, offering_name=None: [{"name": "Jane Doe", "thread_role": "founder_ceo", "reasoning": "CEO"}])

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        result = play.search_icypeas(db, PARTNER, ICP)

    assert result["outcomes"] == {"created": 1}
    assert db.query(Company).count() == 1


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
    # 2, not 1: the first run now makes one real FREE icypeas_count_companies pre-check (2026-10-05
    # fix) before its one real paid page -- the mock doesn't distinguish tool name, so both land in
    # `calls`. The real invariant this test protects is still intact: the SECOND run makes zero
    # further calls of either kind, since it hits the pagination-exhaustion cooldown up front.
    assert len(calls) == 2, "the second run must not spend anything re-fetching an exhausted pool"
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

    def flaky(db_, tenant_id, co, known_leads, pending, department_atoms=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OperationalError("SELECT", {}, Exception("SSL SYSCALL error: Operation timed out"))
        return real(db_, tenant_id, co, known_leads, pending, department_atoms=department_atoms)

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
                        lambda db_, t, comp, cands, n, offering_name=None: [{"name": cands[0]["name"], "thread_role": "founder_ceo", "reasoning": "CEO"}])

    with spend_scope(db, BILLING, "icp_filters", run_cap_usd=0.5):
        resolved = play._resolve_decision_makers_batch(db, PARTNER, [company], ["CEO"])

    assert resolved[company.id] is not None
    assert resolved[company.id].first_name == "Sam"
