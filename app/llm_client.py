"""Unified LLM entrypoint for Phase 13 -- tries Gemini first (cheaper, confirmed equivalent
output quality for these extraction/synthesis tasks), falls back to Claude Haiku if Gemini is
unavailable for any reason (quota exhausted, outage, credential not set)."""
import logging

from sqlalchemy.orm import Session

from app.claude_client import call_claude, call_claude_json
from app.gemini_client import GeminiError, call_gemini, call_gemini_json

logger = logging.getLogger("llm_client")


def generate_text(prompt: str, db: Session, tenant_id: int, max_tokens: int = 2000) -> str:
    try:
        return call_gemini(prompt, db, tenant_id, max_tokens=max_tokens)
    except GeminiError as e:
        logger.warning("Gemini call failed, falling back to Claude Haiku: %s", e)
        return call_claude(prompt, db, tenant_id, max_tokens=max_tokens)


def generate_json(prompt: str, db: Session, tenant_id: int, max_tokens: int = 2000) -> dict:
    try:
        return call_gemini_json(prompt, db, tenant_id, max_tokens=max_tokens)
    except GeminiError as e:
        logger.warning("Gemini call failed, falling back to Claude Haiku: %s", e)
        return call_claude_json(prompt, db, tenant_id, max_tokens=max_tokens)
