"""Regression test for POST /gtm-os/messages/{id}/review.

Real bug fixed 2026-09-28, confirmed live: approve_and_send() returns a plain dict
({"draft_id", "status", "send"}), not a MessageDraft -- unlike reject_message_draft()/
request_changes_message_draft(), which both return the MessageDraft row. The route used to
read draft.id/draft.reviewed_at/etc unconditionally on whatever handler() returned, so every
single real "approve" call raised an unhandled AttributeError ('dict' object has no attribute
'id') and returned a bare 500 -- even though the underlying approval and the real SalesRobot/
SMTP send had both already succeeded. The approval and the send were never broken; only this
response was."""
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.strategy.strategy import GtmStrategy
from app.main import app

TENANT = 2  # ELEPHANT_EDGE_TENANT_ID

client = TestClient(app)


@pytest.fixture
def db():
    # StaticPool: TestClient runs the app in its own thread; a plain in-memory SQLite
    # connection from this thread is unusable there otherwise.
    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    sa.orm.configure_mappers()
    tables = [ProblemHypothesis.__table__, DemandHypothesis.__table__, Opportunity.__table__,
              GtmStrategy.__table__, MessageDraft.__table__]
    MessageDraft.__table__.metadata.create_all(engine, tables=tables)
    session = sessionmaker(bind=engine)()
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


def _draft(db, status="ready_for_review"):
    problem = ProblemHypothesis(tenant_id=TENANT, company_name_raw="Acme", affected_function="sales",
                                problem_statement="p")
    db.add(problem)
    db.flush()
    demand = DemandHypothesis(tenant_id=TENANT, company_name_raw="Acme", problem_hypothesis_id=problem.id,
                              affected_function="sales", demand_statement="d")
    db.add(demand)
    db.flush()
    opp = Opportunity(tenant_id=TENANT, company_name_raw="Acme", demand_hypothesis_id=demand.id,
                      problem_hypothesis_id=problem.id, affected_function="sales", opportunity_statement="o",
                      status="qualified")
    db.add(opp)
    db.flush()
    strategy = GtmStrategy(tenant_id=TENANT, opportunity_id=opp.id, strategy_type="consultative")
    db.add(strategy)
    db.flush()
    draft = MessageDraft(tenant_id=TENANT, opportunity_id=opp.id, gtm_strategy_id=strategy.id,
                         channel="linkedin", status=status, message_text="hi", generation_method="llm")
    db.add(draft)
    db.commit()
    return draft


def test_approve_returns_200_with_the_real_draft_and_send_result_not_a_500(db, monkeypatch):
    draft = _draft(db)

    def fake_approve_and_send(db_, tenant_id, message_draft_id, approved_by):
        row = db_.get(MessageDraft, message_draft_id)
        row.status, row.approved_by = "approved", approved_by
        db_.commit()
        return {"draft_id": row.id, "status": row.status, "send": {"status": "enrolled", "provider_ref": "campaign-uuid"}}

    import app.routes.api as api
    monkeypatch.setattr(api, "approve_and_send", fake_approve_and_send)

    response = client.post(f"/api/gtm-os/messages/{draft.id}/review", json={"action": "approve", "reviewed_by": "Kishan"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == draft.id
    assert body["status"] == "approved"
    assert body["approved_by"] == "Kishan"
    assert body["send"] == {"status": "enrolled", "provider_ref": "campaign-uuid"}


def test_reject_still_returns_the_draft_unchanged_in_shape(db, monkeypatch):
    draft = _draft(db)
    response = client.post(f"/api/gtm-os/messages/{draft.id}/review", json={"action": "reject", "reviewed_by": "Kishan"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected"
    assert body["send"] is None
