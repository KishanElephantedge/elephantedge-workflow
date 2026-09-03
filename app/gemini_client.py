"""Direct Gemini API client -- primary LLM provider for Phase 13 (cheaper than Claude Haiku
for equivalent quality on these extraction/synthesis tasks, confirmed by a real side-by-side
test: ~$0.0009 vs ~$0.002 per call, same output quality). Claude is the fallback -- see
app/llm_client.py -- for whenever Gemini is unavailable (quota, outage, missing key)."""
import json
import time
import logging

import httpx
from sqlalchemy.orm import Session

from app.db.models import Credential

BASE_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Gemini's free tier is a PER-DAY, PER-MODEL quota -- 500 requests/day for flash-lite, confirmed
# live from the 429 body (GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit 500). A single
# daily sweep makes ~525 LLM calls, so ONE run exhausts the day's allowance and every later run
# fails outright. That is what happened on 2026-09-03: three runs, none produced anything.
#
# Because the quota is per MODEL, a different model has its own untouched allowance. That is not a
# fix for the underlying problem -- it is one more free tier to exhaust, and the real answer is a
# paid key (Anthropic, once topped up, via llm_client.PRIMARY) -- but it buys a working pipeline
# today instead of a dead one.
#
# FALLBACK_MODELS is tried in order when the primary is quota-exhausted. Keep them ordered by
# preference, and expect to add to it: each entry is a separate 500/day bucket, not extra capacity
# on the same one.
DEFAULT_MODEL = "gemini-flash-latest"
FALLBACK_MODELS = ["gemini-flash-lite-latest"]

# gemini-flash-latest is a THINKING model: it spends output tokens on internal reasoning before
# emitting any text, so a small budget returns finishReason=MAX_TOKENS with no content at all --
# an empty answer, not a short one. Measured live: 60 tokens produced nothing, 300 produced fenced
# JSON, 1000 produced clean JSON. Callers across this codebase pass 200-800 for small extractions,
# which was correct for flash-lite and silently produces nothing here.
#
# The floor applies only to models that reason before answering; flash-lite is unaffected and keeps
# whatever the caller asked for.
THINKING_MODELS = {"gemini-flash-latest"}
THINKING_MIN_MAX_TOKENS = 1200

TRANSIENT_STATUS = {500, 502, 503, 504}
TRANSIENT_MAX_RETRIES = 3
TRANSIENT_BACKOFF_SECONDS = 2
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
    if model in THINKING_MODELS:
        max_tokens = max(max_tokens, THINKING_MIN_MAX_TOKENS)
    api_key = _get_api_key(db, tenant_id)
    url = BASE_URL_TEMPLATE.format(model=model)
    params = {"key": api_key}
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    response = httpx.post(url, params=params, headers=headers, json=payload, timeout=60)
    # 503/500 are Gemini being transiently overloaded, not a quota wall or a bad request. Without a
    # retry a single blip fails the calling stage outright -- and with Anthropic out of credit there
    # is no working fallback to catch it, so one 503 becomes a lost stage. Short, bounded, and only
    # for the transient codes: a 429 must still fall through to the model-level quota fallback in
    # llm_client, and a 400 must still fail loudly.
    for attempt in range(TRANSIENT_MAX_RETRIES):
        if response.status_code not in TRANSIENT_STATUS:
            break
        time.sleep(TRANSIENT_BACKOFF_SECONDS * (attempt + 1))
        logger.warning("gemini: %s from %s, retry %d/%d", response.status_code, model, attempt + 1, TRANSIENT_MAX_RETRIES)
        response = httpx.post(url, params=params, headers=headers, json=payload, timeout=60)

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
        # Close on the NEXT fence, not the end of the string, and decode with raw_decode so any
        # trailing commentary is ignored. Identical to the fix applied to call_claude_json on
        # 2026-09-03: an end-anchored strip leaves prose attached whenever the model adds a note
        # after the closing fence, and the parse then fails on valid JSON. The docstring's claim
        # that "Gemini tends to return clean JSON without markdown fences" held for flash-lite and
        # is false for gemini-flash-latest, which fences every response -- so this path went from
        # defensive to load-bearing the moment the model changed.
        body = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        cleaned = body.split("```", 1)[0].strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        obj, _ = json.JSONDecoder().raw_decode(cleaned)
    except ValueError as e:
        raise GeminiError(f"Gemini did not return valid JSON: {e}. Raw response: {text[:500]}") from e
    if not isinstance(obj, dict):
        raise GeminiError(f"Gemini returned {type(obj).__name__}, not a JSON object. Raw response: {text[:500]}")
    return obj
