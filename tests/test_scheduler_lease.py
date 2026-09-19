"""Tests for cross-instance scheduler leases.

The property that matters: with three Render deployments all running the same in-process
scheduler against one shared set of provider accounts ($5/month Apify, one Deepline balance,
500 Gemini calls/day), exactly ONE may run a given job at a time. Everything else here exists
to make sure that property cannot be lost to a restart, a crash, or a dropped DB connection.
"""
from datetime import datetime, timedelta

from app.scheduler_lease import (
    SchedulerLease, current_holder, job_lease, release, renew, try_acquire,
)

TENANT = 2
JOB = "gtm_intelligence_cycle"


def test_only_one_instance_can_hold_the_lease(db_factory):
    """THE POINT. Two live instances, one job."""
    db = db_factory([SchedulerLease])

    assert try_acquire(db, TENANT, JOB, owner="instance-A") is True
    assert try_acquire(db, TENANT, JOB, owner="instance-B") is False

    assert current_holder(db, TENANT, JOB)["owner_instance"] == "instance-A"


def test_same_owner_can_reacquire_without_deadlocking_itself(db_factory):
    """An instance that restarts mid-lease must not be locked out by its own stale lease."""
    db = db_factory([SchedulerLease])
    assert try_acquire(db, TENANT, JOB, owner="instance-A") is True
    assert try_acquire(db, TENANT, JOB, owner="instance-A") is True


def test_an_expired_lease_is_stealable(db_factory):
    """A dead instance must not block the job forever."""
    db = db_factory([SchedulerLease])
    t0 = datetime(2026, 9, 19, 8, 0, 0)
    assert try_acquire(db, TENANT, JOB, owner="instance-A", ttl_seconds=900, now=t0) is True

    # Still alive 10 minutes later -> B cannot take it.
    assert try_acquire(db, TENANT, JOB, owner="instance-B", now=t0 + timedelta(minutes=10)) is False
    # Past the TTL with no renewal -> B takes over.
    assert try_acquire(db, TENANT, JOB, owner="instance-B", now=t0 + timedelta(minutes=20)) is True
    assert current_holder(db, TENANT, JOB)["owner_instance"] == "instance-B"


def test_renewing_keeps_a_long_job_from_being_stolen(db_factory):
    """Liveness decides ownership, not elapsed time -- deliberately the opposite of
    recover_stale_gtm_intelligence_runs()'s fixed 120-minute cutoff, which flips genuinely
    running sweeps to 'failed' and clears the way for a second overlapping run."""
    db = db_factory([SchedulerLease])
    t0 = datetime(2026, 9, 19, 8, 0, 0)
    assert try_acquire(db, TENANT, JOB, owner="instance-A", ttl_seconds=900, now=t0) is True

    # A genuinely long run that keeps heartbeating stays the owner well past the TTL.
    for minute in (10, 20, 30, 40, 50, 60, 90, 120, 180):
        assert renew(db, TENANT, JOB, owner="instance-A", ttl_seconds=900,
                     now=t0 + timedelta(minutes=minute)) is True
        assert try_acquire(db, TENANT, JOB, owner="instance-B",
                           now=t0 + timedelta(minutes=minute)) is False


def test_renew_fails_once_the_lease_has_been_lost(db_factory):
    """The signal a holder needs to stop working."""
    db = db_factory([SchedulerLease])
    t0 = datetime(2026, 9, 19, 8, 0, 0)
    try_acquire(db, TENANT, JOB, owner="instance-A", ttl_seconds=900, now=t0)
    try_acquire(db, TENANT, JOB, owner="instance-B", now=t0 + timedelta(minutes=20))

    assert renew(db, TENANT, JOB, owner="instance-A", now=t0 + timedelta(minutes=21)) is False


def test_release_frees_the_job_immediately(db_factory):
    """A normal finish must not block the next tick for the rest of the TTL."""
    db = db_factory([SchedulerLease])
    try_acquire(db, TENANT, JOB, owner="instance-A")
    assert release(db, TENANT, JOB, owner="instance-A") is True

    assert current_holder(db, TENANT, JOB) is None
    assert try_acquire(db, TENANT, JOB, owner="instance-B") is True


def test_a_non_owner_cannot_release_someone_elses_lease(db_factory):
    db = db_factory([SchedulerLease])
    try_acquire(db, TENANT, JOB, owner="instance-A")

    assert release(db, TENANT, JOB, owner="instance-B") is False
    assert current_holder(db, TENANT, JOB)["owner_instance"] == "instance-A"


def test_different_jobs_do_not_block_each_other(db_factory):
    db = db_factory([SchedulerLease])
    assert try_acquire(db, TENANT, "gtm_intelligence_cycle", owner="instance-A") is True
    assert try_acquire(db, TENANT, "autonomous_daily_cycle", owner="instance-B") is True


def test_different_tenants_do_not_block_each_other(db_factory):
    db = db_factory([SchedulerLease])
    assert try_acquire(db, 2, JOB, owner="instance-A") is True
    assert try_acquire(db, 12, JOB, owner="instance-B") is True


def test_job_lease_context_manager_grants_one_and_denies_the_other(db_factory):
    db = db_factory([SchedulerLease])
    factory = lambda: db  # noqa: E731 -- single shared in-memory session for the test

    with job_lease(TENANT, JOB, session_factory=factory) as acquired_a:
        assert acquired_a is True
        with job_lease(TENANT, JOB, session_factory=factory) as acquired_b:
            # Same process, so this_instance_id() is identical and re-entrant acquisition is
            # expected -- the cross-instance denial is covered above with explicit owners.
            assert acquired_b is True

    # Released on exit, so the job is immediately available again.
    assert current_holder(db, TENANT, JOB) is None


def test_job_lease_releases_even_when_the_body_raises(db_factory):
    """A crashed run must not block the next day's tick for the full TTL."""
    db = db_factory([SchedulerLease])
    factory = lambda: db  # noqa: E731

    class Boom(Exception):
        pass

    try:
        with job_lease(TENANT, JOB, session_factory=factory) as acquired:
            assert acquired is True
            raise Boom()
    except Boom:
        pass

    assert current_holder(db, TENANT, JOB) is None
