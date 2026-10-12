"""DELETE /gtm-os/admin/companies -- precise, tenant-agnostic cleanup for wrong-fit matches.

Added 2026-10-08 after Jeff Ballard's Crustdata search matched large Indian industrial
conglomerates for a "B2B technology" ICP; the only prior deletion route was hardcoded to
Elephant Edge's own tenant and deleted an entire batch wholesale.
"""
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import (
    Batch, CalendarBooking, CampaignEvent, CampaignPush, Company, Contact,
    PartnerCompanyRecommendation, PersonalizedMessage, Proposal, Score, Signal, Tenant,
)
from app.gtm_os.content.topic import ContentTopicEvidence, TopicCandidate
from app.gtm_os.icp.icp_matching import ICPMatch
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis, DemandHypothesisEvidence
from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal
from app.gtm_os.intelligence.investigation_memory import InvestigationObjective
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis, ProblemHypothesisEvidence
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.learning.outcome import SalesOutcome
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.plays.lead import GtmLead
from app.gtm_os.send.send_state import MessageSendAttempt
from app.gtm_os.strategy.strategy import GtmStrategy
from app.main import app

ELEPHANT_EDGE, PARTNER = 2, 77
client = TestClient(app)

# "enrichments" has no ORM model anywhere in this codebase (a legacy table, referenced by the
# real production schema's own FK but by no app code) -- defined ad hoc here so the real DELETE
# statement touching it has something to run against in the test DB too.
_METADATA = sa.MetaData()
_ENRICHMENTS_TABLE = sa.Table(
    "enrichments", _METADATA,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("contact_id", sa.Integer),
)


@pytest.fixture
def db():
    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool,
                              connect_args={"check_same_thread": False})
    sa.orm.configure_mappers()
    tables = [Tenant.__table__, Batch.__table__, Company.__table__, Contact.__table__,
             CampaignPush.__table__, GtmLead.__table__, MessageSendAttempt.__table__,
             SalesOutcome.__table__, DemandHypothesisEvidence.__table__,
             ProblemHypothesisEvidence.__table__, ContentTopicEvidence.__table__,
             TopicCandidate.__table__, CalendarBooking.__table__, CampaignEvent.__table__,
             PersonalizedMessage.__table__, Proposal.__table__,
             PartnerCompanyRecommendation.__table__, Score.__table__, Signal.__table__,
             ICPMatch.__table__, InvestigationObjective.__table__, MessageDraft.__table__,
             GtmStrategy.__table__, Opportunity.__table__, DemandHypothesis.__table__,
             ProblemHypothesis.__table__, InterpretedSignal.__table__, GtmSignal.__table__,
             _ENRICHMENTS_TABLE]
    Tenant.__table__.metadata.create_all(engine, tables=tables)
    session = sessionmaker(bind=engine)()
    session.add_all([Tenant(id=ELEPHANT_EDGE, name="Elephant Edge", slug="ee"),
                     Tenant(id=PARTNER, name="Partner", slug="partner")])
    session.commit()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _override_db(db):
    from app.db.session import get_db

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    yield
    app.dependency_overrides.pop(get_db, None)


def _company(db, tenant_id, name="BadCo"):
    batch = Batch(tenant_id=tenant_id, name="b")
    db.add(batch)
    db.commit()
    c = Company(batch_id=batch.id, name=name)
    db.add(c)
    db.commit()
    return c


def _admin(method, path, **kw):
    return client.request(method.upper(), f"/api{path}", headers={"X-Tenant-Id": str(ELEPHANT_EDGE)}, **kw)


def test_deletes_a_company_and_its_contact_from_a_partner_tenant(db):
    company = _company(db, PARTNER)
    contact = Contact(company_id=company.id, first_name="Wrong", last_name="Fit")
    db.add(contact)
    db.commit()

    res = _admin("delete", "/gtm-os/admin/companies", json={"company_ids": [company.id]})
    assert res.status_code == 200
    assert res.json()["deleted_count"] == 1
    assert db.query(Company).count() == 0
    assert db.query(Contact).count() == 0


def test_deletes_the_gtm_lead_too(db):
    company = _company(db, PARTNER)
    db.add(GtmLead(tenant_id=PARTNER, play="icp_filters", lead_key="x", company_id=company.id))
    db.commit()

    _admin("delete", "/gtm-os/admin/companies", json={"company_ids": [company.id]})
    assert db.query(GtmLead).count() == 0


def test_deletes_a_fully_qualified_company_with_its_opportunity_chain(db):
    """REAL BUG, found live 2026-10-12 deleting Supabase from Angela Shori's account list: this
    route only ever cleaned up GtmLead and Contact, but any company that made it through the
    Qualifier -- which writes a real ProblemHypothesis -> DemandHypothesis -> Opportunity chain
    via _write_opportunity, the same path every qualified lead in this codebase goes through --
    hit a raw ForeignKeyViolation on demand_hypotheses and crashed with a 500 instead of
    deleting. Reproduces the full real chain, not just GtmLead/Contact."""
    company = _company(db, PARTNER)
    problem = ProblemHypothesis(tenant_id=PARTNER, company_id=company.id, affected_function="sales",
                                problem_statement="p")
    db.add(problem)
    db.commit()
    demand = DemandHypothesis(tenant_id=PARTNER, company_id=company.id, problem_hypothesis_id=problem.id,
                              affected_function="sales", demand_statement="d")
    db.add(demand)
    db.commit()
    opportunity = Opportunity(tenant_id=PARTNER, company_id=company.id, demand_hypothesis_id=demand.id,
                              problem_hypothesis_id=problem.id, affected_function="sales",
                              opportunity_statement="o")
    db.add(opportunity)
    db.commit()
    db.add(GtmLead(tenant_id=PARTNER, play="icp_filters", lead_key="x", company_id=company.id,
                   opportunity_id=opportunity.id))
    db.commit()

    res = _admin("delete", "/gtm-os/admin/companies", json={"company_ids": [company.id]})
    assert res.status_code == 200, res.text
    assert db.query(Company).count() == 0
    assert db.query(Opportunity).count() == 0
    assert db.query(DemandHypothesis).count() == 0
    assert db.query(ProblemHypothesis).count() == 0


def test_refuses_when_a_contact_was_already_pushed_to_a_campaign(db):
    company = _company(db, PARTNER)
    contact = Contact(company_id=company.id, first_name="Already", last_name="Sent")
    db.add(contact)
    db.commit()
    db.add(CampaignPush(contact_id=contact.id, status="pushed"))
    db.commit()

    res = _admin("delete", "/gtm-os/admin/companies", json={"company_ids": [company.id]})
    assert res.status_code == 409
    assert db.query(Company).count() == 1  # untouched


def test_a_partner_cannot_call_this_route(db):
    company = _company(db, PARTNER)
    res = client.request("DELETE", "/api/gtm-os/admin/companies", json={"company_ids": [company.id]},
                         headers={"X-Tenant-Id": str(PARTNER)})
    assert res.status_code == 404
    assert db.query(Company).count() == 1


def test_company_ids_is_required(db):
    res = _admin("delete", "/gtm-os/admin/companies", json={})
    assert res.status_code == 400
