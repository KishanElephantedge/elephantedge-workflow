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

from app.db.models import Batch, CampaignPush, Company, Contact, Tenant
from app.gtm_os.plays.lead import GtmLead
from app.main import app

ELEPHANT_EDGE, PARTNER = 2, 77
client = TestClient(app)


@pytest.fixture
def db():
    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool,
                              connect_args={"check_same_thread": False})
    sa.orm.configure_mappers()
    tables = [Tenant.__table__, Batch.__table__, Company.__table__, Contact.__table__,
             CampaignPush.__table__, GtmLead.__table__]
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
