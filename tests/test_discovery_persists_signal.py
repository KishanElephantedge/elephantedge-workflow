"""Characterization tests for the single most expensive defect in the pipeline.

THE BUG, measured on production 2026-09-19:
    1,066 of 1,175 companies (91%) have no gtm_signal at all.
    352 of 506 linkedin_job signals have no company_id (200 "unresolved", 143 never attempted).

Discovery BUYS a LinkedIn job posting, creates a Company from it, writes V1-shaped
`Company.hiring_signal_*` columns -- and then discards the posting. It never writes a GtmSignal.
But V2's whole intelligence chain (interpretation -> problem -> demand -> opportunity) reads
GtmSignal exclusively, so a company discovered this way can never produce an opportunity.

Worse, the link is thrown away at the one moment the system definitively HAS it: discovery
creates the Company *from* that posting, so it knows both sides. The system then pays Deepline
(company_resolution.py) later to re-derive a link it already had -- and when that spend is
budget-blocked, the signal is marked "unresolved" and the evidence is stranded forever.

So this is both the "nothing flows" bug and a recurring cost: buy the data, throw away half of
it, pay again to reconstruct it.

These tests pin the required behavior. They are expected to FAIL against the code as of
commit 12c2860 and pass after the fix.
"""
import pytest

from app.db.models import Batch, Company, Parameter
from app.gtm_os.intelligence.signal import GtmSignal


TENANT = 2

# One realistic posting, using the REAL field names the fantastic-jobs actor returns.
# (Field naming here is load-bearing: a 2026-08-24 bug had sensing.py reading
# "organizationName"/"companyName", which the actor never returns, so company_name_raw was
# silently None on every linkedin_job signal ever produced.)
POSTING = {
    "id": "job-1001",
    "title": "Head of Sales",
    "organization": "Acme Robotics",
    "organization_url": "https://www.linkedin.com/company/acme-robotics",
    "org_linkedin_website": "acmerobotics.com",
    "org_linkedin_headcount": 40,
    "org_linkedin_headquarters": "Austin, Texas",
    "org_linkedin_industry": "Software Development",
    "date_posted": "2026-09-18",
    "url": "https://www.linkedin.com/jobs/view/acme-head-of-sales-1001",
    "description_text": "We are hiring our first Head of Sales to build the GTM function.",
}


@pytest.fixture
def discovery_db(db_factory):
    # Parameter is needed because get_discovery_oversample() reads the tenant's own
    # oversample config; with no row it falls back to the module default, which is what we want.
    db = db_factory([Company, Batch, GtmSignal, Parameter])
    batch = Batch(tenant_id=TENANT, name="test-batch")
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return db, batch


@pytest.fixture
def patched_discovery(monkeypatch):
    """Stub every paid/network call in the discovery path. Nothing here spends."""
    import app.phases.apify_discovery as ad

    monkeypatch.setattr(ad, "_get_apify_api_key", lambda db, tid: "test-key")
    monkeypatch.setattr(ad, "check_apify_budget", lambda db, tid, est: {"status": ad.APIFY_BUDGET_ALLOWED, "reason": "test"})
    monkeypatch.setattr(ad, "search_linkedin_jobs", lambda *a, **k: [POSTING])
    # Unreadable public profile -> company is kept (profile_rejection_reason returns None).
    monkeypatch.setattr(ad, "fetch_public_company_profile", lambda url: None)
    # Team composition is a paid step; "ok" tier means "not excluded", so the keep loop proceeds.
    monkeypatch.setattr(ad, "assess_team_composition", lambda company, db: {"tier": "ok"})
    monkeypatch.setattr(ad, "_existing_domains", lambda tid, db: set())
    return ad


def test_discovery_creates_the_company(discovery_db, patched_discovery):
    """Baseline: this already works today. Here so a failure below can't be blamed on the fixture."""
    db, batch = discovery_db
    result = patched_discovery.run_apify_discovery(batch.id, db, TENANT, target=1)

    assert result["companies_discovered"] == 1
    company = db.query(Company).one()
    assert company.domain == "acmerobotics.com"
    assert company.name == "Acme Robotics"


