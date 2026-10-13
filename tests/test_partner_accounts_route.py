"""Tests for GET /gtm-os/partner/accounts (app/routes/api.py's get_partner_accounts).

2026-10-13, real bug found on Sandy Yu's dashboard: companies the ICP-filter play persisted as
GtmLead.state == STATE_REJECTED (wrong industry, vendor/agency name, size, etc.) are kept in the
DB on purpose -- so a rejected company is never re-fetched and re-paid-for -- but were showing on
the partner's own dashboard exactly like a real match, since the route never filtered on the
verdict at all. "why have to kept the unmatched once in her dashboard dont show but we can keep
in the db but need to remove from her dashboard" -- keep the row, hide it from the list.
"""
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Batch, Company, Contact
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.plays.lead import GtmLead, STATE_QUALIFIED, STATE_REJECTED
from app.main import app

TENANT = 6  # partner:sandy-yu

client = TestClient(app)


@pytest.fixture
def db():
    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    sa.orm.configure_mappers()
    tables = [Batch.__table__, Company.__table__, Contact.__table__, GtmLead.__table__, GtmSignal.__table__]
    Batch.__table__.metadata.create_all(engine, tables=tables)
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


def _batch(db):
    b = Batch(tenant_id=TENANT, name="test-batch")
    db.add(b)
    db.commit()
    return b


def _company(db, batch, name, industry):
    c = Company(batch_id=batch.id, name=name, industry=industry, source="crustdata-v3:company_search")
    db.add(c)
    db.commit()
    return c


def test_a_rejected_company_is_excluded_from_the_partner_accounts_list():
    from app.db.session import get_db as _get_db_dep
    db = next(app.dependency_overrides[_get_db_dep]())

    batch = _batch(db)
    kept = _company(db, batch, "LinkSure Network", "Mobile Computing Software Products")
    rejected = _company(db, batch, "FCS Solutions", "Hospitality")
    db.add(GtmLead(tenant_id=TENANT, play="icp_filter", lead_key="fcs-solutions",
                    state=STATE_REJECTED, company_id=rejected.id,
                    qualifier_reason="industry mismatch (LLM): Hospitality is not a fit"))
    db.commit()

    resp = client.get("/api/gtm-os/partner/accounts", headers={"X-Tenant-Id": str(TENANT)})
    assert resp.status_code == 200
    body = resp.json()
    names = [a["name"] for a in body["accounts"]]
    assert "LinkSure Network" in names
    assert "FCS Solutions" not in names
    assert body["counts"]["firmographic"] == 1
    assert body["counts"]["all"] == 1

    # the row itself still exists -- only hidden from the dashboard, never deleted
    assert db.query(Company).filter(Company.id == rejected.id).first() is not None


def test_a_qualified_company_with_a_non_rejected_gtmlead_still_shows():
    from app.db.session import get_db as _get_db_dep
    db = next(app.dependency_overrides[_get_db_dep]())

    batch = _batch(db)
    co = _company(db, batch, "Hillhouse Investment", "Investment Management")
    db.add(GtmLead(tenant_id=TENANT, play="icp_filter", lead_key="hillhouse",
                    state=STATE_QUALIFIED, company_id=co.id))
    db.commit()

    resp = client.get("/api/gtm-os/partner/accounts", headers={"X-Tenant-Id": str(TENANT)})
    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()["accounts"]]
    assert "Hillhouse Investment" in names
