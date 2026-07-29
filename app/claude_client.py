"""Direct Anthropic API client -- not routed through Deepline, since the reasoning/writing
steps in Phase 13 (personalized outreach) are pure LLM calls with no data-provider dependency,
and Deepline's own pricing for equivalent tools is opaque ("calculated at execution time").
Real, published Anthropic pricing applies instead."""
import json

import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

BASE_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
ANTHROPIC_VERSION = "2023-06-01"


class ClaudeError(Exception):
    pass


def _get_api_key(db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == "anthropic_api_key")
        .first()
    )
    if not cred or not cred.value:
        raise ClaudeError("anthropic_api_key credential is not set for this tenant")
    return cred.value


def call_claude(prompt: str, db: Session, tenant_id: int, system: str | None = None, max_tokens: int = 2000, model: str = DEFAULT_MODEL) -> str:
    """Single-turn call, returns the text of Claude's response. Raises ClaudeError on any
    non-2xx response or malformed output -- callers (each research/synthesis module) are
    responsible for deciding whether a failure here should fail the whole pipeline or just
    that one module."""
    api_key = _get_api_key(db, tenant_id)
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system

    response = httpx.post(
        BASE_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if response.status_code != 200:
        raise ClaudeError(f"Claude API call failed ({response.status_code}): {response.text}")
    data = response.json()
    content = data.get("content", [])
    if not content or content[0].get("type") != "text":
        raise ClaudeError(f"Unexpected Claude response shape: {data}")
    return content[0]["text"]


def call_claude_json(prompt: str, db: Session, tenant_id: int, system: str | None = None, max_tokens: int = 2000, model: str = DEFAULT_MODEL) -> dict:
    """Same as call_claude, but parses the response as JSON -- the prompt must instruct
    Claude to return ONLY JSON, no prose. Strips markdown code fences if present, since
    models often wrap JSON in ```json ... ``` even when told not to."""
    text = call_claude(prompt, db, tenant_id, system=system, max_tokens=max_tokens, model=model)
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ClaudeError(f"Claude did not return valid JSON: {e}. Raw response: {text[:500]}") from e
