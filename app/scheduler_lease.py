"""One-owner-at-a-time leases for scheduled jobs, shared across every deployed instance.

THE PROBLEM THIS SOLVES. This codebase runs an in-process APScheduler (app/main.py:45,462),
started unconditionally on startup. Three Render accounts are deliberately kept deployed as a
free-tier cost rotation (deployment.md, 2026-09-15) and ALL of them auto-deploy from the same
repo on every push -- so every live instance runs the full scheduler. Nothing anywhere in app/
coordinates them: no advisory lock, no max_instances, no leader election. The risk was named on
2026-08-31 ("both copies run the scheduler against the same database... I'd delete the redundant
service first") and never actioned.

The consequence is not just duplicated work. Every provider account behind these runs is a
SINGLE shared account -- one Apify key on a $5/month cap, one Deepline balance, one Gemini free
tier of 500 calls/model/day. Two instances running the same daily sweep spend that one budget
twice and race each other's budget checks, which are all read-then-act.

WHY A DB LEASE AND NOT pg_try_advisory_lock. A session-level advisory lock is released the
instant its connection drops -- and this codebase has a documented, recurring "SSL connection
has been closed unexpectedly" failure (commits f724ff1, 2e5ba30, 29e3872, and sweep.py's own
stage-retry docstrings). A lock-based lease would silently unlock mid-run under the exact
failure mode this system hits weekly, and the other instance would start a second paid run
believing it was alone. A row with an expiry survives connection churn, and is inspectable from
any instance with plain SQL -- you can see who holds what, and since when.

HOW EXPIRY WORKS. The holder renews (heartbeats) while it works; the lease is only stealable
once it has gone unrenewed past its TTL. So liveness, not elapsed time, decides ownership --
which is deliberately the opposite of recover_stale_gtm_intelligence_runs(), whose fixed
120-minute cutoff flips genuinely-running long sweeps to "failed" and thereby clears the way for
a second overlapping run.

Acquisition is a single conditional UPDATE, which is atomic in Postgres without any explicit
locking: only one instance can observe rowcount == 1 for the same row.
"""
import logging
import os
import socket
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.orm import Session

from app.db.models import Base

logger = logging.getLogger(__name__)

# Long enough that a slow-but-healthy renew cycle never loses the lease, short enough that a
# genuinely dead instance's lease is reclaimed within one scheduler tick rather than a whole day.
DEFAULT_TTL_SECONDS = 900  # 15 minutes
RENEW_EVERY_SECONDS = 60


class SchedulerLease(Base):
    """One row per (tenant, job). Created by ensure_indexes(); see app/db/session.py."""
    __tablename__ = "scheduler_leases"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    job_key = Column(String, nullable=False)
    owner_instance = Column(String, nullable=True)  # NULL = free
    acquired_at = Column(DateTime, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)


def this_instance_id() -> str:
    """Stable, human-readable identity for the process holding a lease, so `owner_instance` in
    the table says something useful when you are looking at why a job did not run. Render sets
    RENDER_INSTANCE_ID; the hostname+pid fallback keeps this meaningful locally."""
    return os.environ.get("RENDER_INSTANCE_ID") or f"{socket.gethostname()}:{os.getpid()}"


def _ensure_row(db: Session, tenant_id: int, job_key: str) -> None:
    exists = (
        db.query(SchedulerLease)
        .filter(SchedulerLease.tenant_id == tenant_id, SchedulerLease.job_key == job_key)
        .first()
    )
    if exists:
        return
    db.add(SchedulerLease(tenant_id=tenant_id, job_key=job_key))
    try:
        db.commit()
    except Exception:  # noqa: BLE001 -- another instance inserted the same row first; harmless
        db.rollback()


