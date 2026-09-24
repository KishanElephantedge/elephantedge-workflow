"""Tests for Sandy Yu's webinar outreach CRM (app/routes/api.py's /gtm-os/partner/crm/leads
routes, app/db/models.py's CrmLead). Her own stated pain point: no structured way to see where
each outreach target is in the process."""
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import CrmLead
from app.main import app

TENANT = 6  # partner:sandy-yu

client = TestClient(app)


@pytest.fixture
def db():
    # NOT db_factory (tests/conftest.py) -- that engine has no StaticPool/check_same_thread,
    # which is fine for every other test here (they call functions directly), but TestClient
    # runs the app in its own thread, so a plain in-memory SQLite connection from THIS thread
    # is unusable there ("SQLite objects created in a thread can only be used in that same
    # thread"). StaticPool shares the one connection across threads instead.
    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    sa.orm.configure_mappers()
    CrmLead.__table__.metadata.create_all(engine, tables=[CrmLead.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _override_db(db, monkeypatch):
    from app.db.session import get_db

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    yield
    app.dependency_overrides.pop(get_db, None)


def _make_lead(db, **kw):
    defaults = dict(
        tenant_id=TENANT, event="2026-10-08-sf", first_name="Jane", last_name="Doe",
        title="Chief Revenue Officer", company_name="Acme Co", source_file="JV Sales Nav",
        stage="imported", profile_linkedin_url="https://linkedin.com/in/janedoe",
    )
    defaults.update(kw)
    lead = CrmLead(**defaults)
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


def test_list_leads_scoped_to_tenant(db):
    _make_lead(db)
    _make_lead(db, tenant_id=99, first_name="Other")

    resp = client.get("/api/gtm-os/partner/crm/leads", headers={"X-Tenant-Id": str(TENANT)})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["leads"][0]["first_name"] == "Jane"


def test_stage_counts_are_the_true_totals_not_the_filtered_page(db):
    _make_lead(db, stage="imported")
    _make_lead(db, stage="fit_review", profile_linkedin_url="https://linkedin.com/in/x2")
    _make_lead(db, stage="fit_review", profile_linkedin_url="https://linkedin.com/in/x3")

    resp = client.get("/api/gtm-os/partner/crm/leads", headers={"X-Tenant-Id": str(TENANT)}, params={"stage": "imported"})
    data = resp.json()
    assert data["total"] == 1, "the filtered list itself respects the stage filter"
    assert data["stage_counts"]["imported"] == 1
    assert data["stage_counts"]["fit_review"] == 2, "counts must reflect ALL stages, not just the active filter"


def test_update_stage_moves_the_lead(db):
    lead = _make_lead(db)
    resp = client.patch(f"/api/gtm-os/partner/crm/leads/{lead.id}", headers={"X-Tenant-Id": str(TENANT)}, json={"stage": "fit_review"})
    assert resp.status_code == 200
    assert resp.json()["stage"] == "fit_review"


def test_update_rejects_an_unknown_stage(db):
    lead = _make_lead(db)
    resp = client.patch(f"/api/gtm-os/partner/crm/leads/{lead.id}", headers={"X-Tenant-Id": str(TENANT)}, json={"stage": "made_up_stage"})
    assert resp.status_code == 400


def test_update_can_set_fit_fields_and_email(db):
    lead = _make_lead(db)
    resp = client.patch(
        f"/api/gtm-os/partner/crm/leads/{lead.id}", headers={"X-Tenant-Id": str(TENANT)},
        json={"role_fit": "pass", "company_fit": "pass", "fit_notes": "B2B SaaS, $54M revenue", "email": "jane@acme.com", "email_source": "deepline"},
    )
    body = resp.json()
    assert body["role_fit"] == "pass"
    assert body["company_fit"] == "pass"
    assert body["email"] == "jane@acme.com"


def test_a_lead_from_another_tenant_is_not_reachable(db):
    lead = _make_lead(db, tenant_id=99)
    resp = client.get(f"/api/gtm-os/partner/crm/leads/{lead.id}", headers={"X-Tenant-Id": str(TENANT)})
    assert resp.status_code == 404


def test_search_matches_name_and_company(db):
    _make_lead(db, first_name="Erik", last_name="Lossbroek", company_name="Degreed", profile_linkedin_url="https://linkedin.com/in/erik")
    _make_lead(db, first_name="Mike", last_name="Mooney", company_name="MariaDB", profile_linkedin_url="https://linkedin.com/in/mike")

    resp = client.get("/api/gtm-os/partner/crm/leads", headers={"X-Tenant-Id": str(TENANT)}, params={"search": "MariaDB"})
    data = resp.json()
    assert data["total"] == 1
    assert data["leads"][0]["last_name"] == "Mooney"


def test_source_file_filter_shows_only_that_list(db):
    _make_lead(db, source_file="JV Sales Nav", profile_linkedin_url="https://linkedin.com/in/a")
    _make_lead(db, source_file="SalesIntel SF", profile_linkedin_url="https://linkedin.com/in/b")

    resp = client.get("/api/gtm-os/partner/crm/leads", headers={"X-Tenant-Id": str(TENANT)}, params={"source_file": "SalesIntel SF"})
    data = resp.json()
    assert data["total"] == 1
    assert data["leads"][0]["source_file"] == "SalesIntel SF"
    assert set(data["source_files"]) == {"JV Sales Nav", "SalesIntel SF"}, "source_files must list every real list for this tenant"


def test_company_fit_filter_pass_vs_pending(db):
    _make_lead(db, company_fit="pass", profile_linkedin_url="https://linkedin.com/in/a")
    _make_lead(db, company_fit="fail", profile_linkedin_url="https://linkedin.com/in/b")
    _make_lead(db, company_fit=None, profile_linkedin_url="https://linkedin.com/in/c")

    resp_pass = client.get("/api/gtm-os/partner/crm/leads", headers={"X-Tenant-Id": str(TENANT)}, params={"company_fit": "pass"})
    assert resp_pass.json()["total"] == 1

    resp_pending = client.get("/api/gtm-os/partner/crm/leads", headers={"X-Tenant-Id": str(TENANT)}, params={"company_fit": "pending"})
    assert resp_pending.json()["total"] == 1, "pending must mean company_fit IS NULL, not a literal string"


def test_role_fit_and_source_file_combine(db):
    _make_lead(db, source_file="SalesIntel SF", role_fit="pass", profile_linkedin_url="https://linkedin.com/in/a")
    _make_lead(db, source_file="SalesIntel SF", role_fit="fail", profile_linkedin_url="https://linkedin.com/in/b")
    _make_lead(db, source_file="JV Sales Nav", role_fit="pass", profile_linkedin_url="https://linkedin.com/in/c")

    resp = client.get(
        "/api/gtm-os/partner/crm/leads", headers={"X-Tenant-Id": str(TENANT)},
        params={"source_file": "SalesIntel SF", "role_fit": "pass"},
    )
    data = resp.json()
    assert data["total"] == 1
    assert data["leads"][0]["profile_linkedin_url"] == "https://linkedin.com/in/a"


def test_fit_check_fields_industry_revenue_size_round_trip(db):
    lead = _make_lead(db)
    resp = client.patch(
        f"/api/gtm-os/partner/crm/leads/{lead.id}", headers={"X-Tenant-Id": str(TENANT)},
        json={"industry": "Software Development", "estimated_revenue": "$10M-$75M", "employee_count": "51-200 employees"},
    )
    body = resp.json()
    assert body["industry"] == "Software Development"
    assert body["estimated_revenue"] == "$10M-$75M"
    assert body["employee_count"] == "51-200 employees"

    listed = client.get("/api/gtm-os/partner/crm/leads", headers={"X-Tenant-Id": str(TENANT)}).json()
    assert listed["leads"][0]["industry"] == "Software Development"
