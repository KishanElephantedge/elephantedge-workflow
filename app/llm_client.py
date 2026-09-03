"""Unified LLM entrypoint.

CLAUDE HAIKU IS PRIMARY, GEMINI IS THE FALLBACK (2026-09-03). It used to be the other way round,
on the reasoning that Gemini was cheaper at equivalent quality for these extraction/synthesis
tasks. That reasoning held until the call volume grew: a single daily sweep now makes ~525 LLM
calls, and Gemini's free tier allows 15 per minute.

Measured on run 122: 296 of 525 Gemini calls were REJECTED for quota, each one asking for a ~40
second retry. The consequences went well beyond slowness:

  - runs stretched from ~27 minutes to 233,
  - and the idle windows those retries created were long enough for Neon to drop the database
    connection mid-sweep, which killed the run outright ("server closed the connection
    unexpectedly", then PendingRollbackError for everything after it).

So the free tier was not actually free -- it cost a day's output. Haiku is paid but has no such
limit: roughly $0.0013 per call, about $0.70 for a full sweep.

BUT IT IS NOT SWITCHED ON YET. The Anthropic account currently has no credit -- every call returns
"Your credit balance is too low to access the Anthropic API" (400) -- so PRIMARY stays "gemini"
until that is topped up. Flipping it before then would add a guaranteed failed request in front of
every Gemini call. One word changes it once there is balance.

Whichever is primary, the other is the fallback, so an outage or an exhausted balance on either
side degrades to a working model instead of failing the run.
"""
import logging

from sqlalchemy.orm import Session

from app.claude_client import ClaudeError, call_claude, call_claude_json
from app.gemini_client import GeminiError, call_gemini, call_gemini_json

logger = logging.getLogger("llm_client")

# Set to "claude" the moment the Anthropic account has credit -- that is the whole point of this
# switch and the reason both paths are wired. It is "gemini" today ONLY because Anthropic returns
# "Your credit balance is too low to access the Anthropic API" (400) on every call, so making
# Claude primary would add a guaranteed failed request before each Gemini one: strictly worse than
# the rate limiting it was meant to fix.
PRIMARY = "gemini"  # "claude" | "gemini"


def generate_text(prompt: str, db: Session, tenant_id: int, max_tokens: int = 2000) -> str:
    if PRIMARY == "gemini":
        try:
            return call_gemini(prompt, db, tenant_id, max_tokens=max_tokens)
        except GeminiError as e:
            logger.warning("Gemini call failed, falling back to Claude Haiku: %s", e)
            return call_claude(prompt, db, tenant_id, max_tokens=max_tokens)
    try:
        return call_claude(prompt, db, tenant_id, max_tokens=max_tokens)
    except ClaudeError as e:
        logger.warning("Claude call failed, falling back to Gemini: %s", e)
        return call_gemini(prompt, db, tenant_id, max_tokens=max_tokens)


def generate_json(prompt: str, db: Session, tenant_id: int, max_tokens: int = 2000) -> dict:
    if PRIMARY == "gemini":
        try:
            return call_gemini_json(prompt, db, tenant_id, max_tokens=max_tokens)
        except GeminiError as e:
            logger.warning("Gemini call failed, falling back to Claude Haiku: %s", e)
            return call_claude_json(prompt, db, tenant_id, max_tokens=max_tokens)
    try:
        return call_claude_json(prompt, db, tenant_id, max_tokens=max_tokens)
    except ClaudeError as e:
        logger.warning("Claude call failed, falling back to Gemini: %s", e)
        return call_gemini_json(prompt, db, tenant_id, max_tokens=max_tokens)
