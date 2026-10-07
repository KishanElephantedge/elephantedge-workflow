"""The admin routes that configure a partner's features.

These are what replaced hardcoding a tenant id in the backend. The important properties are that
a partner cannot reach them, and that enabling a feature for a brand-new partner is data entry
rather than a code change.
"""
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Credential, Parameter, Tenant
from app.main import app

ELEPHANT_EDGE, PARTNER = 2, 77
client = TestClient(app)


@pytest.fixture
def db():
    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool,
                              connect_args={"check_same_thread": False})
    sa.orm.configure_mappers()
    tables = [Tenant.__table__, Parameter.__table__, Credential.__table__]
    Tenant.__table__.metadata.create_all(engine, tables=tables)
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id=ELEPHANT_EDGE, name="Elephant Edge", slug="elephant-edge"))
    session.add(Tenant(id=PARTNER, name="New Partner", slug="new-partner"))
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


def _admin(method, path, **kw):
    return getattr(client, method)(f"/api{path}", headers={"X-Tenant-Id": str(ELEPHANT_EDGE)}, **kw)


def test_a_partner_cannot_reach_the_admin_routes(db):
    # Configuring OTHER tenants must never be reachable by a partner's own session.
    res = client.get(f"/api/gtm-os/admin/partners/{PARTNER}/features",
                     headers={"X-Tenant-Id": str(PARTNER)})
    assert res.status_code == 404


def test_a_new_partner_starts_with_every_feature_listed_and_none_enabled(db):
    res = _admin("get", f"/gtm-os/admin/partners/{PARTNER}/features")
    assert res.status_code == 200
    body = res.json()
    assert body["tenant_name"] == "New Partner"
    assert body["enabled_features"] == []
    assert {f["key"] for f in body["features"]} >= {"accounts", "content", "webinars", "email_campaigns"}
    assert all(f["enabled"] is False for f in body["features"])


def test_onboarding_a_partner_onto_an_existing_feature_is_data_entry(db):
    """The whole point: no code change, no deploy, for the second partner wanting email reporting."""
    res = _admin("put", f"/gtm-os/admin/partners/{PARTNER}/features", json={
        "enabled_features": ["accounts", "email_campaigns"],
        "config": {"email_campaigns": {"smartlead_campaign_ids": [{"id": 4242, "label": "Theirs"}]}},
    })
    assert res.status_code == 200
    body = res.json()
    assert set(body["enabled_features"]) == {"accounts", "email_campaigns"}
    email = next(f for f in body["features"] if f["key"] == "email_campaigns")
    assert email["config"]["smartlead_campaign_ids"] == [{"id": 4242, "label": "Theirs"}]
    # Still not ready: the credential is genuinely missing, and it says so.
    assert email["ready"] is False
    assert email["missing_credentials"] == ["smartlead_api_key"]


def test_readiness_flips_once_the_credential_exists(db):
    _admin("put", f"/gtm-os/admin/partners/{PARTNER}/features", json={
        "enabled_features": ["email_campaigns"],
        "config": {"email_campaigns": {"smartlead_campaign_ids": [{"id": 1, "label": "x"}]}},
    })
    db.add(Credential(tenant_id=PARTNER, name="smartlead_api_key", value="sk-real"))
    db.commit()

    body = _admin("get", f"/gtm-os/admin/partners/{PARTNER}/features").json()
    email = next(f for f in body["features"] if f["key"] == "email_campaigns")
    assert (email["ready"], email["missing_config"], email["missing_credentials"]) == (True, [], [])


def test_the_screen_is_generated_from_the_backend_schema(db):
    # The admin UI renders fields from config_schema, so adding a setting server-side needs no
    # frontend change. If this contract breaks, the screen silently stops showing a field.
    body = _admin("get", f"/gtm-os/admin/partners/{PARTNER}/features").json()
    email = next(f for f in body["features"] if f["key"] == "email_campaigns")
    schema = {f["key"]: f for f in email["config_schema"]}
    assert schema["smartlead_campaign_ids"]["required"] is True
    assert schema["smartlead_campaign_ids"]["type"] == "object_list"
    assert schema["smartlead_campaign_ids"]["help"]


def test_an_unknown_feature_or_config_key_is_rejected_with_400(db):
    bad_feature = _admin("put", f"/gtm-os/admin/partners/{PARTNER}/features",
                         json={"enabled_features": ["webinarz"]})
    assert bad_feature.status_code == 400

    bad_key = _admin("put", f"/gtm-os/admin/partners/{PARTNER}/features",
                     json={"config": {"content": {"nope": "x"}}})
    assert bad_key.status_code == 400


def test_config_merges_so_a_partial_save_cannot_wipe_another_field(db):
    _admin("put", f"/gtm-os/admin/partners/{PARTNER}/features", json={
        "enabled_features": ["content"],
        "config": {"content": {"own_linkedin_profile_url": "https://linkedin.com/in/x"}},
    })
    _admin("put", f"/gtm-os/admin/partners/{PARTNER}/features", json={
        "enabled_features": ["content"],
        "config": {"content": {"partner_content_context": {"voice": "direct"}}},
    })
    body = _admin("get", f"/gtm-os/admin/partners/{PARTNER}/features").json()
    content = next(f for f in body["features"] if f["key"] == "content")
    assert content["config"]["own_linkedin_profile_url"] == "https://linkedin.com/in/x"
    assert content["config"]["partner_content_context"] == {"voice": "direct"}


def test_configuring_an_unknown_tenant_is_404(db):
    assert _admin("get", "/gtm-os/admin/partners/9999/features").status_code == 404
