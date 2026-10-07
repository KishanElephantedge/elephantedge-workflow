"""Phase 9: the shared decision-maker resolver, tested directly (not just through either play).

Both icp_filters.py and hiring.py now call this module instead of maintaining their own, separate
copies. Their own test suites already exercise the shared name-batching and budget-resilience
fixes end to end; this file covers the two behaviors specific to unifying them that aren't visible
from either play alone.
"""
import pytest

from app.db.models import Batch, CampaignPush, Company, Contact, Parameter, Tenant
from app.gtm_os.sourcing.decision_maker import resolve_decision_makers_batch

TENANT = 2


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
