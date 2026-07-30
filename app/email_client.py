"""
Generic SMTP email sender for the pre-/post-outreach notifications. Provider-agnostic --
works with Gmail (App Password), or any SMTP-capable provider -- credentials are read from
this tenant's own Credential rows, never hardcoded, same pattern as every other integration
in this codebase.

Required credentials (Settings page): smtp_host, smtp_port, smtp_username, smtp_password,
notify_email (the address that receives these notifications, e.g. ceo@elephantedge.ai).
"""

import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy.orm import Session

from app.db.models import Credential


class EmailError(Exception):
    pass


def _get_credential(name: str, db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == name)
        .first()
    )
    if not cred or not cred.value:
        raise EmailError(f"{name} credential is not set for this tenant")
    return cred.value


def send_email(subject: str, body: str, db: Session, tenant_id: int, attachment_bytes: bytes | None = None, attachment_filename: str | None = None) -> None:
    smtp_host = _get_credential("smtp_host", db, tenant_id)
    smtp_port = int(_get_credential("smtp_port", db, tenant_id))
    smtp_username = _get_credential("smtp_username", db, tenant_id)
    smtp_password = _get_credential("smtp_password", db, tenant_id)
    notify_email = _get_credential("notify_email", db, tenant_id)

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = smtp_username
    msg["To"] = notify_email
    msg.attach(MIMEText(body, "plain"))

    if attachment_bytes is not None:
        part = MIMEApplication(attachment_bytes, Name=attachment_filename or "attachment.csv")
        part["Content-Disposition"] = f'attachment; filename="{attachment_filename or "attachment.csv"}"'
        msg.attach(part)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_username, smtp_password)
            server.sendmail(smtp_username, [notify_email], msg.as_string())
    except (smtplib.SMTPException, OSError) as e:
        # OSError, not just SMTPException -- confirmed live: a raw socket-level failure
        # ("Network is unreachable") during the connection itself isn't an SMTPException
        # subclass, so it was propagating uncaught past this function entirely. Callers
        # (autonomous_orchestrator's approval notification) only catch EmailError and are
        # contractually never supposed to have a notification failure affect the run's own
        # status -- an unwrapped OSError broke that contract and (via the orchestrator's
        # exception safety net) incorrectly flipped an already-successful run to "failed".
        raise EmailError(f"Failed to send email: {e}") from e
