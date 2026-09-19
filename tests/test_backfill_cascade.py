"""Tests for cascading a signal's company link onto its already-created interpretation.

This is the half that makes the backfill actually do anything. interpretation.py copies
signal.company_id onto the InterpretedSignal at interpretation time, and problem_detection.py /
demand_detection.py read company_id off the INTERPRETATION, not off the raw signal. Most of the
production backlog was already interpreted while its signal was still orphaned, so linking only
the raw signal would leave the chain seeing an unattributed row and change nothing downstream.
"""
from app.db.models import Batch, Company
from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal
from app.gtm_os.intelligence.signal import GtmSignal
from scripts.backfill_orphaned_signal_links import cascade_links_to_interpretations

TENANT = 2


def _seed(db):
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    company = Company(batch_id=batch.id, name="Acme Robotics", domain="acme.com")
    db.add(company)
    db.commit()
    return company


def _signal(company_id=None, **kw):
    defaults = dict(
        tenant_id=TENANT, source="linkedin_job", signal_type="job_posting",
        source_ref="j1", dedup_key="k1", company_name_raw="Acme Robotics",
        company_id=company_id,
    )
    defaults.update(kw)
    return GtmSignal(**defaults)


def _interpreted(signal_id, company_id=None, **kw):
    defaults = dict(
        tenant_id=TENANT, source_signal_id=signal_id, event_type="hiring_activity",
        # affected_function/business_change/extraction_method are NOT NULL on the real model --
        # an interpretation always records what changed and how that was determined.
        affected_function="sales",
        business_change="hiring a first sales leader",
        extraction_method="deterministic",
        company_id=company_id,
    )
    defaults.update(kw)
    return InterpretedSignal(**defaults)


def test_cascades_company_id_onto_an_orphaned_interpretation(db_factory):
    db = db_factory([Company, Batch, GtmSignal, InterpretedSignal])
    company = _seed(db)
    signal = _signal(company_id=company.id)
    db.add(signal)
    db.commit()
    db.add(_interpreted(signal.id, company_id=None))
    db.commit()

    assert cascade_links_to_interpretations(db, apply=False) == 1
    assert cascade_links_to_interpretations(db, apply=True) == 1

    interpreted = db.query(InterpretedSignal).one()
    assert interpreted.company_id == company.id
    assert interpreted.company_name_raw == "Acme Robotics"


def test_dry_run_counts_but_writes_nothing(db_factory):
    db = db_factory([Company, Batch, GtmSignal, InterpretedSignal])
    company = _seed(db)
    signal = _signal(company_id=company.id)
    db.add(signal)
    db.commit()
    db.add(_interpreted(signal.id, company_id=None))
    db.commit()

    assert cascade_links_to_interpretations(db, apply=False) == 1
    assert db.query(InterpretedSignal).one().company_id is None, "dry run must not write"


def test_never_overwrites_an_interpretation_that_already_has_a_company(db_factory):
    db = db_factory([Company, Batch, GtmSignal, InterpretedSignal])
    company = _seed(db)
    other = Company(batch_id=company.batch_id, name="Other Co", domain="other.com")
    db.add(other)
    db.commit()

    signal = _signal(company_id=company.id)
    db.add(signal)
    db.commit()
    db.add(_interpreted(signal.id, company_id=other.id))
    db.commit()

    assert cascade_links_to_interpretations(db, apply=True) == 0
    assert db.query(InterpretedSignal).one().company_id == other.id


def test_leaves_interpretations_whose_signal_is_still_orphaned(db_factory):
    """An unresolved signal must not cause a guess downstream."""
    db = db_factory([Company, Batch, GtmSignal, InterpretedSignal])
    _seed(db)
    signal = _signal(company_id=None)
    db.add(signal)
    db.commit()
    db.add(_interpreted(signal.id, company_id=None))
    db.commit()

    assert cascade_links_to_interpretations(db, apply=True) == 0
    assert db.query(InterpretedSignal).one().company_id is None
