"""Slack Incoming Webhook client -- direct, not routed through Deepline (same reasoning as
claude_client.py: Slack's own webhook posting is free, no opaque per-call pricing to worry
about). A webhook URL is tied to one specific channel, chosen when the webhook was created --
posting to a second channel means a second webhook (Slack has no "post to multiple channels
with one webhook" option), so this sends to every configured webhook URL."""
import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential, Parameter

WEBHOOK_CREDENTIAL_NAMES = ["slack_webhook_url", "slack_webhook_url_2"]


class SlackError(Exception):
    pass


def is_slack_enabled(db: Session, tenant_id: int) -> bool:
    """Defaults to True (existing behavior) -- set slack_enabled=false via the parameters
    endpoint to silence notifications without touching/losing the webhook credential itself.
    Callers already treat SlackError as best-effort (isolated try/except, never fails the
    run), so this reuses that same path rather than needing its own guard at every call site."""
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == "slack_enabled")
        .first()
    )
    if not param or param.value is None:
        return True
    value = param.value
    return bool(value.get("enabled") if isinstance(value, dict) else value)


def _get_webhook_urls(db: Session, tenant_id: int) -> list[str]:
    if not is_slack_enabled(db, tenant_id):
        raise SlackError("Slack notifications are disabled (slack_enabled=false)")
    creds = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name.in_(WEBHOOK_CREDENTIAL_NAMES))
        .all()
    )
    urls = [c.value for c in creds if c.value]
    if not urls:
        raise SlackError("No slack_webhook_url credential is set for this tenant")
    return urls


def _post_to_webhook(webhook_url: str, text: str) -> None:
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


def send_slack_message(text: str, db: Session, tenant_id: int) -> None:
    """Posts a plain-text message to every configured webhook's channel. Incoming Webhooks
    don't support file attachments (unlike email's CSV) -- callers should inline any detail
    that matters directly into the message text. Raises SlackError only if EVERY configured
    webhook failed; if at least one channel got the message, that's treated as a success (the
    caller doesn't distinguish "reached 1 of 2 channels" from "reached 2 of 2" today)."""
    webhook_urls = _get_webhook_urls(db, tenant_id)
    errors = []
    for url in webhook_urls:
        try:
            _post_to_webhook(url, text)
        except SlackError as e:
            errors.append(str(e))
    if len(errors) == len(webhook_urls):
        raise SlackError("; ".join(errors))
