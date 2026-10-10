"""Phase 9: the shared decision-maker resolver, tested directly (not just through either play).

Both icp_filters.py and hiring.py now call this module instead of maintaining their own, separate
copies. Their own test suites already exercise the shared name-batching and budget-resilience
fixes end to end; this file covers the two behaviors specific to unifying them that aren't visible
from either play alone.
"""
import pytest

import app.deepline_client as dc
from app.db.models import Batch, CampaignPush, Company, Contact, Parameter, Tenant
from app.gtm_os.sourcing.decision_maker import resolve_decision_makers_batch

TENANT = 2


@pytest.fixture(autouse=True)
def _crustdata_tier_always_misses(monkeypatch):
    """TIER 1 (crustdata_v3_person_search, added 2026-10-10) must never make a real subprocess/
    network call during a test -- every test in this file predates it and exercises TIER 2
    (HarvestAPI) behavior specifically, so tier 1 is made to cleanly find nobody, same as any
    other provider call in this codebase's tests."""
    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"profiles": []}}})


@pytest.fixture
def db(db_factory):
    db = db_factory([Tenant, Parameter, Company, Batch, Contact, CampaignPush])
    db.add(Tenant(id=TENANT, name="Elephant Edge", slug="ee"))
    db.commit()
    return db


def _company(db, name="Acme"):
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    c = Company(batch_id=batch.id, name=name, linkedin_url=f"https://www.linkedin.com/company/{name.lower()}/")
    db.add(c)
    db.commit()
    return c


def _found_person(**over):
    base = {"first_name": "Sam", "last_name": "Lee", "linkedin_url": "https://linkedin.com/in/sam-lee",
           "title": "CEO", "company_name": "Acme", "company_linkedin_url": "https://www.linkedin.com/company/acme"}
    base.update(over)
    return base


def test_the_agents_own_thread_role_wins_over_the_default(db, monkeypatch):
    """A previous version of icp_filters.py's own copy of this logic IGNORED the agent's thread_
    role pick and hardcoded a single label regardless of what the agent actually determined. The
    shared resolver restores the richer, already-correct behavior hiring.py had: the agent's pick
    is used whenever it supplies one."""
    import app.harvestapi as h
    import app.phases.decision_maker_reasoning as dmr

    company = _company(db)
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_found_person()] if page == 1 else [])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, c, cands, n, offering_name=None: [
                            {"name": "Sam Lee", "thread_role": "founder_ceo", "reasoning": "CEO"}])

    out = resolve_decision_makers_batch(db, TENANT, [company], ["CEO"],
                                        default_thread_role="should_not_be_used",
                                        reasoning_label="test")
    assert out[company.id].thread_role == "founder_ceo"


def test_the_default_thread_role_is_used_only_when_the_agent_supplies_none(db, monkeypatch):
    import app.harvestapi as h
    import app.phases.decision_maker_reasoning as dmr

    company = _company(db)
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_found_person()] if page == 1 else [])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, c, cands, n, offering_name=None: [
                            {"name": "Sam Lee", "reasoning": "CEO"}])   # no thread_role key at all

    out = resolve_decision_makers_batch(db, TENANT, [company], ["CEO"],
                                        default_thread_role="fallback_role", reasoning_label="test")
    assert out[company.id].thread_role == "fallback_role"


def test_offering_name_is_threaded_per_company_not_shared_across_the_batch(db, monkeypatch):
    import app.harvestapi as h
    import app.phases.decision_maker_reasoning as dmr

    acme = _company(db, "Acme")
    other = _company(db, "Globex")
    seen_offering_names = {}

    def fake_leads(page=1, **f):
        if page != 1:
            return []
        return [_found_person(), _found_person(company_name="Globex",
                                                company_linkedin_url="https://www.linkedin.com/company/globex",
                                                linkedin_url="https://linkedin.com/in/other")]

    def fake_select(db_, t, company, cands, n, offering_name=None):
        seen_offering_names[company.id] = offering_name
        return [{"name": cands[0]["name"], "reasoning": "x"}]

    monkeypatch.setattr(h, "search_leads", fake_leads)
    monkeypatch.setattr(dmr, "select_best_decision_makers", fake_select)

    resolve_decision_makers_batch(
        db, TENANT, [acme, other], ["CEO"], default_thread_role="x", reasoning_label="test",
        offering_name_for={acme.id: "Sales OS", other.id: "Playbook"})

    assert seen_offering_names[acme.id] == "Sales OS"
    assert seen_offering_names[other.id] == "Playbook"


