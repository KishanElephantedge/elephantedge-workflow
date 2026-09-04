"""Granola public REST API client -- meeting notes, AI summaries and transcripts.

WHY THIS EXISTS. The meeting-outcome -> revenue-pace loop was built (record_meeting_outcome,
get_revenue_pace, get_revenue_pace_diagnosis) and is completely unused: 15 calendar bookings,
ZERO outcomes recorded. The loop is not broken -- the human step in front of it never happens.
Nobody stops after a call to type in what the outcome was, so revenue pace has nothing to read
and the "AI CEO" loop the product is named after cannot run.

Granola already holds what that step needs: every meeting, its AI summary and its full
transcript. Reading them turns the outcome from something a person must remember to record into
something the system drafts and a person confirms.

PLAN REQUIREMENT, STATED PLAINLY. API keys need Granola's Business plan ($14/user/month).
The workspace is on Basic today, so there is no key yet and nothing here can run. That is a
deliberate, known state -- this module is built against the documented contract so it works the
day a key is added, and get_notes() fails with a clear GranolaError rather than a mystery 401.

There is also an MCP endpoint (https://mcp.granola.ai/mcp) for interactive use from a client
like Claude Code. It is NOT used here: the backend needs a plain server-to-server HTTP call it
can make from a scheduled job on Render, with no MCP client in the loop.
"""
from __future__ import annotations

import logging
import time

import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

logger = logging.getLogger(__name__)

BASE_URL = "https://public-api.granola.ai/v1"
CREDENTIAL_NAME = "granola_api_key"

# Documented limits: burst 25 req / 5s, sustained 5 req/s (300/min), 429 on exceed.
# Kept well under the sustained rate -- this is a background sync, never latency-critical, and a
# 429 costs more time than the pause it would have saved.
REQUEST_INTERVAL_SECONDS = 0.25
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
TIMEOUT_SECONDS = 60


class GranolaError(Exception):
    pass


class GranolaNotConfigured(GranolaError):
    """No API key on file -- expected until the workspace is on a Business plan."""


def _get_api_key(db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id, Credential.name == CREDENTIAL_NAME)
        .first()
    )
    key = (cred.value or "").strip() if cred else ""
    if not key:
        raise GranolaNotConfigured(
            f"{CREDENTIAL_NAME} is not set. Granola API keys require a Business plan "
            "($14/user/month); the workspace is on Basic. Add the key as a Credential once upgraded."
        )
    return key


def _request(db: Session, tenant_id: int, path: str, params: dict | None = None) -> dict:
    key = _get_api_key(db, tenant_id)
    url = f"{BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = httpx.get(url, headers=headers, params=params or {}, timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            # Every other client in this codebase that lacked a timeout has hung a sweep; this one
            # has one, and a transport error retries rather than killing the caller.
            last_error = f"transport error: {e}"
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        if response.status_code == 429:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            last_error = "429 rate limited"
            continue
        if response.status_code == 401:
            raise GranolaError("Granola rejected the API key (401) -- check the key and its scopes")
        if response.status_code == 413:
            # Documented: transcript too large to inline. The caller is expected to fall back to
            # the dedicated /transcript endpoint, so this is signalled distinctly, not as a generic failure.
            raise GranolaError("413 payload too large -- fetch the transcript from /notes/{id}/transcript")
        if response.status_code >= 400:
            raise GranolaError(f"Granola {path} failed ({response.status_code}): {response.text[:300]}")
        return response.json()
    raise GranolaError(f"Granola {path} failed after {MAX_RETRIES} attempts: {last_error}")


def list_notes(db: Session, tenant_id: int, created_after: str | None = None,
               cursor: str | None = None) -> dict:
    """One page of notes. Returns the raw {notes, hasMore, cursor} envelope."""
    params = {}
    if created_after:
        params["created_after"] = created_after
    if cursor:
        params["cursor"] = cursor
    return _request(db, tenant_id, "/notes", params)


def iter_notes(db: Session, tenant_id: int, created_after: str | None = None, max_pages: int = 20):
    """Walk the cursor pagination, pacing requests under the documented rate limit."""
    cursor = None
    for _ in range(max_pages):
        page = list_notes(db, tenant_id, created_after=created_after, cursor=cursor)
        for note in (page.get("notes") or []):
            yield note
        if not page.get("hasMore"):
            return
        cursor = page.get("cursor")
        if not cursor:
            return
        time.sleep(REQUEST_INTERVAL_SECONDS)


def get_note(db: Session, tenant_id: int, note_id: str, include_transcript: bool = True) -> dict:
    params = {"include": "transcript"} if include_transcript else None
    try:
        return _request(db, tenant_id, f"/notes/{note_id}", params)
    except GranolaError as e:
        if "413" not in str(e) or not include_transcript:
            raise
        note = _request(db, tenant_id, f"/notes/{note_id}")
        note["transcript"] = get_transcript(db, tenant_id, note_id)
        return note


def get_transcript(db: Session, tenant_id: int, note_id: str) -> list:
    data = _request(db, tenant_id, f"/notes/{note_id}/transcript")
    if isinstance(data, list):
        return data
    return data.get("transcript") or []


def transcript_to_text(transcript: list) -> str:
    """Flatten Granola's transcript items into readable text.

    Item shape differs by platform: macOS gives speaker.source ("microphone" = the user,
    "speaker" = everyone else); iOS adds speaker.diarization_label ("Speaker A"). Both are
    handled, and an unknown shape degrades to the raw text rather than dropping the line.
    """
    lines = []
    for item in transcript or []:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        speaker = item.get("speaker") or {}
        label = speaker.get("diarization_label")
        if not label:
            source = speaker.get("source")
            label = {"microphone": "Us", "speaker": "Them"}.get(source, "Unknown")
        lines.append(f"{label}: {text}")
    return "\n".join(lines)
