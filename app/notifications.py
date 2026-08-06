"""In-app notification feed -- created at the same real event points the existing Slack
alerts already fire on (see autonomous_orchestrator.py), plus meeting-booked events Slack
never covered. Deliberately separate from Slack, not a replacement -- some teammates without
dashboard access still rely on the Slack channel."""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import Notification

NOTIFICATION_RETENTION_DAYS = 30


def create_notification(
    db: Session,
    tenant_id: int,
    type_: str,
    title: str,
    message: str | None = None,
    severity: str = "info",
    batch_id: int | None = None,
    run_id: int | None = None,
) -> None:
    """Best-effort by design, same as the Slack senders it sits alongside -- a notification
    failing to write must never block or fail the real work (a run finishing, a message
    sending) that triggered it."""
    try:
        notif = Notification(
            tenant_id=tenant_id, type=type_, severity=severity, title=title, message=message,
            batch_id=batch_id, run_id=run_id,
        )
        db.add(notif)
        db.commit()
    except Exception:  # noqa: BLE001 -- notifications are best-effort, never worth crashing the caller over
        db.rollback()


def delete_expired_notifications(db: Session, tenant_id: int) -> int:
    """Swept on every list read (see GET /notifications) rather than a separate scheduled
    job -- simple, and the sweep only ever needs to run when someone's actually about to look
    at the list."""
    cutoff = datetime.utcnow() - timedelta(days=NOTIFICATION_RETENTION_DAYS)
    deleted = (
        db.query(Notification)
        .filter(Notification.tenant_id == tenant_id)
        .filter(Notification.created_at < cutoff)
        .delete()
    )
    db.commit()
    return deleted
