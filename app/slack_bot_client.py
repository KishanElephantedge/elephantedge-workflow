"""Slack Bot (Web API) client -- distinct from app/slack_client.py's Incoming Webhook, which is
write-only and can only post to the one fixed channel it was created for. This client uses a
real Bot User OAuth Token (xoxb-...) so it can DM a SPECIFIC person, which is what actually
delivering a GTM partner's recommendation message (app/phases/gtm_partner_messaging.py) needs.

IDENTITY SAFETY: this file deliberately does NOT expose any name-based or fuzzy user lookup.
The only ways a LinkedinMonitorProfile.slack_user_id ever gets set are (1) an exact
users.lookupByEmail match, or (2) a human manually pasting in a confirmed Slack user id. This is
a direct response to a real incident in this project's history: a wrong-person LinkedIn message
was once sent because a similarly-titled person outranked the actual target and couldn't be
un-sent. A DM to the wrong person is the same class of mistake and just as unrecoverable --
guessing is not an acceptable trade for convenience here, "for now" or later."""

import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

BOT_TOKEN_CREDENTIAL_NAME = "slack_bot_token"
API_BASE = "https://slack.com/api"


class SlackBotError(Exception):
    pass


def _get_bot_token(db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == BOT_TOKEN_CREDENTIAL_NAME)
        .first()
    )
    if not cred or not cred.value:
        raise SlackBotError(
            f"No {BOT_TOKEN_CREDENTIAL_NAME!r} credential set for this tenant -- create a Slack "
            "app in the target workspace with chat:write, im:write, and users:read.email scopes, "
            "install it, and save the Bot User OAuth Token (starts with xoxb-)."
        )
    return cred.value


def _call(token: str, method: str, params: dict | None = None, json_body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        if json_body is not None:
            response = httpx.post(f"{API_BASE}/{method}", headers=headers, json=json_body, timeout=15)
        else:
            response = httpx.get(f"{API_BASE}/{method}", headers=headers, params=params, timeout=15)
    except httpx.HTTPError as e:
        raise SlackBotError(f"Slack API call to {method} failed: {e}") from e
    data = response.json()
    if not data.get("ok"):
        raise SlackBotError(f"Slack API {method} returned an error: {data.get('error', 'unknown')}")
    return data


def lookup_user_id_by_email(db: Session, tenant_id: int, email: str) -> str | None:
    """Exact-match lookup only (Slack's own users.lookupByEmail -- no fuzzy matching happens
    anywhere in this codebase). Returns None (not an error) when there's simply no Slack account
    with that exact email in this workspace -- a normal, expected outcome, not a failure."""
    token = _get_bot_token(db, tenant_id)
    try:
        data = _call(token, "users.lookupByEmail", params={"email": email})
    except SlackBotError as e:
        if "users_not_found" in str(e):
            return None
        raise
    return data.get("user", {}).get("id")


def send_dm(db: Session, tenant_id: int, user_id: str, text: str) -> None:
    """Opens (or reuses) a DM channel with this exact user id, then posts the message into it.
    conversations.open is idempotent -- safe to call every time rather than caching the channel
    id, and it's the only Slack-recommended way to DM a user (posting directly to a user id as
    if it were a channel id is not reliable across all workspace/app configurations)."""
    token = _get_bot_token(db, tenant_id)
    opened = _call(token, "conversations.open", json_body={"users": user_id})
    channel_id = opened.get("channel", {}).get("id")
    if not channel_id:
        raise SlackBotError(f"conversations.open did not return a channel id for user {user_id}")
    _call(token, "chat.postMessage", json_body={"channel": channel_id, "text": text})
