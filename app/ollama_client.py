"""Self-hosted open-weight model provider (Ollama), as a third option alongside Claude and
Gemini -- same call signature as both, so llm_client.py can treat it identically.

WHY THIS EXISTS. The problem it addresses is not cost, it is the rate limit. Gemini's free
tier has repeatedly destroyed whole runs (see llm_client.py's own memo: run 122 stretched from
27 minutes to 233 and then died when the idle windows dropped the database connection; run 124
made 368 quota-rejected requests to get 1 useful answer). A model we host has no per-minute or
per-day quota at all.

MEASURED, NOT ASSUMED (2026-09-13, llama3.1:8b, the real classify_job_description prompt):

    CPU-only, no GPU        ~9s per call    -> ~79 min for a 525-call sweep
    Apple Silicon / Metal   ~2s per call    -> ~19 min

So a GPU is genuinely not required for our batch workloads. Speed was never the blocker.

THE ACTUAL BLOCKER IS THE PROMPT, AND IT IS WHY THIS IS OFF BY DEFAULT. Our prompts were
written for Claude/Gemini, which infer an intended default from prose. Run verbatim against
llama3.1:8b, the real job-classification prompt scored 3/6 -- and every single miss was a FALSE
POSITIVE on first_sales_hire_signal, the exact category that promotes a company into the
pipeline. The existing quote-verification guard does not catch this: 4 of 5 bad answers quoted
the source text verbatim and so passed straight through.

Rewriting the same task with the decision rule stated explicitly (ordered rules, named traps,
an explicit default of "ordinary_hire") scored 6/6 on the same cases, on CPU.

The conclusion is therefore per-call-site, not global: a local model is viable for the narrow
classification work, but ONLY with a prompt written for it and validated against real data.
Pointing the existing prompts at this provider wholesale would flood the pipeline with
false positives, which is strictly worse than the rate limiting it would be fixing.
"""
import json
import logging

import httpx
from sqlalchemy.orm import Session

from app.db.models import Parameter

logger = logging.getLogger("ollama_client")

# Overridable per tenant via an `ollama_base_url` Parameter, for the same reason every other
# provider reads its credential from the database: a self-hosted model may run anywhere.
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.1:latest"

# Generous because CPU inference is legitimately slow -- a 4000-character job description took
# ~16s in the measurements above, and a timeout shorter than the real work would turn a working
# setup into a mysterious failure.
TIMEOUT_SECONDS = 300


class OllamaError(Exception):
    pass


def _get_base_url(db: Session, tenant_id: int) -> str:
    row = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id, Parameter.key == "ollama_base_url")
        .first()
    )
    if row and isinstance(row.value, dict):
        url = row.value.get("url")
        if url:
            return str(url).rstrip("/")
    return DEFAULT_BASE_URL


def call_ollama(prompt: str, db: Session, tenant_id: int, max_tokens: int = 2000, model: str = DEFAULT_MODEL) -> str:
    base_url = _get_base_url(db, tenant_id)
    try:
        response = httpx.post(
            f"{base_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                # temperature 0: these are classification/extraction calls, where the same input
                # should always produce the same answer. Sampling variety is a liability here.
                "options": {"temperature": 0, "num_predict": max_tokens},
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        raise OllamaError(f"Could not reach Ollama at {base_url}: {e}") from e

    if response.status_code >= 300:
        raise OllamaError(f"Ollama returned {response.status_code}: {response.text[:500]}")
    return (response.json().get("response") or "").strip()


def call_ollama_json(prompt: str, db: Session, tenant_id: int, max_tokens: int = 2000, model: str = DEFAULT_MODEL) -> dict:
    """Uses Ollama's native JSON mode (`format: "json"`), which constrains decoding to valid
    JSON rather than asking politely for it. Measured 6/6 valid responses where the same model
    in free-text mode still needs fence-stripping -- so this deliberately does NOT reuse the
    markdown-fence cleanup call_gemini_json/call_claude_json need."""
    base_url = _get_base_url(db, tenant_id)
    try:
        response = httpx.post(
            f"{base_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0, "num_predict": max_tokens},
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        raise OllamaError(f"Could not reach Ollama at {base_url}: {e}") from e

    if response.status_code >= 300:
        raise OllamaError(f"Ollama returned {response.status_code}: {response.text[:500]}")

    text = (response.json().get("response") or "").strip()
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise OllamaError(f"Ollama did not return valid JSON: {e}. Raw response: {text[:500]}") from e
    if not isinstance(obj, dict):
        raise OllamaError(f"Ollama returned {type(obj).__name__}, not a JSON object. Raw response: {text[:500]}")
    return obj


def is_available(db: Session, tenant_id: int) -> bool:
    """Cheap liveness check, so a caller can decide to use a local model without paying a full
    inference timeout to discover nothing is listening."""
    base_url = _get_base_url(db, tenant_id)
    try:
        response = httpx.get(f"{base_url}/api/tags", timeout=3)
        return response.status_code < 300
    except httpx.HTTPError:
        return False
