"""Characterization tests for a real bug found live 2026-09-21 (majji tenant, batch 171):
run_apify_discovery's keep loop used to `break` the moment `target` companies were kept, even
though the Apify call above it already pays a FIXED cost for every posting in `jobs` up front
(`limit=discovery_limit`, billed regardless of how many are actually used). The break didn't
save a cent -- it just stopped the loop from looking at postings already bought. Measured on
production: 80 postings for $0.41, 65 new-domain companies available, only 8 kept -- 57
already-paid-for companies discarded for nothing.

These tests pin the required behavior: every posting that clears the FREE filters (domain,
headcount, HQ, role/product-fit, public profile) must become a real Company + signal regardless
of `target`; only the PAID step (assess_team_composition, real Deepline money) may still respect
`target` as a genuine cost ceiling.
"""
import pytest

from app.db.models import Batch, Company, Contact, Parameter, Score, Signal
from app.gtm_os.intelligence.signal import GtmSignal

TENANT = 2


def _posting(n: int) -> dict:
    return {
        "id": f"job-{n}",
        "title": "Head of Sales",
        "organization": f"Company {n}",
        "organization_url": f"https://www.linkedin.com/company/company-{n}",
        "org_linkedin_website": f"company{n}.com",
        "org_linkedin_headcount": 40,
        "org_linkedin_headquarters": "Austin, Texas",
        "org_linkedin_industry": "Software Development",
        "date_posted": "2026-09-18",
        "url": f"https://www.linkedin.com/jobs/view/company-{n}-head-of-sales",
        "description_text": "We are hiring our first Head of Sales to build the GTM function.",
    }


@pytest.fixture
def discovery_db(db_factory):
    db = db_factory([Company, Batch, GtmSignal, Parameter, Signal, Score, Contact])
    batch = Batch(tenant_id=TENANT, name="test-batch")
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return db, batch


@pytest.fixture
def patched_discovery(monkeypatch):
    import app.phases.apify_discovery as ad

    monkeypatch.setattr(ad, "_get_apify_api_key", lambda db, tid: "test-key")
    monkeypatch.setattr(ad, "check_apify_budget", lambda db, tid, est: {"status": ad.APIFY_BUDGET_ALLOWED, "reason": "test"})
    monkeypatch.setattr(ad, "search_linkedin_jobs", lambda *a, **k: [_posting(i) for i in range(10)])
    monkeypatch.setattr(ad, "fetch_public_company_profile", lambda url: None)
    monkeypatch.setattr(ad, "_existing_domains", lambda tid, db: set())
    return ad


def test_every_free_qualified_posting_becomes_a_company_not_just_target_many(discovery_db, patched_discovery, monkeypatch):
    """THE BUG. 10 postings all clear the free filters; target=3. All 10 must become Company
    rows (already paid for in the one Apify call) -- not just the first 3."""
    db, batch = discovery_db
    calls = {"n": 0}

    def _assess(company, db):
        calls["n"] += 1
        return {"tier": "ok"}

    monkeypatch.setattr(patched_discovery, "assess_team_composition", _assess)

    result = patched_discovery.run_apify_discovery(batch.id, db, TENANT, target=3)

    assert result["companies_discovered"] == 3, "kept count (passed the paid gate) still respects target"
    assert db.query(Company).filter(Company.batch_id == batch.id).count() == 10, (
        "all 10 already-paid-for postings must be persisted as companies, not just the 3 kept"
    )


def test_the_paid_team_composition_check_is_still_bounded_by_target(discovery_db, patched_discovery, monkeypatch):
    """THE COST GUARANTEE. Even with 10 free-qualified candidates, the PAID assess_team_composition
    call must run at most `target` times -- this is the one place `target` is still a real spend
    ceiling, unchanged from before the fix."""
    db, batch = discovery_db
    calls = {"n": 0}

    def _assess(company, db):
        calls["n"] += 1
        return {"tier": "ok"}

    monkeypatch.setattr(patched_discovery, "assess_team_composition", _assess)

    patched_discovery.run_apify_discovery(batch.id, db, TENANT, target=3)

    assert calls["n"] == 3, "assess_team_composition (real Deepline money) must not run more than target times"


def test_companies_beyond_target_are_not_team_fit_assessed_but_still_get_a_signal(discovery_db, patched_discovery, monkeypatch):
    """Companies past the target cutoff never reach the paid gate at all -- they're real,
    persisted, dedup-safe backlog, not a promise of a fully-enriched company."""
    db, batch = discovery_db
    monkeypatch.setattr(patched_discovery, "assess_team_composition", lambda company, db: {"tier": "ok"})

    patched_discovery.run_apify_discovery(batch.id, db, TENANT, target=1)

    companies = db.query(Company).filter(Company.batch_id == batch.id).order_by(Company.id).all()
    assert len(companies) == 10
    assessed = [c for c in companies if c.hiring_signal_role is not None]
    unassessed = [c for c in companies if c.hiring_signal_role is None]
    assert len(assessed) == 1
    assert len(unassessed) == 9
    # The one that went through the full pipeline has a linked signal; the backlog ones don't yet
    # (they were never evaluated for team fit, so nothing has been decided about them either way).
    signal_company_ids = {s.company_id for s in db.query(GtmSignal).filter(GtmSignal.tenant_id == TENANT).all()}
    assert {c.id for c in assessed} <= signal_company_ids


def test_a_team_fit_excluded_company_is_still_deleted_same_as_before(discovery_db, patched_discovery, monkeypatch):
    """Real ICP disqualification (already has a full sales team) is unchanged behavior -- this
    is a genuine content-based rejection, not the discard-purchased-data bug this fix targets.
    With one exclusion, the loop must try the NEXT free-qualified candidate to still reach
    target=1 kept, at zero extra Apify cost."""
    db, batch = discovery_db
    seen = {"n": 0}

    def _assess(company, db):
        seen["n"] += 1
        return {"tier": "excluded"} if seen["n"] == 1 else {"tier": "ok"}

    monkeypatch.setattr(patched_discovery, "assess_team_composition", _assess)

    result = patched_discovery.run_apify_discovery(batch.id, db, TENANT, target=1)

    assert result["companies_discovered"] == 1
    names = {c.name for c in db.query(Company).filter(Company.batch_id == batch.id).all()}
    assert "Company 0" not in names, "the excluded company must still be deleted, same as before"
    assert len(names) == 9, "10 free-qualified minus the 1 deleted-for-exclusion"
