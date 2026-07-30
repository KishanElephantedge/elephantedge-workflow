"""Slack Incoming Webhook client -- direct, not routed through Deepline (same reasoning as
claude_client.py: Slack's own webhook posting is free, no opaque per-call pricing to worry
about). A webhook URL is tied to one specific channel, chosen when the webhook was created."""
import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential


class SlackError(Exception):
    pass


def _get_webhook_url(db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == "slack_webhook_url")
        .first()
    )
    if not cred or not cred.value:
        raise SlackError("slack_webhook_url credential is not set for this tenant")
    return cred.value


def send_slack_message(text: str, db: Session, tenant_id: int) -> None:
    """Posts a plain-text message to the configured webhook's channel. Incoming Webhooks
    don't support file attachments (unlike email's CSV) -- callers should inline any
    detail that matters directly into the message text."""
    webhook_url = _get_webhook_url(db, tenant_id)
    try:
        response = httpx.post(webhook_url, json={"text": text}, timeout=15)
    except httpx.HTTPError as e:
        # Confirmed live (email_client.py hit the same class of bug): a raw network-level
        # failure here wasn't wrapped at all, so it propagated uncaught past this function --
        # callers (autonomous_orchestrator's approval notification) only catch SlackError and
        # are contractually never supposed to have a notification failure affect the run's
        # own status.
        raise SlackError(f"Slack webhook post failed: {e}") from e
    if response.status_code != 200:
        raise SlackError(f"Slack webhook post failed ({response.status_code}): {response.text}")
