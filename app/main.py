from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.session import SessionLocal
from app.phases.autonomous_orchestrator import resume_pending_approvals, run_daily_autonomous_cycle
from app.routes import api
from app.routes.api import ELEPHANT_EDGE_TENANT_ID

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


@app.on_event("startup")
def on_startup():
    scheduler.add_job(_scheduled_autonomous_tick, "interval", hours=24, id="autonomous_daily_cycle")
    scheduler.add_job(_scheduled_approval_sweep, "interval", minutes=5, id="approval_window_sweep")
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown(wait=False)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "elephant-edge-backend"}
