"""Idempotent insert helper for EfficiencyActivityEvent -- the only way anything should write to
that table. Every caller supplies a real, already-computed count from an actual pipeline stage
(see the call sites in app/phases/autonomous_orchestrator.py and app/main.py) -- this module never
computes or infers a count itself.

Idempotency is application-level (query-then-insert), matching this codebase's existing pattern
(e.g. outcome.py's _already_recorded, message_draft.py's _existing_draft) rather than a DB
constraint, since every write to this table goes through this one function."""

from datetime import date, datetime

from sqlalchemy.orm import Session

from app.db.models import EfficiencyActivityEvent


def _already_recorded(db: Session, tenant_id: int, activity_type: str, activity_date: date, source: str, source_run_id: int) -> bool:
    return (
        db.query(EfficiencyActivityEvent)
        .filter(EfficiencyActivityEvent.tenant_id == tenant_id)
        .filter(EfficiencyActivityEvent.activity_type == activity_type)
        .filter(EfficiencyActivityEvent.activity_date == activity_date)
        .filter(EfficiencyActivityEvent.source == source)
        .filter(EfficiencyActivityEvent.source_run_id == source_run_id)
        .first() is not None
    )


def record_activity(
    db: Session,
    tenant_id: int,
    activity_type: str,
    activity_date: date,
    volume: int,
    source: str,
    source_run_id: int | None = None,
    metadata: dict | None = None,
) -> EfficiencyActivityEvent | None:
    """Records one real batch of activity volume.

    When `source_run_id` is given (activity tied to a specific AutonomousRun), this is idempotent
    -- a repeat call for the same (tenant, activity_type, activity_date, source, source_run_id)
    is a no-op, preventing double-counting if the same run is ever re-processed.

    When `source_run_id` is None (e.g. a scheduled sweep with no per-invocation identity, like
    the LinkedIn monitor's 45-minute cycle), every call inserts a new row -- each invocation is a
    genuinely distinct real event, and get_monthly_efficiency() SUMs across all of a day's rows,
    so this is correct, not a gap: skipping repeat calls here would silently undercount every
    sweep after the first one on a given day.

    `volume` must be a real count already computed by the caller; this function never derives or
    estimates it."""
    if volume < 0:
        raise ValueError(f"activity volume must be non-negative, got {volume}")
    if source_run_id is not None and _already_recorded(db, tenant_id, activity_type, activity_date, source, source_run_id):
        return None

    event = EfficiencyActivityEvent(
        tenant_id=tenant_id,
        activity_type=activity_type,
        activity_date=activity_date,
        volume=volume,
        source=source,
        source_run_id=source_run_id,
        event_metadata=metadata,
        created_at=datetime.utcnow(),
    )
    db.add(event)
    db.commit()
    return event
