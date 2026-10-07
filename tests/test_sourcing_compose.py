"""Phase 7: enforce an atom no provider can search, using data already fetched for free.

Majji's "no dedicated marketing hire" has sat in free-text notes since this session started,
enforced by nothing -- no registered provider can filter on department headcount. This is what
finally closes that gap, without a new paid integration: the free Jobo leadership list, already
fetched during decision-maker resolution, decides it too.
"""
from app.gtm_os.sourcing import atoms as A
from app.gtm_os.sourcing import compose as C

NO_MARKETING_HIRE = A.Atom(A.DEPARTMENT_HEADCOUNT, A.RANGE, (None, 0), qualifier="marketing")
WANTS_SALES_HIRE = A.Atom(A.DEPARTMENT_HEADCOUNT, A.RANGE, (1, None), qualifier="sales")


def _person(name, title):
    return {"name": name, "title": title}


# ---- the core asymmetry: absence of evidence is never evidence of absence ----

def test_an_empty_leadership_list_cannot_tell_either_way():
    result = C.department_presence("marketing", [])
    assert result.satisfied is None


def test_titles_present_but_none_matching_still_cannot_confirm_absence():
    """Jobo's leadership list is a partial index, not the whole company -- no marketing title
    in what Jobo happens to have is not proof no one does that job."""
    people = [_person("Sam Lee", "CEO"), _person("Jordan Kim", "CFO")]
    result = C.department_presence("marketing", people)
    assert result.satisfied is None


def test_a_real_title_match_is_confident_positive_evidence():
    people = [_person("Sam Lee", "CEO"), _person("Taylor Rivers", "VP of Marketing")]
    result = C.department_presence("marketing", people)
    assert result.satisfied is True
    assert "Taylor Rivers" in result.evidence


def test_the_pattern_is_specific_not_a_substring_trap():
    # "Market" appearing inside an unrelated word must not false-positive.
    people = [_person("Alex Chen", "VP of Supermarket Partnerships")]
    result = C.department_presence("marketing", people)
    assert result.satisfied is None


# ---- enrich_to_decide: reads the atom's own bounds, serves both directions ----

def test_no_marketing_title_found_is_inconclusive_not_confirmed_satisfied():
    """The same asymmetry, from the other side: not finding a marketing title in Jobo's partial
    index is not proof no one has that job, so "no dedicated marketing hire" can be DISCONFIRMED
    by a real title match, but it can never be CONFIRMED by an absence. Only a positive sighting
    is ever confident evidence here."""
    people = [_person("Sam Lee", "CEO"), _person("Jordan Kim", "Head of Sales")]
    decision = C.enrich_to_decide(NO_MARKETING_HIRE, people)
    assert decision.satisfied is None


def test_no_marketing_hire_is_violated_when_a_marketing_title_is_found():
    people = [_person("Sam Lee", "CEO"), _person("Taylor Rivers", "Director of Marketing")]
    decision = C.enrich_to_decide(NO_MARKETING_HIRE, people)
    assert decision.satisfied is False
    assert "Taylor Rivers" in decision.evidence


def test_a_wants_hire_atom_is_satisfied_by_the_opposite_evidence():
    people = [_person("Jordan Kim", "VP of Sales")]
    decision = C.enrich_to_decide(WANTS_SALES_HIRE, people)
    assert decision.satisfied is True


def test_inconclusive_evidence_never_rejects_a_company():
    decision = C.enrich_to_decide(NO_MARKETING_HIRE, [])
    assert decision.satisfied is None     # caller must treat None as "do not reject"


def test_an_unrelated_atom_is_not_decided_by_this_module():
    headcount_atom = A.Atom(A.HEADCOUNT, A.RANGE, (11, 50))
    assert C.enrich_to_decide(headcount_atom, [_person("X", "CEO")]).satisfied is None


# ---- intersect: identity-keyed, never name-matched ----

def test_intersect_keeps_only_companies_both_sides_found():
    a = [{"id": "li:acme", "name": "Acme"}, {"id": "li:other", "name": "Other"}]
    b = [{"id": "li:acme", "revenue": 3_000_000}]
    merged = C.intersect(a, b, identity_of=lambda r: r.get("id"))
    assert [r["name"] for r in merged] == ["Acme"]


def test_intersect_merges_fields_the_first_side_lacked():
    a = [{"id": "li:acme", "name": "Acme", "revenue": None}]
    b = [{"id": "li:acme", "revenue": 3_000_000}]
    merged = C.intersect(a, b, identity_of=lambda r: r.get("id"))
    assert merged[0]["revenue"] == 3_000_000


def test_intersect_never_lets_the_second_side_overwrite_a_real_value():
    a = [{"id": "li:acme", "name": "Acme", "revenue": 1_000_000}]
    b = [{"id": "li:acme", "revenue": 9_999_999}]
    merged = C.intersect(a, b, identity_of=lambda r: r.get("id"))
    assert merged[0]["revenue"] == 1_000_000


