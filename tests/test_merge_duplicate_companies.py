"""Tests for merging a duplicate company row.

The refusals matter more than the happy path here. Merging the wrong pair is not recoverable by
re-running anything: it collapses two real accounts into one, or moves a contact that is already
in someone's live outreach sequence.
"""
import pytest

from app.db.models import Batch, CampaignPush, Company, Contact
from app.gtm_os.intelligence.signal import GtmSignal
from scripts.merge_duplicate_companies import apply_merge, plan_merge

TENANT = 2

# contacts/campaign_pushes are always included: the pushed-contacts refusal is a safety
# check, so it must genuinely execute in every test rather than be skipped for absence.
_TABLES = [Company, Batch, GtmSignal, Contact, CampaignPush]


def _batch(db, tenant_id=TENANT, name="b"):
    b = Batch(tenant_id=tenant_id, name=name)
    db.add(b)
    db.commit()
    return b


def test_moves_references_and_fills_only_empty_fields(db_factory):
    """The real Lumion shape: the survivor holds the signals, the duplicate holds the headcount."""
    db = db_factory(_TABLES)
    b = _batch(db)
    keep = Company(batch_id=b.id, name="Lumion", domain="lumion.ai", industry="Software Development")
    drop = Company(batch_id=b.id, name="Lumion", domain="hi.switchy.io",
                   industry="Should Not Overwrite", employee_count=37)
    db.add_all([keep, drop])
    db.commit()

    db.add(GtmSignal(
        tenant_id=TENANT, source="linkedin_job", source_ref="j1", signal_type="job_posting",
        dedup_key="k1", company_id=drop.id,
    ))
    db.commit()

    plan = plan_merge(db, keep.id, drop.id)
    assert plan["moves"].get("gtm_signals") == 1
    assert "employee_count" in plan["fills"]
    assert "industry" not in plan["fills"], "a field already set on the survivor must not be overwritten"

    apply_merge(db, plan)

    assert db.query(GtmSignal).one().company_id == keep.id
    survivor = db.query(Company).filter(Company.id == keep.id).one()
    assert survivor.employee_count == 37
    assert survivor.industry == "Software Development"
    assert survivor.domain == "lumion.ai", "the real domain must survive, not the shortener"
    assert db.query(Company).filter(Company.id == drop.id).first() is None


def test_refuses_to_merge_across_tenants(db_factory):
    """THE BOUNDARY CASE. All 91 duplicate-domain groups in production are cross-tenant -- a
    partner and Elephant Edge legitimately each hold their own copy."""
    db = db_factory(_TABLES)
    mine, theirs = _batch(db, TENANT, "mine"), _batch(db, 9, "theirs")
    keep = Company(batch_id=mine.id, name="ATALNT", domain="atalnt.ai")
    drop = Company(batch_id=theirs.id, name="ATALNT", domain="atalnt.ai")
    db.add_all([keep, drop])
    db.commit()

    with pytest.raises(ValueError, match="across tenants"):
        plan_merge(db, keep.id, drop.id)


def test_refuses_when_the_duplicate_has_pushed_contacts(db_factory):
    """Reassigning a contact already in a live sequence is an outreach change, not a data fix."""
    db = db_factory(_TABLES)
    b = _batch(db)
    keep = Company(batch_id=b.id, name="Lumion", domain="lumion.ai")
    drop = Company(batch_id=b.id, name="Lumion", domain="hi.switchy.io")
    db.add_all([keep, drop])
    db.commit()

    contact = Contact(company_id=drop.id, first_name="A", last_name="B")
    db.add(contact)
    db.commit()
    db.add(CampaignPush(contact_id=contact.id, status="pushed"))
    db.commit()

    with pytest.raises(ValueError, match="already pushed"):
        plan_merge(db, keep.id, drop.id)


def test_refuses_a_missing_or_self_merge(db_factory):
    db = db_factory(_TABLES)
    b = _batch(db)
    only = Company(batch_id=b.id, name="Solo", domain="solo.com")
    db.add(only)
    db.commit()

    with pytest.raises(ValueError, match="same company"):
        plan_merge(db, only.id, only.id)
    with pytest.raises(ValueError, match="not found"):
        plan_merge(db, only.id, 999999)


def test_dry_run_changes_nothing(db_factory):
    db = db_factory(_TABLES)
    b = _batch(db)
    keep = Company(batch_id=b.id, name="Lumion", domain="lumion.ai")
    drop = Company(batch_id=b.id, name="Lumion", domain="hi.switchy.io", employee_count=37)
    db.add_all([keep, drop])
    db.commit()

    plan_merge(db, keep.id, drop.id)  # planning only

    assert db.query(Company).filter(Company.id == drop.id).first() is not None
    assert db.query(Company).filter(Company.id == keep.id).one().employee_count is None
