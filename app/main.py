import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.session import SessionLocal, ensure_indexes
from app.google_calendar_client import GoogleCalendarError
from app.gtm_os.governance.governance import compute_and_store_governance_snapshot
from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run, get_intelligence_schedule_utc
from app.gtm_os.orchestration.sweep import (
    GtmIntelligenceRun,
    finish_gtm_intelligence_run,
    recover_stale_gtm_intelligence_runs,
    run_gtm_intelligence_sweep,
    start_gtm_intelligence_run,
)
from app.phases.autonomous_orchestrator import get_autonomous_schedule_utc, resume_pending_approvals, run_daily_autonomous_cycle
from app.phases.calendar_sync import sync_calendar_bookings
from app.phases.gtm_partner_matching import get_match_schedule, run_partner_matching_sweep
from app.phases.linkedin_monitor import get_monitor_schedule, run_linkedin_monitor_sweep
from app.gtm_os.efficiency.activity_recorder import record_activity
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


def _scheduled_linkedin_monitor_sweep():
    """Runs every 45 minutes -- checks every active monitored profile for new posts since its
    own last check, keyword-matches, and alerts on a hit. See app/phases/linkedin_monitor.py
    for the full design (batched Apify calls, cost model, why this isn't Deepline Monitors)."""
    db = SessionLocal()
    try:
        result = run_linkedin_monitor_sweep(db, tenant_id=ELEPHANT_EDGE_TENANT_ID)
        # V2 Efficiency -- this count was previously computed and discarded. No source_run_id
        # here (this isn't tied to an AutonomousRun), so every sweep call inserts its own row;
        # get_monthly_efficiency() SUMs across a day's rows rather than deduping them.
        record_activity(
            db, ELEPHANT_EDGE_TENANT_ID, "signal_monitoring", datetime.utcnow().date(),
            result["profiles_checked"], source="linkedin_monitor_sweep",
        )
    finally:
        db.close()


def _scheduled_partner_matching_sweep():
    """Runs on its own schedule (default daily -- see get_match_schedule) -- proposes up to the
    configured cap of company matches for each classified GTM partner without a pending batch.
    See app/phases/gtm_partner_matching.py for the full design (candidate pre-filter, one
    bounded LLM call per partner, no embeddings)."""
    db = SessionLocal()
    try:
        result = run_partner_matching_sweep(db, tenant_id=ELEPHANT_EDGE_TENANT_ID)
        logging.getLogger(__name__).info("partner_matching_sweep: %s", result)
    finally:
        db.close()


def _scheduled_gtm_intelligence_cycle():
    """Runs once daily, at a fixed configurable UTC time (see control.py's
    get_intelligence_schedule_utc/set_intelligence_schedule_utc, default 10:00 UTC) -- the full
    GTM Intelligence sweep (app/gtm_os/orchestration/sweep.py): sensing (TheirStack/LinkedIn
    replies/Hacker News/RSS) -> interpretation -> Problem/Demand hypotheses -> Opportunity -> ICP
    matching -> GTM strategy -> message generation -> sales readiness -> outcome detection, and
    independently, configured topic linking -> candidate extraction -> candidate normalization ->
    candidate promotion -> trend intelligence.

    Was IntervalTrigger(minutes=60) until 2026-08-23 -- changed to a once-daily CronTrigger to
    match V1's own daily autonomous cycle cadence/pattern (see reschedule_autonomous_job's own
    docstring for why a fixed wall-clock time beats an interval timer: an interval's countdown
    restarts on every deploy, so its actual fire time silently drifts). This cycle's
    candidate_extraction and message_generation stages make real LLM calls (see sweep.py's own
    module docstring), so daily also keeps that cost bounded and predictable.

    Per this backend's own established single-tenant pattern (see _scheduled_autonomous_tick),
    this dedicated backend only ever runs its own tenant's cycle -- never loops over the shared
    `tenants` table.

    RUN STATE (GTM-OS end-to-end wiring): mirrors V1's own stale-run/concurrency-safety pattern
    (_clear_stale_running_flags + a running-run check in autonomous_orchestrator.py) rather than
    inventing a new one -- recover_stale_gtm_intelligence_runs() first, then skip starting a new
    run entirely if one is still genuinely "running" (guards against two ticks overlapping if one
    runs long), otherwise persist a GtmIntelligenceRun row for this run so "what happened during
    the last run" is a real database query, not just a log line to grep for.

    Never raises: run_gtm_intelligence_sweep's own per-source/per-stage error isolation means a
    real failure is captured in its returned result (and now also in the persisted run's own
    status/error_summary), not surfaced as an uncaught exception here.

    V2 CONTROL PLANE (Phase 0): check_can_run() gates this before anything else -- when the
    control plane's state is "paused"/"stopped", this tick no-ops entirely (no GtmIntelligenceRun
    row is even created) rather than running and only skipping the write-side stages, per the
    approved "paused/stopped must prevent new autonomous actions" decision."""
    db = SessionLocal()
    try:
        try:
            check_can_run(db, ELEPHANT_EDGE_TENANT_ID)
        except ControlPlaneHalted as e:
            logging.getLogger(__name__).info("gtm_intelligence_cycle: skipped -- %s", e)
            return

        recover_stale_gtm_intelligence_runs(db, ELEPHANT_EDGE_TENANT_ID)
        already_running = (
            db.query(GtmIntelligenceRun)
            .filter(GtmIntelligenceRun.tenant_id == ELEPHANT_EDGE_TENANT_ID, GtmIntelligenceRun.status == "running")
            .first()
        )
        if already_running is not None:
            logging.getLogger(__name__).warning(
                "gtm_intelligence_cycle: skipped -- run %s already in progress since %s",
                already_running.id, already_running.started_at,
            )
            return

        run = start_gtm_intelligence_run(db, ELEPHANT_EDGE_TENANT_ID)
        result = run_gtm_intelligence_sweep(db, tenant_id=ELEPHANT_EDGE_TENANT_ID)
        finish_gtm_intelligence_run(db, run, result)
        logging.getLogger(__name__).info("gtm_intelligence_cycle: %s", result)
    finally:
        db.close()