def test_intersect_does_not_match_on_name_a_confirmed_real_failure_mode():
    # Two different real companies named "Acme" must never be treated as the same row.
    a = [{"id": "li:acme-123", "name": "Acme"}]
    b = [{"id": "li:acme-456", "name": "Acme"}]
    assert C.intersect(a, b, identity_of=lambda r: r.get("id")) == []


# ---- live wiring: this is what actually closes Majji's gap ----

def test_live_search_rejects_a_company_with_a_confirmed_marketing_hire(db_factory, monkeypatch):
    import app.deepline_client as dc
    import app.phases.free_decision_maker as fdm
    from app.db.models import Batch, CampaignPush, Company, Contact, Parameter, Tenant
    from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
    from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
    from app.gtm_os.learning.message_draft import MessageDraft
    from app.gtm_os.opportunity.opportunity import Opportunity
    from app.gtm_os.orchestration.control import DEFAULT_GTM_OS_CONTROL_CONFIG, set_control_config
    from app.gtm_os.plays import icp_filters as play
    from app.gtm_os.plays.lead import GtmLead
    from app.gtm_os.strategy.strategy import GtmStrategy
    from app.spend_ledger import ProviderSpend

    PARTNER, BILLING = 15, 2
    db = db_factory([Tenant, Parameter, ProviderSpend, GtmLead, Batch, Company, Contact,
                     CampaignPush, ProblemHypothesis, DemandHypothesis, Opportunity, GtmStrategy,
                     MessageDraft])
    db.add(Tenant(id=BILLING, name="Elephant Edge", slug="ee"))
    db.add(Tenant(id=PARTNER, name="Majji", slug="majji"))
    config = DEFAULT_GTM_OS_CONTROL_CONFIG.copy()
    config["spend"] = {"daily_cap_usd": 1.0, "run_cap_usd": 0.5}
    set_control_config(db, BILLING, config)
    db.commit()

    co = {"url": "https://www.linkedin.com/company/acme", "name": "Acme Co",
         "numberOfEmployees": 25, "industry": "Professional Services"}
    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"leads": [co], "pagination": {"token": None}}}})
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: [
        {"name": "Sam Lee", "title": "CEO"},
        {"name": "Taylor Rivers", "title": "Director of Marketing"},
    ])

    icp = {"employee_min": 11, "employee_max": 50,
          "department_headcount": {"marketing": {"max": 0}}}
    result = play.search_icypeas(db, PARTNER, icp)

    assert result["outcomes"].get("department_requirement_failed:marketing") == 1
    lead = db.query(GtmLead).filter(GtmLead.tenant_id == PARTNER).one()
    assert lead.state == "rejected"
    assert "marketing" in lead.qualifier_reason.lower()


def test_live_search_accepts_a_company_with_no_marketing_title_found(db_factory, monkeypatch):
    import app.deepline_client as dc
    import app.phases.decision_maker_reasoning as dmr
    import app.phases.free_decision_maker as fdm
    from app.db.models import Batch, CampaignPush, Company, Contact, Parameter, Tenant
    from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
    from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
    from app.gtm_os.learning.message_draft import MessageDraft
    from app.gtm_os.opportunity.opportunity import Opportunity
    from app.gtm_os.orchestration.control import DEFAULT_GTM_OS_CONTROL_CONFIG, set_control_config
    from app.gtm_os.plays import icp_filters as play
    from app.gtm_os.plays.lead import GtmLead
    from app.gtm_os.strategy.strategy import GtmStrategy
    from app.spend_ledger import ProviderSpend

    PARTNER, BILLING = 15, 2
    db = db_factory([Tenant, Parameter, ProviderSpend, GtmLead, Batch, Company, Contact,
                     CampaignPush, ProblemHypothesis, DemandHypothesis, Opportunity, GtmStrategy,
                     MessageDraft])
    db.add(Tenant(id=BILLING, name="Elephant Edge", slug="ee"))
    db.add(Tenant(id=PARTNER, name="Majji", slug="majji"))
    config = DEFAULT_GTM_OS_CONTROL_CONFIG.copy()
    config["spend"] = {"daily_cap_usd": 1.0, "run_cap_usd": 0.5}
    set_control_config(db, BILLING, config)
    db.commit()

    co = {"url": "https://www.linkedin.com/company/acme", "name": "Acme Co",
         "numberOfEmployees": 25, "industry": "Professional Services"}
    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"leads": [co], "pagination": {"token": None}}}})
    monkeypatch.setattr(fdm, "_jobo_leadership_candidates", lambda db_, t, company: [
        {"name": "Sam Lee", "title": "CEO", "linkedin_url": "https://www.linkedin.com/in/sam-lee"},
    ])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, company, cands, n, offering_name=None: [
                            {"name": "Sam Lee", "thread_role": "founder_ceo", "reasoning": "CEO"}])

    icp = {"employee_min": 11, "employee_max": 50,
          "department_headcount": {"marketing": {"max": 0}}}
    result = play.search_icypeas(db, PARTNER, icp)

    assert "department_requirement_failed:marketing" not in result["outcomes"]
    assert result["created"] == 1
