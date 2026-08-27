"""Escalation / Manual Research Capture (2026-08-27) -- see JobDismissal's own docstring
(app/db/models.py) for why this exists and why it's scoped to a dismiss/undo mechanism rather
than a phone-specific capture flow. The two "found it" paths already have real, working routes
(POST /companies/{id}/contacts/import, PATCH /gtm-os/contacts/{id}/email) -- this module only
adds the "skip it" half."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import JobDismissal

VALID_CATEGORIES = {"contacts_to_find"}
VALID_SOURCE_TYPES = {"company", "contact"}


def dismiss_job_item(
    db: Session,
    tenant_id: int,
    category: str,
    source_type: str,
    source_id: int,
    subcategory: str | None = None,
    reason: str | None = None,
    dismissed_by: str | None = None,
) -> JobDismissal:
    """Upsert -- re-dismissing an already-dismissed item just updates the reason/timestamp
    rather than creating a duplicate row (the unique index on (tenant_id, category, source_type,
    source_id) would reject a second insert anyway; this makes the intent explicit)."""
    if category not in VALID_CATEGORIES:
        raise ValueError(f"category must be one of {sorted(VALID_CATEGORIES)}, got {category!r}")
    if source_type not in VALID_SOURCE_TYPES:
        raise ValueError(f"source_type must be one of {sorted(VALID_SOURCE_TYPES)}, got {source_type!r}")

    existing = (
        db.query(JobDismissal)
        .filter(
            JobDismissal.tenant_id == tenant_id,
            JobDismissal.category == category,
            JobDismissal.source_type == source_type,
            JobDismissal.source_id == source_id,
        )
        .first()
    )
    if existing:
        existing.subcategory = subcategory
        existing.reason = reason
        existing.dismissed_by = dismissed_by
        existing.dismissed_at = datetime.utcnow()
        db.commit()
        return existing

    dismissal = JobDismissal(
        tenant_id=tenant_id, category=category, subcategory=subcategory,
        source_type=source_type, source_id=source_id, reason=reason, dismissed_by=dismissed_by,
    )
    db.add(dismissal)
    db.commit()
    return dismissal


def undo_job_dismissal(db: Session, tenant_id: int, category: str, source_type: str, source_id: int) -> bool:
    """Returns True if a real dismissal existed and was removed, False if there was nothing to
    undo -- never raises just because the caller's state was already what they wanted."""
    existing = (
        db.query(JobDismissal)
        .filter(
            JobDismissal.tenant_id == tenant_id,
            JobDismissal.category == category,
            JobDismissal.source_type == source_type,
            JobDismissal.source_id == source_id,
        )
        .first()
    )
    if not existing:
        return False
    db.delete(existing)
    db.commit()
    return True


def get_dismissed_keys(db: Session, tenant_id: int, category: str) -> set[tuple[str, int]]:
    """Returns the real, current set of (source_type, source_id) pairs dismissed for this
    category -- used by jobs_to_be_done.py to filter its derived queue, never to persist state
    of its own (the queue itself stays fully re-derived every call, per that module's own
    discipline)."""
    rows = (
        db.query(JobDismissal.source_type, JobDismissal.source_id)
        .filter(JobDismissal.tenant_id == tenant_id, JobDismissal.category == category)
        .all()
    )
    return {(source_type, source_id) for source_type, source_id in rows}