def test_discovery_persists_the_posting_as_a_signal(discovery_db, patched_discovery):
    """THE BUG. Every posting discovery pays for and keeps must survive as a GtmSignal.

    Without this, the company exists but has no evidence, and V2's interpretation chain --
    which reads GtmSignal, not Company.hiring_signal_* -- has nothing to work with. This is
    why runs 147/149/150 completed 'cleanly' with signals_created: 0 and opportunity
    eligible: 0."""
    db, batch = discovery_db
    patched_discovery.run_apify_discovery(batch.id, db, TENANT, target=1)

    signals = db.query(GtmSignal).filter(GtmSignal.source == "linkedin_job").all()
    assert len(signals) == 1, (
        "discovery bought a posting, kept the company, and discarded the posting -- "
        "no GtmSignal was written, so nothing downstream can ever interpret it"
    )


def test_persisted_signal_is_linked_to_the_company_it_created(discovery_db, patched_discovery):
    """THE EXPENSIVE HALF. The signal must carry company_id at write time.

    Discovery creates the Company from this exact posting, so the link is known for free,
    right here. Leaving company_id NULL is what sends the signal into company_resolution.py's
    PAID Deepline path -- which, when budget-blocked, records "unresolved" and strands the
    evidence permanently. 352 production signals are in that state today."""
    db, batch = discovery_db
    patched_discovery.run_apify_discovery(batch.id, db, TENANT, target=1)

    company = db.query(Company).one()
    signal = db.query(GtmSignal).filter(GtmSignal.source == "linkedin_job").one()

    assert signal.company_id == company.id, (
        "the posting that CREATED this company must be linked to it at write time -- "
        "otherwise the system pays Deepline later to re-derive a link it already had"
    )
    assert signal.company_resolution_status == "resolved"
    assert signal.company_resolution_method == "explicit"


def test_persisted_signal_matches_the_shape_sensing_writes(discovery_db, patched_discovery):
    """The signal must be the SAME shape app/gtm_os/intelligence/sensing.py writes, so
    interpretation.py / problem_detection.py / demand_detection.py need no changes at all
    to consume it. A near-miss variant here would be silently ignored downstream."""
    db, batch = discovery_db
    patched_discovery.run_apify_discovery(batch.id, db, TENANT, target=1)

    signal = db.query(GtmSignal).filter(GtmSignal.source == "linkedin_job").one()

    assert signal.tenant_id == TENANT
    assert signal.signal_type == "job_posting"
    assert signal.source_ref == "job-1001"
    assert signal.company_name_raw == "Acme Robotics"
    assert signal.dedup_key, "dedup_key is NOT NULL and is how repeat observations are identified"
    # The full description is the input a real interpreter needs; a bare title is not enough.
    assert signal.extracted_info.get("description_text") == POSTING["description_text"]
    assert signal.raw_evidence == POSTING


def test_rerunning_discovery_does_not_duplicate_the_signal(discovery_db, patched_discovery):
    """The same posting can legitimately be returned again by a later overlapping search.
    sense_linkedin_jobs already guards this (a real 2026-08-24 bug: the same Codeable BDR
    posting stored twice); discovery must not reintroduce it."""
    db, batch = discovery_db
    patched_discovery.run_apify_discovery(batch.id, db, TENANT, target=1)
    # Second run: the domain is now already owned, so no new company -- and no new signal.
    import app.phases.apify_discovery as ad
    monkey_seen = {s.lower() for s in ["acmerobotics.com"]}
    ad._existing_domains = lambda tid, db: monkey_seen
    patched_discovery.run_apify_discovery(batch.id, db, TENANT, target=1)

    signals = db.query(GtmSignal).filter(GtmSignal.source == "linkedin_job").all()
    assert len(signals) == 1, "a repeat observation of the same posting must not create a second signal"
