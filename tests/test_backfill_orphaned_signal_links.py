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


def test_duplicate_company_rows_are_ambiguous_and_link_nothing(db_factory):
    """THE SAFETY CASE. Two rows named ATALNT exist in production; guessing between them
    would attribute real evidence to the wrong account."""
    db = db_factory([Company, Batch, GtmSignal])
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    db.add(Company(batch_id=batch.id, name="ATALNT", domain="atalnt.ai"))
    db.add(Company(batch_id=batch.id, name="ATALNT", domain="atalnt.com"))
    db.add(_signal(source_ref="j1", dedup_key="k1", company_name_raw="ATALNT"))
    db.commit()

    plan = plan_backfill(db)
    assert plan["linkable"] == []
    assert len(plan["ambiguous"]) == 1

    apply_backfill(db, plan)
    signal = db.query(GtmSignal).one()
    assert signal.company_id is None, "must not guess between duplicate company rows"
    assert signal.company_resolution_status == "ambiguous"
    assert "duplicates must be merged" in signal.company_resolution_reason


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
