"""Direct Anthropic API client -- not routed through Deepline, since the reasoning/writing
steps in Phase 13 (personalized outreach) are pure LLM calls with no data-provider dependency,
and Deepline's own pricing for equivalent tools is opaque ("calculated at execution time").
Real, published Anthropic pricing applies instead."""
import json
import logging

import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

BASE_URL = "https://api.anthropic.com/v1/messages"
# Haiku, not Sonnet -- 3x cheaper ($1/$5 per MTok vs $3/$15) and this pipeline's 4 calls are
# straightforward extraction/synthesis from already-scraped text, not the kind of deep
# reasoning that needs a bigger model. Confirmed against Anthropic's published pricing page.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_VERSION = "2023-06-01"

# Published per-million-token pricing (input, output), by model -- used to compute the REAL
# cost of every call from the actual usage Anthropic returns, not an estimate. There's no
# BudgetGuard/ledger for this feature (it's a manual per-contact dashboard action, not a
# budget-capped autonomous cycle), so a clear log line is the cost visibility mechanism here.
MODEL_PRICING_PER_MTOK_USD = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
}

logger = logging.getLogger("claude_client")


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

    usage = data.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    input_price, output_price = MODEL_PRICING_PER_MTOK_USD.get(model, MODEL_PRICING_PER_MTOK_USD[DEFAULT_MODEL])
    cost_usd = (input_tokens / 1_000_000 * input_price) + (output_tokens / 1_000_000 * output_price)
    logger.warning(
        "claude_client call: model=%s input_tokens=%d output_tokens=%d cost_usd=%.5f",
        model, input_tokens, output_tokens, cost_usd,
    )

    content = data.get("content", [])
    if not content or content[0].get("type") != "text":
        raise ClaudeError(f"Unexpected Claude response shape: {data}")
    return content[0]["text"]


def call_claude_messages(messages: list[dict], db: Session, tenant_id: int, system: str | None = None, tools: list[dict] | None = None, max_tokens: int = 1500, model: str = DEFAULT_MODEL) -> dict:
    """Multi-turn / tool-use call -- returns the FULL parsed response (content blocks incl.
    any tool_use, stop_reason, usage), unlike call_claude which only returns the first text
    block. Built for the chat widget's tool-calling loop (see app/routes/api.py's
    _run_chat_turn), where the caller has to inspect stop_reason and any tool_use blocks
    across multiple round-trips, not just get text back once.

    Haiku, same as the rest of this file -- keeps chat cost in line with everything else here."""
    api_key = _get_api_key(db, tenant_id)
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools

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

    usage = data.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    input_price, output_price = MODEL_PRICING_PER_MTOK_USD.get(model, MODEL_PRICING_PER_MTOK_USD[DEFAULT_MODEL])
    cost_usd = (input_tokens / 1_000_000 * input_price) + (output_tokens / 1_000_000 * output_price)
    logger.warning(
        "claude_client call_messages: model=%s input_tokens=%d output_tokens=%d cost_usd=%.5f stop_reason=%s",
        model, input_tokens, output_tokens, cost_usd, data.get("stop_reason"),
    )
    return data


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