def _crustdata_profile(name="Jim Smittkamp", title="Chief Revenue Officer", company="Acme",
                       linkedin="https://www.linkedin.com/in/jim-smittkamp"):
    return {
        "basic_profile": {"name": name, "current_title": title},
        "experience": {"employment_details": {"current": [{"company_name": company}]}},
        "social_handles": {"professional_network_identifier": {"profile_url": linkedin}},
    }


def test_crustdata_person_search_tier_resolves_without_ever_calling_harvestapi(db, monkeypatch):
    """TIER 1, 2026-10-10: when crustdata_v3_person_search finds a real candidate, the company
    must never reach TIER 2 at all -- the whole point of trying the $0.002/result tier first is
    that a resolved company doesn't pay HarvestAPI's $0.07/page on top of it."""
    import app.deepline_client as local_dc
    import app.harvestapi as h
    import app.phases.decision_maker_reasoning as dmr

    company = _company(db, "Acme")
    harvest_calls = []
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: harvest_calls.append(1) or [])
    monkeypatch.setattr(local_dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"profiles": [_crustdata_profile()]}}})
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, c, cands, n, offering_name=None: [{"name": cands[0]["name"], "reasoning": "x"}])

    out = resolve_decision_makers_batch(
        db, TENANT, [company], ["Chief Revenue Officer"], default_thread_role="x", reasoning_label="test")

    assert harvest_calls == []  # tier 2 never ran
    contact = out[company.id]
    assert contact is not None
    assert contact.first_name == "Jim" and contact.last_name == "Smittkamp"
    assert contact.linkedin_url == "https://www.linkedin.com/in/jim-smittkamp"
    assert contact.title == "Chief Revenue Officer"


def test_crustdata_person_search_tier_miss_falls_through_to_harvestapi(db, monkeypatch):
    """A company tier 1 can't resolve must still reach tier 2 -- tier 1 is a cheaper FIRST try,
    never a replacement that silently drops coverage HarvestAPI could still provide."""
    import app.deepline_client as local_dc
    import app.harvestapi as h
    import app.phases.decision_maker_reasoning as dmr

    company = _company(db, "Acme")
    monkeypatch.setattr(local_dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"profiles": []}}})
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_found_person()] if page == 1 else [])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, c, cands, n, offering_name=None: [{"name": cands[0]["name"], "reasoning": "x"}])

    out = resolve_decision_makers_batch(
        db, TENANT, [company], ["CEO"], default_thread_role="x", reasoning_label="test")

    contact = out[company.id]
    assert contact is not None
    assert contact.first_name == "Sam" and contact.last_name == "Lee"  # the HarvestAPI tier's pick


def test_crustdata_person_search_tier_error_falls_through_to_harvestapi(db, monkeypatch):
    """A tier 1 outage (budget block, provider error) must degrade to tier 2, not abort the
    whole resolution -- the same resilience the existing HarvestAPI chunking already has."""
    import app.deepline_client as local_dc
    import app.harvestapi as h
    import app.phases.decision_maker_reasoning as dmr

    company = _company(db, "Acme")

    def failing_cli(tool, payload):
        if tool == "crustdata_v3_person_search":
            raise local_dc.DeeplineError("simulated outage")
        raise AssertionError(f"unexpected tool: {tool}")

    monkeypatch.setattr(local_dc, "_call_deepline_cli", failing_cli)
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_found_person()] if page == 1 else [])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, c, cands, n, offering_name=None: [{"name": cands[0]["name"], "reasoning": "x"}])

    out = resolve_decision_makers_batch(
        db, TENANT, [company], ["CEO"], default_thread_role="x", reasoning_label="test")

    assert out[company.id] is not None  # tier 2 still delivered despite tier 1's outage


def test_no_offering_name_for_mapping_passes_none_not_a_crash(db, monkeypatch):
    """icp_filters.py's own call site (company-first, no signal/lead yet) never has an offering
    name to pass -- the shared resolver must work with offering_name_for entirely absent."""
    import app.harvestapi as h
    import app.phases.decision_maker_reasoning as dmr

    company = _company(db)
    seen = {}
    monkeypatch.setattr(h, "search_leads", lambda page=1, **f: [_found_person()] if page == 1 else [])
    monkeypatch.setattr(dmr, "select_best_decision_makers",
                        lambda db_, t, c, cands, n, offering_name=None: seen.setdefault("v", offering_name) or
                        [{"name": cands[0]["name"], "reasoning": "x"}])

    resolve_decision_makers_batch(db, TENANT, [company], ["CEO"],
                                  default_thread_role="x", reasoning_label="test")
    assert seen["v"] is None