def _scheduled_governance_snapshot():
    """Runs every 60 minutes -- V2 Briefing performance fix (see governance.py's
    GovernanceSnapshot). evaluate_gtm_governance() itself is untouched; this just moves its
    (confirmed, 5+ minute) live sweep off the Briefing page's request path onto this schedule, so
    GET /gtm-os/briefing-governance becomes a single fast read of the latest stored snapshot.
    A separate job (not folded into _scheduled_gtm_intelligence_cycle above) since governance
    summarizes overall account/pipeline state, not specifically this hour's sweep activity --
    keeping them independent means a slow/failed governance computation can never block or be
    blocked by the intelligence cycle, or vice versa. Never raises: a failure here just means
    Briefing keeps showing its last-known-good snapshot until the next successful tick, which is
    materially better than the request-time exception a live-compute failure would have caused."""
    db = SessionLocal()
    try:
        compute_and_store_governance_snapshot(db, ELEPHANT_EDGE_TENANT_ID)
        logging.getLogger(__name__).info("governance_snapshot: computed and stored")
    except Exception:
        logging.getLogger(__name__).exception("governance_snapshot: failed, keeping last-known-good snapshot")
    finally:
        db.close()


AUTONOMOUS_MISFIRE_GRACE_SECONDS = 7200


def reschedule_autonomous_job(hour: int, minute: int):
    """Called from the Settings save endpoint so a new schedule time takes effect
    immediately -- no redeploy/restart needed. CronTrigger fires at this exact wall-clock
    time every day, unaffected by when the process itself started (unlike the old
    interval-based job, whose "24h from now" countdown restarted on every deploy).

    misfire_grace_time matters here: found live that 2 daily runs (2026-07-31, 2026-08-01)
    were silently skipped entirely, with no error anywhere, because APScheduler's default
    misfire handling only fires a job if the process is alive within ~1 second of its exact
    trigger time -- if the container happened to be mid-restart/outage at that exact moment,
    the job is just dropped, not deferred. A 2-hour grace window means a restart overlapping
    09:00 UTC still fires the job (late) as soon as the process comes back, instead of
    silently waiting a full 24h for the next tick."""
    scheduler.reschedule_job(
        "autonomous_daily_cycle",
        trigger=CronTrigger(hour=hour, minute=minute, timezone="UTC"),
        misfire_grace_time=AUTONOMOUS_MISFIRE_GRACE_SECONDS,
    )


def reschedule_linkedin_monitor_job(interval_minutes: int):
    """Called from PUT /linkedin-monitor/schedule so a new poll interval (Targets > Settings,
    2026-08-18) takes effect immediately -- same "reschedule the live job, don't wait for a
    restart" pattern as reschedule_autonomous_job above. The job itself keeps firing at this
    cadence unconditionally; PAUSE is handled inside run_linkedin_monitor_sweep (an "enabled"
    DB flag check, not an APScheduler pause_job() call) -- see that function's own docstring
    for why a DB flag, not in-memory scheduler state, is the source of truth."""
    scheduler.reschedule_job("linkedin_monitor_sweep", trigger=IntervalTrigger(minutes=interval_minutes))