def try_acquire(db: Session, tenant_id: int, job_key: str, owner: str | None = None,
                ttl_seconds: int = DEFAULT_TTL_SECONDS, now: datetime | None = None) -> bool:
    """Take the lease if it is free or expired. Returns False if someone else holds it.

    Re-entrant for the SAME owner: an instance that already holds the lease re-acquires it
    (and extends it) rather than deadlocking against itself after a restart."""
    owner = owner or this_instance_id()
    now = now or datetime.utcnow()
    _ensure_row(db, tenant_id, job_key)

    # Single atomic conditional UPDATE -- only one instance can see rowcount == 1.
    updated = (
        db.query(SchedulerLease)
        .filter(
            SchedulerLease.tenant_id == tenant_id,
            SchedulerLease.job_key == job_key,
            (SchedulerLease.owner_instance.is_(None))
            | (SchedulerLease.expires_at < now)
            | (SchedulerLease.owner_instance == owner),
        )
        .update(
            {
                "owner_instance": owner,
                "acquired_at": now,
                "heartbeat_at": now,
                "expires_at": now + timedelta(seconds=ttl_seconds),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return updated == 1


def renew(db: Session, tenant_id: int, job_key: str, owner: str | None = None,
          ttl_seconds: int = DEFAULT_TTL_SECONDS, now: datetime | None = None) -> bool:
    """Extend a lease this instance still holds. False means it was lost (expired and stolen),
    which the caller should treat as "stop working" -- someone else owns this job now."""
    owner = owner or this_instance_id()
    now = now or datetime.utcnow()
    updated = (
        db.query(SchedulerLease)
        .filter(
            SchedulerLease.tenant_id == tenant_id,
            SchedulerLease.job_key == job_key,
            SchedulerLease.owner_instance == owner,
        )
        .update(
            {"heartbeat_at": now, "expires_at": now + timedelta(seconds=ttl_seconds)},
            synchronize_session=False,
        )
    )
    db.commit()
    return updated == 1


def release(db: Session, tenant_id: int, job_key: str, owner: str | None = None) -> bool:
    """Give the lease up immediately rather than waiting out the TTL, so a normal finish frees
    the job for the next tick instead of blocking it for up to ttl_seconds."""
    owner = owner or this_instance_id()
    updated = (
        db.query(SchedulerLease)
        .filter(
            SchedulerLease.tenant_id == tenant_id,
            SchedulerLease.job_key == job_key,
            SchedulerLease.owner_instance == owner,
        )
        .update(
            {"owner_instance": None, "expires_at": None, "acquired_at": None, "heartbeat_at": None},
            synchronize_session=False,
        )
    )
    db.commit()
    return updated == 1


def current_holder(db: Session, tenant_id: int, job_key: str) -> dict | None:
    """Who holds this job right now, for diagnostics -- so "why did the sweep not run?" is
    answerable from SQL instead of guessed at from logs across three deployments."""
    row = (
        db.query(SchedulerLease)
        .filter(SchedulerLease.tenant_id == tenant_id, SchedulerLease.job_key == job_key)
        .first()
    )
    if not row or not row.owner_instance:
        return None
    return {
        "owner_instance": row.owner_instance,
        "acquired_at": row.acquired_at,
        "heartbeat_at": row.heartbeat_at,
        "expires_at": row.expires_at,
    }


@contextmanager
def job_lease(tenant_id: int, job_key: str, ttl_seconds: int = DEFAULT_TTL_SECONDS,
              session_factory=None):
    """Run a scheduled job only if this instance wins the lease.

        with job_lease(tenant_id, "gtm_intelligence_cycle") as acquired:
            if not acquired:
                return
            ...

    Renews in a background daemon thread while the body runs, so a job legitimately longer than
    the TTL keeps its lease, and always releases on the way out -- including on exception, so a
    crashed run does not block the next day's tick for the full TTL.

    Uses its OWN short-lived sessions for acquire/renew/release, never the caller's: the renewer
    runs concurrently with the job body, and a SQLAlchemy Session is not thread-safe.
    """
    if session_factory is None:
        from app.db.session import SessionLocal
        session_factory = SessionLocal

    owner = this_instance_id()
    db = session_factory()
    try:
        acquired = try_acquire(db, tenant_id, job_key, owner, ttl_seconds)
    finally:
        db.close()

    if not acquired:
        db = session_factory()
        try:
            holder = current_holder(db, tenant_id, job_key)
        finally:
            db.close()
        logger.info(
            "job_lease: %s not acquired for tenant %s -- held by %s since %s",
            job_key, tenant_id, (holder or {}).get("owner_instance"), (holder or {}).get("acquired_at"),
        )
        yield False
        return

    stop = threading.Event()

    def _renewer():
        while not stop.wait(RENEW_EVERY_SECONDS):
            renew_db = session_factory()
            try:
                if not renew(renew_db, tenant_id, job_key, owner, ttl_seconds):
                    # Lost it (expired and stolen). Nothing to do but stop renewing -- the
                    # body is not forcibly killed, but the next tick will not double up.
                    logger.warning("job_lease: lost lease %s for tenant %s", job_key, tenant_id)
                    return
            except Exception:  # noqa: BLE001 -- a transient DB blip must not kill the job
                logger.exception("job_lease: renew failed for %s", job_key)
            finally:
                renew_db.close()

    thread = threading.Thread(target=_renewer, name=f"lease-renew-{job_key}", daemon=True)
    thread.start()
    try:
        yield True
    finally:
        stop.set()
        db = session_factory()
        try:
            release(db, tenant_id, job_key, owner)
        except Exception:  # noqa: BLE001 -- expiry is the backstop if release cannot land
            logger.exception("job_lease: release failed for %s; will expire at TTL", job_key)
        finally:
            db.close()
