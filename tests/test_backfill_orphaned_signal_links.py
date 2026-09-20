"""Tests for the free backfill of orphaned signal -> company links.

The dangerous case is ambiguity. Production has duplicate company rows for the same name
(confirmed 2026-09-19: ATALNT, Infisical and Lumion each appear twice, with contacts on one row
and the ICP match on the other). Linking real buying evidence to the wrong row attributes it to
the wrong account and can put a real person into the wrong campaign -- worse than no link.
So a name matching more than one company must link NOTHING.
"""
from app.db.models import Batch, Company
from app.gtm_os.intelligence.signal import GtmSignal
from scripts.backfill_orphaned_signal_links import apply_backfill, plan_backfill

TENANT = 2


def _signal(**kw):
    defaults = dict(
        tenant_id=TENANT, source="linkedin_job", signal_type="job_posting",
        source_ref="ref", dedup_key="k",
    )
    defaults.update(kw)
    return GtmSignal(**defaults)


def test_links_a_signal_to_its_one_matching_company(db_factory):
    db = db_factory([Company, Batch, GtmSignal])
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    company = Company(batch_id=batch.id, name="Acme Robotics", domain="acmerobotics.com")
    db.add(company)
    db.add(_signal(source_ref="j1", dedup_key="k1", company_name_raw="Acme Robotics"))
    db.commit()

    plan = plan_backfill(db)
    assert len(plan["linkable"]) == 1
    apply_backfill(db, plan)

    signal = db.query(GtmSignal).one()
    assert signal.company_id == company.id
    assert signal.company_resolution_status == "resolved"
    assert signal.company_resolution_method == "exact_name_match"


def test_matching_is_insensitive_to_case_and_whitespace(db_factory):
    db = db_factory([Company, Batch, GtmSignal])
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    db.add(Company(batch_id=batch.id, name="Acme  Robotics", domain="a.com"))
    db.add(_signal(source_ref="j1", dedup_key="k1", company_name_raw="  acme robotics "))
    db.commit()

    assert len(plan_backfill(db)["linkable"]) == 1


def test_duplicate_company_rows_in_the_SAME_tenant_are_ambiguous_and_link_nothing(db_factory):
    """THE SAFETY CASE. Lumion genuinely appears twice inside tenant 2 in production
    (lumion.ai and hi.switchy.io, created by the link-shortener domain bug). Guessing between
    them would attribute real buying evidence to the wrong row."""
    db = db_factory([Company, Batch, GtmSignal])
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    db.add(Company(batch_id=batch.id, name="Lumion", domain="lumion.ai"))
    db.add(Company(batch_id=batch.id, name="Lumion", domain="hi.switchy.io"))
    db.add(_signal(source_ref="j1", dedup_key="k1", company_name_raw="Lumion"))
    db.commit()

    plan = plan_backfill(db)
    assert plan["linkable"] == []
    assert len(plan["ambiguous"]) == 1

    apply_backfill(db, plan)
    signal = db.query(GtmSignal).one()
    assert signal.company_id is None, "must not guess between duplicate company rows"
    assert signal.company_resolution_status == "ambiguous"
    assert "duplicates must be merged" in signal.company_resolution_reason


def test_a_copy_in_ANOTHER_tenant_is_not_ambiguity(db_factory):
    """Production has 91 duplicate-domain groups and ALL of them are cross-tenant -- a partner
    and Elephant Edge legitimately each hold their own copy of the same company. ATALNT and
    Infisical were each blocked as 'ambiguous' purely because a tenant-9 copy existed. Matching
    must be scoped to the signal's own tenant, which also prevents a cross-tenant link that
    would breach the boundary partner tenants exist to enforce."""
    db = db_factory([Company, Batch, GtmSignal])
    mine = Batch(tenant_id=TENANT, name="mine")
    theirs = Batch(tenant_id=9, name="theirs")
    db.add_all([mine, theirs])
    db.commit()

    ours = Company(batch_id=mine.id, name="ATALNT", domain="atalnt.ai")
    db.add(ours)
    db.add(Company(batch_id=theirs.id, name="ATALNT", domain="atalnt.ai"))
    db.add(_signal(source_ref="j1", dedup_key="k1", company_name_raw="ATALNT"))
    db.commit()

    plan = plan_backfill(db)
    assert plan["ambiguous"] == []
    assert len(plan["linkable"]) == 1

    apply_backfill(db, plan)
    assert db.query(GtmSignal).one().company_id == ours.id


def test_a_signal_never_links_to_another_tenants_company(db_factory):
    """The data-boundary case: the only matching company belongs to a different tenant, so
    there is no correct link to make."""
    db = db_factory([Company, Batch, GtmSignal])
    theirs = Batch(tenant_id=9, name="theirs")
    db.add(theirs)
    db.commit()
    db.add(Company(batch_id=theirs.id, name="ATALNT", domain="atalnt.ai"))
    db.add(_signal(source_ref="j1", dedup_key="k1", company_name_raw="ATALNT"))
    db.commit()

    plan = plan_backfill(db)
    assert plan["linkable"] == []
    assert plan["unmatched"] == 1


def test_never_overwrites_an_existing_link(db_factory):
    db = db_factory([Company, Batch, GtmSignal])
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    right = Company(batch_id=batch.id, name="Right Co", domain="right.com")
    db.add(right)
    db.commit()
    db.add(_signal(source_ref="j1", dedup_key="k1", company_name_raw="Right Co", company_id=right.id))
    db.commit()

    # Already linked, so it is not part of the orphan backlog at all.
    assert plan_backfill(db)["linkable"] == []


def test_unmatched_names_are_counted_not_guessed(db_factory):
    db = db_factory([Company, Batch, GtmSignal])
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    db.add(Company(batch_id=batch.id, name="Acme Robotics", domain="a.com"))
    db.add(_signal(source_ref="j1", dedup_key="k1", company_name_raw="Someone Else Entirely"))
    db.commit()

    plan = plan_backfill(db)
    assert plan["linkable"] == []
    assert plan["unmatched"] == 1


def test_source_filter_restricts_the_backlog(db_factory):
    db = db_factory([Company, Batch, GtmSignal])
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    db.add(Company(batch_id=batch.id, name="Acme Robotics", domain="a.com"))
    db.add(_signal(source_ref="j1", dedup_key="k1", company_name_raw="Acme Robotics"))
    db.add(_signal(source="linkedin_post", source_ref="p1", dedup_key="k2", company_name_raw="Acme Robotics"))
    db.commit()

    assert len(plan_backfill(db, source="linkedin_job")["linkable"]) == 1
    assert len(plan_backfill(db)["linkable"]) == 2
