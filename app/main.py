from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.session import SessionLocal, ensure_indexes
from app.google_calendar_client import GoogleCalendarError
from app.phases.autonomous_orchestrator import get_autonomous_schedule_utc, resume_pending_approvals, run_daily_autonomous_cycle
from app.phases.calendar_sync import sync_calendar_bookings
from app.routes import api
from app.routes.api import ELEPHANT_EDGE_TENANT_ID, refresh_active_batch_caches

app = FastAPI(title="Elephant Edge ABM System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # same shared dashboard frontend as Synefi
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api.router, prefix="/api")

# No Base.metadata.create_all() here -- the shared schema is owned and migrated by Synefi's
# backend (synefi/app/main.py + the manual migrations run against the shared database).
# This backend only ever reads/writes rows within tables that already exist.

scheduler = BackgroundScheduler()


def _scheduled_autonomous_tick():
    """Runs every 24h, for Elephant Edge only -- this backend is dedicated to exactly one
    tenant. Must never loop over every tenant in the shared `tenants` table; each tenant's own
    dedicated backend schedules its own cycle independently."""
    db = SessionLocal()
    try:
        run_daily_autonomous_cycle(db, tenant_id=ELEPHANT_EDGE_TENANT_ID)
    finally:
        db.close()


def _scheduled_approval_sweep():
    """Runs every few minutes -- resumes any run whose 1-hour approval window has elapsed.
    Deliberately a repeating sweep, not a one-shot delayed callback, so a server restart
    mid-window doesn't lose the resume (the next tick after restart just picks it up)."""
    db = SessionLocal()
    try:
        resume_pending_approvals(db, tenant_id=ELEPHANT_EDGE_TENANT_ID)
    finally:
        db.close()


def _scheduled_cache_refresh():
    """Keeps the Redis cache behind GET /batches/{id} warm for every batch page someone's
    actually viewing, so a real click almost never waits on a cold DB query -- see
    app/cache.py and refresh_active_batch_caches in routes/api.py."""
    db = SessionLocal()
    try:
        refresh_active_batch_caches(db)
    finally:
        db.close()


def _scheduled_calendar_sync():
    """Pulls new/changed Appointment Schedule bookings every 15 minutes -- a no-op until the
    google_calendar_* credentials are set (Settings), since OAuth setup is a manual one-time
    step done outside this codebase. Failures here (e.g. credentials not set yet, or Google's
    API being temporarily unreachable) are swallowed -- this is a background convenience sync,
    never something that should crash the scheduler or spam logs on every tick before setup
    is done."""
    db = SessionLocal()
    try:
        sync_calendar_bookings(db, tenant_id=ELEPHANT_EDGE_TENANT_ID)
    except GoogleCalendarError:
        pass
    finally:
        db.close()


def reschedule_autonomous_job(hour: int, minute: int):
    """Called from the Settings save endpoint so a new schedule time takes effect
    immediately -- no redeploy/restart needed. CronTrigger fires at this exact wall-clock
    time every day, unaffected by when the process itself started (unlike the old
    interval-based job, whose "24h from now" countdown restarted on every deploy)."""
    scheduler.reschedule_job("autonomous_daily_cycle", trigger=CronTrigger(hour=hour, minute=minute, timezone="UTC"))


@app.on_event("startup")
def on_startup():
    ensure_indexes()
    db = SessionLocal()
    try:
        hour, minute = get_autonomous_schedule_utc(db, ELEPHANT_EDGE_TENANT_ID)
    finally:
        db.close()
    scheduler.add_job(_scheduled_autonomous_tick, CronTrigger(hour=hour, minute=minute, timezone="UTC"), id="autonomous_daily_cycle")
    scheduler.add_job(_scheduled_approval_sweep, "interval", minutes=5, id="approval_window_sweep")
    scheduler.add_job(_scheduled_cache_refresh, "interval", minutes=3, id="batch_cache_refresh")
    scheduler.add_job(_scheduled_calendar_sync, "interval", minutes=15, id="calendar_booking_sync")
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown(wait=False)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "elephant-edge-backend"}