def reschedule_partner_matching_job(interval_minutes: int):
    """Called from PUT /linkedin-monitor/match-schedule -- same "reschedule the live job, don't
    wait for a restart" pattern as reschedule_linkedin_monitor_job above. PAUSE is handled inside
    run_partner_matching_sweep (an "enabled" DB flag check), not an APScheduler pause_job()
    call, same reasoning as the LinkedIn monitor's own schedule."""
    scheduler.reschedule_job("partner_matching_sweep", trigger=IntervalTrigger(minutes=interval_minutes))


INTELLIGENCE_CYCLE_MISFIRE_GRACE_SECONDS = 7200


def reschedule_gtm_intelligence_job(hour: int, minute: int):
    """Called from PUT /gtm-os/intelligence-schedule -- same "reschedule the live job, don't wait
    for a restart" pattern as reschedule_autonomous_job above, and the same misfire_grace_time
    reasoning: once this only fires once a day, a missed exact-second trigger (e.g. a deploy
    landing mid-tick) would otherwise silently cost a full day's sensing cycle instead of firing
    late as soon as the process is back."""
    scheduler.reschedule_job(
        "gtm_intelligence_cycle",
        trigger=CronTrigger(hour=hour, minute=minute, timezone="UTC"),
        misfire_grace_time=INTELLIGENCE_CYCLE_MISFIRE_GRACE_SECONDS,
    )


@app.on_event("startup")
def on_startup():
    ensure_indexes()
    db = SessionLocal()
    try:
        hour, minute = get_autonomous_schedule_utc(db, ELEPHANT_EDGE_TENANT_ID)
        linkedin_monitor_interval_minutes = get_monitor_schedule(db, ELEPHANT_EDGE_TENANT_ID)["interval_minutes"]
        partner_matching_interval_minutes = get_match_schedule(db, ELEPHANT_EDGE_TENANT_ID)["interval_minutes"]
        intelligence_hour, intelligence_minute = get_intelligence_schedule_utc(db, ELEPHANT_EDGE_TENANT_ID)
    finally:
        db.close()
    scheduler.add_job(
        _scheduled_autonomous_tick,
        CronTrigger(hour=hour, minute=minute, timezone="UTC"),
        id="autonomous_daily_cycle",
        misfire_grace_time=AUTONOMOUS_MISFIRE_GRACE_SECONDS,
    )
    scheduler.add_job(_scheduled_approval_sweep, "interval", minutes=5, id="approval_window_sweep")
    # Widened from 3/15 minutes (2026-08-21) -- a 3-minute cache warmer and a 15-minute calendar
    # sync individually cost little, but together with every other job on this scheduler they
    # kept the DB compute effectively always-on, never idle long enough for the host's
    # auto-suspend to kick in -- confirmed the real driver behind burning through a full month's
    # free-tier compute-hour quota in under three weeks despite near-zero real data/traffic.
    # Both are pure convenience/freshness jobs (a warm cache for whoever's actively viewing a
    # batch page; a periodic pull of calendar bookings) -- correctness never depends on this
    # cadence, only how fresh a background view can be, so widening costs nothing but staleness.
    scheduler.add_job(_scheduled_cache_refresh, "interval", minutes=15, id="batch_cache_refresh")
    scheduler.add_job(_scheduled_calendar_sync, "interval", minutes=300, id="calendar_booking_sync")
    # Interval read from real, editable config (Targets > Settings) -- was hardcoded
    # minutes=45 until 2026-08-18, with no way to change it without a code change/redeploy.
    scheduler.add_job(_scheduled_linkedin_monitor_sweep, "interval", minutes=linkedin_monitor_interval_minutes, id="linkedin_monitor_sweep")
    # Interval read from real, editable config (Targets > Settings), default daily -- see
    # app/phases/gtm_partner_matching.py.
    scheduler.add_job(_scheduled_partner_matching_sweep, "interval", minutes=partner_matching_interval_minutes, id="partner_matching_sweep")
    # Once daily, fixed UTC time -- matches V1's own daily autonomous cycle cadence/pattern
    # (CronTrigger at a configured hour/minute, not an interval timer whose "next fire" drifts on
    # every deploy). Was IntervalTrigger(minutes=60) until 2026-08-23.
    scheduler.add_job(
        _scheduled_gtm_intelligence_cycle,
        CronTrigger(hour=intelligence_hour, minute=intelligence_minute, timezone="UTC"),
        id="gtm_intelligence_cycle",
        misfire_grace_time=INTELLIGENCE_CYCLE_MISFIRE_GRACE_SECONDS,
    )
    scheduler.add_job(_scheduled_governance_snapshot, "interval", minutes=60, id="governance_snapshot")
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown(wait=False)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "elephant-edge-backend"}
