"""Monthly Efficiency readout -- pure read-only aggregation over EfficiencyActivityEvent
(recorded at the real pipeline call sites, see activity_recorder.py's callers), MessageDraft
(already a real, insert-only event table -- read directly, not duplicated into the ledger), and
AutonomousRun's own started_at/completed_at (reused, not a second execution-time tracker).

    human_equivalent_minutes = volume x manual_minutes   (per activity, only when a benchmark is
                                                            configured and enabled)
    human_equivalent_hours   = sum(human_equivalent_minutes) / 60
    actual_automation_hours  = sum(AutonomousRun.completed_at - started_at) for the month
    net_hours_saved          = max(0, human_equivalent_hours - actual_automation_hours)

Explicitly NOT computed here (see efficiency-feature-audit.md): "equivalent SDRs", "ran outside
9-5 %", "admin time cut %" -- no real data source exists for any of them yet."""

from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import AutonomousRun, Batch, EfficiencyActivityEvent
from app.gtm_os.efficiency.benchmark_config import get_efficiency_benchmarks
from app.gtm_os.learning.message_draft import MessageDraft

# Every activity_type this backend can produce real volume for, and how each one's volume is
# computed -- kept here (not just in benchmark_config.py) as the single source of truth for what
# "real" means in this readout. Anything not in this dict is never shown as measured.
ACTIVITY_LABELS = {
    "contact_enrichment": "Contact enrichment",
    "decision_maker_research": "Decision-maker research",
    "hiring_signal_check": "Hiring-signal check",
    "dedup_check": "CRM dedup check",
    "message_drafting": "Message drafting",
    "signal_monitoring": "Signal monitoring",
    "company_discovery": "Company discovery",
}

# Real, known gaps -- not derived from data, stated once here rather than invented per-request.
# Each is either a genuinely un-automated human task, or a real automated activity this backend
# performs but doesn't yet count (see efficiency-feature-audit.md, Part 9/11 of that report).
STATIC_STILL_MANUAL = [
    {"activity": "Calls", "reason": "not_automated", "detail": "Live calls with prospects are performed by a human; nothing in this backend places or logs calls."},
    {"activity": "Deal strategy / message review", "reason": "not_automated", "detail": "Message drafts require explicit human approval before anything is sent (Phase 7's review gate) -- by design, not a gap."},
    {"activity": "Per-contact CRM dedup (HubSpot)", "reason": "not_instrumented", "detail": "sync_to_hubspot() skips already-synced contacts silently; only company-level dedup (dedup_check above) is counted."},
    {"activity": "Tech-stack detection", "reason": "not_instrumented", "detail": "check_tech_stack() runs per company but its per-run counters are computed and discarded, not yet persisted to the activity ledger."},
    {"activity": "Trend / market intelligence evaluation", "reason": "not_instrumented", "detail": "evaluate_topic_trend() is a deliberately pure, zero-write computation -- there is no event to count."},
    {"activity": "GTM-signal capture (GtmSignal/InterpretedSignal)", "reason": "not_instrumented", "detail": "Real and fully countable, but deliberately not included in this Efficiency readout yet -- it's a different layer (competitor/market signal sensing) than the core enrichment/outreach pipeline measured above, and mixing them would misrepresent which part of the system is being counted."},
    {"activity": "General sweep-run execution history", "reason": "not_instrumented", "detail": "None of the 19 run_*_sweep functions in app/gtm_os/ have their run (start/end/counts) persisted anywhere -- only this Efficiency phase's specific instrumentation is."},
]


def _month_bounds(month: str | None) -> tuple[date, date, str]:
    now = datetime.utcnow()
    if month:
        year, mon = (int(p) for p in month.split("-"))
    else:
        year, mon = now.year, now.month
    period_start = date(year, mon, 1)
    period_end = date(year + (1 if mon == 12 else 0), 1 if mon == 12 else mon + 1, 1)
    return period_start, period_end, f"{year:04d}-{mon:02d}"


