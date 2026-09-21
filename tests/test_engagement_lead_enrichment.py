"""Tests for enrich_engagement_lead (app/gtm_os/intelligence/company_resolution.py) -- built
2026-09-22, ready to use but deliberately not wired to run automatically (real Deepline balance
is low). Reuses resolve_company_for_signal (unmodified) and resolve_fallback_email (unmodified)
rather than a second resolution path, so these tests mock those two functions directly instead
of re-exercising their own already-covered internals.
"""
import pytest

from app.db.models import Batch, Company, Contact, Parameter
from app.gtm_os.intelligence.company_resolution import enrich_engagement_lead
from app.gtm_os.intelligence.signal import GtmSignal

TENANT = 15  # majji


@pytest.fixture
def db(db_factory):
    return db_factory([GtmSignal, Company, Contact, Batch, Parameter])


def _engagement_signal(db, name="Justin Hughes", ref="https://www.linkedin.com/in/justin-hughes-432444106"):
    s = GtmSignal(
        tenant_id=TENANT, source="linkedin_engagement", source_ref=ref, signal_type="post_comment",
        person_name_raw=name, dedup_key=f"k-{ref}",
        extracted_info={"author_profile_url": ref, "post_author_name": "Ewan Mcallister"},
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _company(db, batch_id):
    c = Company(batch_id=batch_id, name="Acme Robotics", domain="acmerobotics.com")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_unknown_signal_id_returns_not_found(db):
    assert enrich_engagement_lead(db, TENANT, 99999)["status"] == "not_found"


def test_a_non_engagement_signal_is_rejected(db):
    s = GtmSignal(tenant_id=TENANT, source="linkedin_post", source_ref="x", signal_type="post", dedup_key="k1")
    db.add(s)
    db.commit()
    db.refresh(s)
    assert enrich_engagement_lead(db, TENANT, s.id)["status"] == "not_found"


def test_company_unresolved_stops_before_any_email_lookup(db, monkeypatch):
    signal = _engagement_signal(db)
    monkeypatch.setattr(
        "app.gtm_os.intelligence.company_resolution.resolve_company_for_signal",
        lambda d, t, s, allow_paid_enrichment: {"status": "unresolved", "reason": "no match", "company_id": None},
    )
    called = {"n": 0}
    monkeypatch.setattr(
        "app.phases.free_decision_maker.resolve_fallback_email",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )

    result = enrich_engagement_lead(db, TENANT, signal.id)

    assert result["status"] == "company_unresolved"
    assert called["n"] == 0, "must never attempt email lookup without a resolved company"
    assert db.query(Contact).count() == 0


def test_resolved_company_creates_a_real_contact_with_split_name(db, monkeypatch):
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    db.refresh(batch)
    company = _company(db, batch.id)
    signal = _engagement_signal(db, name="Justin Hughes")

    monkeypatch.setattr(
        "app.gtm_os.intelligence.company_resolution.resolve_company_for_signal",
        lambda d, t, s, allow_paid_enrichment: {"status": "resolved", "company_id": company.id},
    )
    monkeypatch.setattr(
        "app.phases.free_decision_maker.resolve_fallback_email",
        lambda d, t, c, first, last: ("justin@acmerobotics.com", "pattern_guess"),
    )

    result = enrich_engagement_lead(db, TENANT, signal.id)

    assert result["status"] == "resolved"
    assert result["company_id"] == company.id
    assert result["email"] == "justin@acmerobotics.com"
    contact = db.query(Contact).filter(Contact.id == result["contact_id"]).one()
    assert contact.first_name == "Justin"
    assert contact.last_name == "Hughes"
    assert contact.linkedin_url == signal.source_ref
    assert contact.email_source == "pattern_guess"
    assert contact.thread_role == "engagement_lead"


def test_calling_twice_does_not_create_a_duplicate_contact(db, monkeypatch):
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    db.refresh(batch)
    company = _company(db, batch.id)
    signal = _engagement_signal(db)

    monkeypatch.setattr(
        "app.gtm_os.intelligence.company_resolution.resolve_company_for_signal",
        lambda d, t, s, allow_paid_enrichment: {"status": "resolved", "company_id": company.id},
    )
    monkeypatch.setattr("app.phases.free_decision_maker.resolve_fallback_email", lambda *a, **k: None)

    first = enrich_engagement_lead(db, TENANT, signal.id)
    second = enrich_engagement_lead(db, TENANT, signal.id)

    assert first["contact_id"] == second["contact_id"]
    assert db.query(Contact).count() == 1


def test_no_email_found_leaves_contact_without_one_not_an_error(db, monkeypatch):
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    db.refresh(batch)
    company = _company(db, batch.id)
    signal = _engagement_signal(db)

    monkeypatch.setattr(
        "app.gtm_os.intelligence.company_resolution.resolve_company_for_signal",
        lambda d, t, s, allow_paid_enrichment: {"status": "resolved", "company_id": company.id},
    )
    # Simulates today's real state: Deepline budget exhausted -> resolve_fallback_email returns None.
    monkeypatch.setattr("app.phases.free_decision_maker.resolve_fallback_email", lambda *a, **k: None)

    result = enrich_engagement_lead(db, TENANT, signal.id)

    assert result["status"] == "resolved"
    assert result["email"] is None
    contact = db.query(Contact).filter(Contact.id == result["contact_id"]).one()
    assert contact.email is None
    assert contact.email_source is None
