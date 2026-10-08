"""The self-serve ICP parse endpoint (POST /gtm-os/partner/icp/parse).

Extended 2026-10-08 onboarding Nora: her worksheet needs funding stage, funding recency,
leadership-change, and technographics, none of which the original 10-field prompt could capture.
These tests pin the new fields going out correctly shaped for decompose_icp() to consume -- never
asserting against the LLM itself (mocked), only against this route's own parsing/validation of
whatever the LLM returned.
"""
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Parameter, Tenant
from app.main import app

TENANT = 77
client = TestClient(app)


@pytest.fixture
def db():
    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool,
                              connect_args={"check_same_thread": False})
    sa.orm.configure_mappers()
    Tenant.__table__.metadata.create_all(engine, tables=[Tenant.__table__, Parameter.__table__])
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id=TENANT, name="Nora", slug="nora"))
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


def _parse(text, monkeypatch, llm_response):
    import app.llm_client as llm

    monkeypatch.setattr(llm, "generate_json", lambda prompt, db_, tenant_id, max_tokens=1000: llm_response)
    return client.post("/api/gtm-os/partner/icp/parse", json={"text": text},
                       headers={"X-Tenant-Id": str(TENANT)})


def test_funding_stage_and_recency_pass_through(monkeypatch):
    res = _parse("...", monkeypatch, {
        "funding_stages": ["Series B", "Series C"],
        "funding_recency_max_days": 180,
    })
    assert res.status_code == 200
    body = res.json()
    assert body["funding_stages"] == ["Series B", "Series C"]
    assert body["funding_recency_max_days"] == 180


def test_leadership_change_dropped_when_titles_present_but_no_window(monkeypatch):
    res = _parse("...", monkeypatch, {"leadership_change": {"titles": ["CMO"], "max_age_days": None}})
    assert res.json()["leadership_change"] is None


def test_leadership_change_dropped_when_window_present_but_no_titles(monkeypatch):
    res = _parse("...", monkeypatch, {"leadership_change": {"titles": [], "max_age_days": 180}})
    assert res.json()["leadership_change"] is None


def test_leadership_change_passes_through_when_complete(monkeypatch):
    res = _parse("...", monkeypatch, {
        "leadership_change": {"titles": ["CMO", "VP Marketing"], "max_age_days": 180},
    })
    body = res.json()
    assert body["leadership_change"] == {"titles": ["CMO", "VP Marketing"], "max_age_days": 180}


def test_technologies_include_and_exclude_pass_through(monkeypatch):
    res = _parse("...", monkeypatch, {
        "technologies": {"include": ["HubSpot", "Salesforce"], "exclude": ["none"]},
    })
    assert res.json()["technologies"] == {"include": ["HubSpot", "Salesforce"], "exclude": ["none"]}


def test_technologies_dropped_when_both_lists_empty(monkeypatch):
    res = _parse("...", monkeypatch, {"technologies": {"include": [], "exclude": []}})
    assert res.json()["technologies"] is None


def test_department_headcount_supports_a_minimum_not_just_a_maximum(monkeypatch):
    # Nora's "marketing team, typically four or more" is a FLOOR -- the opposite shape of
    # Majji's "no dedicated marketing hire" ceiling. Both must survive this route unchanged.
    res = _parse("...", monkeypatch, {"department_headcount": {"Marketing": {"min": 4, "max": None}}})
    assert res.json()["department_headcount"] == {"marketing": {"min": 4, "max": None}}


def test_the_whole_parsed_shape_decomposes_into_atoms_end_to_end(monkeypatch):
    """The real point of this route: whatever it returns must be exactly what put_partner_icp
    stores and exactly what decompose_icp() reads -- no silent field-name mismatch between the
    three."""
    from app.gtm_os.sourcing import atoms as A

    res = _parse("...", monkeypatch, {
        "revenue_min_usd": 10_000_000, "revenue_max_usd": 100_000_000,
        "department_headcount": {"marketing": {"min": 4, "max": None}},
        "funding_stages": ["Series B"],
        "funding_recency_max_days": 180,
        "leadership_change": {"titles": ["CMO"], "max_age_days": 180},
        "technologies": {"include": ["HubSpot"], "exclude": []},
    })
    parsed_icp = res.json()
    names = {a.name for a in A.decompose_icp(parsed_icp).atoms}
    assert names >= {"revenue", "department_headcount(marketing)", "funding_stage",
                     "funding_recency", "leadership_change", "technographics"}