def get_monthly_efficiency(db: Session, tenant_id: int, month: str | None = None) -> dict:
    period_start, period_end, month_label = _month_bounds(month)

    # Real ledger volume, SQL-aggregated (Part 15) -- one GROUP BY, not a per-row Python loop.
    ledger_rows = (
        db.query(EfficiencyActivityEvent.activity_type, func.sum(EfficiencyActivityEvent.volume))
        .filter(EfficiencyActivityEvent.tenant_id == tenant_id)
        .filter(EfficiencyActivityEvent.activity_date >= period_start)
        .filter(EfficiencyActivityEvent.activity_date < period_end)
        .group_by(EfficiencyActivityEvent.activity_type)
        .all()
    )
    volumes: dict[str, int] = {activity_type: int(total) for activity_type, total in ledger_rows}

    # MessageDraft is already a real, insert-only event table -- read it directly rather than
    # duplicating it into the ledger (see this module's own docstring and the model's docstring).
    message_draft_count = (
        db.query(func.count(MessageDraft.id))
        .filter(MessageDraft.tenant_id == tenant_id)
        .filter(MessageDraft.created_at >= period_start)
        .filter(MessageDraft.created_at < period_end)
        .scalar()
    ) or 0
    if message_draft_count:
        volumes["message_drafting"] = message_draft_count

    benchmarks = {b["activity_type"]: b for b in get_efficiency_benchmarks(db, tenant_id)}

    breakdown = []
    total_human_equivalent_minutes = 0.0
    missing_benchmarks = []
    for activity_type, label in ACTIVITY_LABELS.items():
        volume = volumes.get(activity_type, 0)
        benchmark = benchmarks.get(activity_type)
        manual_minutes = benchmark.get("manual_minutes") if benchmark else None
        enabled = benchmark.get("enabled", True) if benchmark else True
        has_usable_benchmark = manual_minutes is not None and enabled

        human_equivalent_minutes = (volume * manual_minutes) if has_usable_benchmark else None
        if human_equivalent_minutes is not None:
            total_human_equivalent_minutes += human_equivalent_minutes

        row = {
            "activity_type": activity_type,
            "label": label,
            "volume": volume,
            "manual_minutes": manual_minutes,
            "benchmark_enabled": enabled,
            "human_equivalent_minutes": human_equivalent_minutes,
            "human_equivalent_hours": round(human_equivalent_minutes / 60, 2) if human_equivalent_minutes is not None else None,
        }
        breakdown.append(row)

        if volume > 0 and not has_usable_benchmark:
            missing_benchmarks.append({
                "activity_type": activity_type,
                "label": label,
                "volume": volume,
                "reason": "disabled" if (benchmark and not enabled) else "no_benchmark_configured",
            })

    human_equivalent_hours = round(total_human_equivalent_minutes / 60, 2)

    # Reuse AutonomousRun's own started_at/completed_at -- not a second execution-time tracker.
    # AutonomousRun has no direct tenant_id column (same as everywhere else in this codebase) --
    # scoped via batch_id -> Batch.tenant_id, same join every other Company/Batch query here uses.
    runs = (
        db.query(AutonomousRun.started_at, AutonomousRun.completed_at)
        .join(Batch, AutonomousRun.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id)
        .filter(AutonomousRun.started_at >= period_start)
        .filter(AutonomousRun.started_at < period_end)
        .filter(AutonomousRun.completed_at.isnot(None))
        .all()
    )
    actual_automation_seconds = sum((completed - started).total_seconds() for started, completed in runs if completed and started)
    actual_automation_hours = round(actual_automation_seconds / 3600, 2)

    net_hours_saved = max(0.0, round(human_equivalent_hours - actual_automation_hours, 2))

    return {
        "month": month_label,
        "human_equivalent_hours": human_equivalent_hours,
        "actual_automation_hours": actual_automation_hours,
        "net_hours_saved": net_hours_saved,
        "runs_counted": len(runs),
        "breakdown": breakdown,
        "missing_benchmarks": missing_benchmarks,
        "still_manual": STATIC_STILL_MANUAL,
        # Deliberately not computed -- see this module's own docstring.
        "equivalent_sdrs": None,
        "ran_outside_9_5_percent": None,
        "admin_time_cut_percent": None,
    }
