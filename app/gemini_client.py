"""Direct Gemini API client -- primary LLM provider for Phase 13 (cheaper than Claude Haiku
for equivalent quality on these extraction/synthesis tasks, confirmed by a real side-by-side
test: ~$0.0009 vs ~$0.002 per call, same output quality). Claude is the fallback -- see
app/llm_client.py -- for whenever Gemini is unavailable (quota, outage, missing key)."""
import json
import logging

import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

BASE_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-flash-lite-latest"
# Confirmed via a live test call: this alias currently resolves to gemini-3.5-flash-lite
# ($0.30/$2.50 per MTok, no hidden "thinking token" tax unlike the full flash model). Google
# may repoint "latest" to a different model over time -- if logged costs look off, check what
# this alias currently resolves to (the response's modelVersion field) and update this.
INPUT_COST_PER_MTOK_USD = 0.30
OUTPUT_COST_PER_MTOK_USD = 2.50

logger = logging.getLogger("gemini_client")


class GeminiError(Exception):
    pass


def _get_api_key(db: Session, tenant_id: int) -> str:
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == tenant_id)
        .filter(Credential.name == "gemini_api_key")
        .first()
    )
    if not cred or not cred.value:
        raise GeminiError("gemini_api_key credential is not set for this tenant")
    return cred.value


def call_gemini(prompt: str, db: Session, tenant_id: int, max_tokens: int = 2000, model: str = DEFAULT_MODEL) -> str:
    """Single-turn call, returns the text of Gemini's response. Raises GeminiError on any
    non-2xx response, missing credential, or malformed output -- app/llm_client.py catches
    this and falls back to Claude."""
    api_key = _get_api_key(db, tenant_id)
    response = httpx.post(
        BASE_URL_TEMPLATE.format(model=model),
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        },
        timeout=60,
    )
    if response.status_code != 200:
        raise GeminiError(f"Gemini API call failed ({response.status_code}): {response.text}")
    data = response.json()

    usage = data.get("usageMetadata", {})
    input_tokens = usage.get("promptTokenCount", 0)
    output_tokens = usage.get("candidatesTokenCount", 0)
    cost_usd = (input_tokens / 1_000_000 * INPUT_COST_PER_MTOK_USD) + (output_tokens / 1_000_000 * OUTPUT_COST_PER_MTOK_USD)
    logger.warning(
        "gemini_client call: model=%s input_tokens=%d output_tokens=%d cost_usd=%.5f",
        model, input_tokens, output_tokens, cost_usd,
    )

    candidates = data.get("candidates", [])
    if not candidates:
        raise GeminiError(f"Unexpected Gemini response shape (no candidates): {data}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if "text" in p)
    if not text:
        raise GeminiError(f"Unexpected Gemini response shape (no text): {data}")
    return text


def call_gemini_json(prompt: str, db: Session, tenant_id: int, max_tokens: int = 2000, model: str = DEFAULT_MODEL) -> dict:
    """Same as call_gemini, but parses the response as JSON. Gemini tends to return clean
    JSON without markdown fences (confirmed in testing), but this strips them defensively
    anyway, matching claude_client.call_claude_json's behavior."""
    text = call_gemini(prompt, db, tenant_id, max_tokens=max_tokens, model=model)
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
        raise GeminiError(f"Gemini did not return valid JSON: {e}. Raw response: {text[:500]}") from e
