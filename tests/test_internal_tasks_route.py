"""Tests for /gtm-os/internal/tasks (app/routes/api.py) -- the internal-only task tracker per
partner engagement, built 2026-10-16 per Majji's request ("Kishan, lets create a simple workflow
to monitor tasks we are doing for Sandy... that partners shouldnt see this as this is for
internal"). The real requirement under test: the gateway sets x-internal-user server-side from
the logged-in user's real role, and these routes must 404 for ANY request missing that header --
a partner's own login is proxied with the exact same X-Tenant-Id as an internal admin viewing
them, so x-tenant-id alone can never express this boundary; only x-internal-user can.
"""
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.db.models import PartnerTask
from app.main import app

TENANT = 19  # partner:erin-felton

client = TestClient(app)
ADMIN_HEADERS = {"X-Tenant-Id": str(TENANT), "x-internal-user": "true"}
PARTNER_HEADERS = {"X-Tenant-Id": str(TENANT)}  # no x-internal-user -- a real partner login


@pytest.fixture
def db():
    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    sa.orm.configure_mappers()
    PartnerTask.__table__.metadata.create_all(engine, tables=[PartnerTask.__table__])
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


def test_a_partner_login_gets_404_not_an_empty_list():
    """The real security requirement: a partner's own login (same tenant_id, no x-internal-user)
    must get the SAME 404 an unrelated route would -- not an empty list, which would leak that
    this route exists and is tenant-scoped to them."""
    resp = client.get("/api/gtm-os/internal/tasks", headers=PARTNER_HEADERS)
    assert resp.status_code == 404


def test_a_partner_login_cannot_create_a_task_either():
    resp = client.post("/api/gtm-os/internal/tasks", headers=PARTNER_HEADERS,
                       json={"title": "snoop", "owner": "us"})
    assert resp.status_code == 404


def test_an_internal_admin_can_create_and_list_a_task():
    resp = client.post("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS,
                       json={"title": "Send Sandy the Q4 deck", "owner": "us",
                             "deadline": "2026-11-01", "notes": "Wait for Majji's sign-off first",
                             "created_by": "majji@elephantedge.ai"})
    assert resp.status_code == 200
    task_id = resp.json()["id"]

    resp = client.get("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["id"] == task_id
    assert tasks[0]["title"] == "Send Sandy the Q4 deck"
    assert tasks[0]["owner"] == "us"
    assert tasks[0]["deadline"] == "2026-11-01"
    assert tasks[0]["notes"] == "Wait for Majji's sign-off first"
    assert tasks[0]["status"] == "pending"
    assert tasks[0]["created_by"] == "majji@elephantedge.ai"


def test_an_invalid_owner_is_rejected():
    resp = client.post("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS,
                       json={"title": "x", "owner": "someone-else"})
    assert resp.status_code == 400


def test_an_invalid_status_is_rejected():
    task_id = client.post("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS,
                          json={"title": "x", "owner": "us"}).json()["id"]
    resp = client.patch(f"/api/gtm-os/internal/tasks/{task_id}", headers=ADMIN_HEADERS,
                        json={"status": "done"})  # the old two-state value, no longer valid
    assert resp.status_code == 400


def test_a_task_can_move_through_all_three_statuses():
    task_id = client.post("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS,
                          json={"title": "Sandy sends her logo files", "owner": "partner"}).json()["id"]

    resp = client.patch(f"/api/gtm-os/internal/tasks/{task_id}", headers=ADMIN_HEADERS,
                        json={"status": "in_progress"})
    assert resp.status_code == 200
    tasks = client.get("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS).json()
    assert tasks[0]["status"] == "in_progress"
    assert tasks[0]["completed_at"] is None

    client.patch(f"/api/gtm-os/internal/tasks/{task_id}", headers=ADMIN_HEADERS, json={"status": "completed"})
    tasks = client.get("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS).json()
    assert tasks[0]["status"] == "completed"
    assert tasks[0]["completed_at"] is not None

    # Reopening clears completed_at again -- a task un-done is genuinely not done.
    client.patch(f"/api/gtm-os/internal/tasks/{task_id}", headers=ADMIN_HEADERS, json={"status": "pending"})
    tasks = client.get("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS).json()
    assert tasks[0]["status"] == "pending"
    assert tasks[0]["completed_at"] is None


def test_notes_can_be_added_and_cleared():
    task_id = client.post("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS,
                          json={"title": "x", "owner": "us"}).json()["id"]
    assert client.get("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS).json()[0]["notes"] is None

    client.patch(f"/api/gtm-os/internal/tasks/{task_id}", headers=ADMIN_HEADERS, json={"notes": "waiting on legal"})
    assert client.get("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS).json()[0]["notes"] == "waiting on legal"

    client.patch(f"/api/gtm-os/internal/tasks/{task_id}", headers=ADMIN_HEADERS, json={"notes": ""})
    assert client.get("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS).json()[0]["notes"] is None


def test_a_tenants_tasks_never_leak_into_another_tenants_list():
    client.post("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS, json={"title": "Erin's task", "owner": "us"})

    other_headers = {"X-Tenant-Id": "20", "x-internal-user": "true"}
    resp = client.get("/api/gtm-os/internal/tasks", headers=other_headers)
    assert resp.json() == []


def test_delete_removes_the_task():
    task_id = client.post("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS,
                          json={"title": "stale task", "owner": "us"}).json()["id"]
    resp = client.delete(f"/api/gtm-os/internal/tasks/{task_id}", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert client.get("/api/gtm-os/internal/tasks", headers=ADMIN_HEADERS).json() == []
