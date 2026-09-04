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


# Models known to be quota-dead, and the UTC date that knowledge belongs to. Gemini's free quota
# is per-day, so this resets naturally when the date rolls over.
#
# WITHOUT THIS, the fallback re-tries every exhausted model on EVERY call. Measured on run 124: 368
# quota-rejected requests for 1 useful answer, because each of ~130 calls walked the whole list
# from the top and got a 429 from each dead model. A rejected request still counts against the
# daily quota, so the retries were themselves consuming the very allowance they were trying to find
# -- the run destroyed more quota failing than it would have used succeeding.
_EXHAUSTED: dict[str, str] = {}

# The free tier enforces TWO separate 429s and they mean opposite things:
#
#   GenerateRequestsPerDayPerProjectPerModel-FreeTier      500/day  -- gone until UTC midnight
#   GenerateRequestsPerMinutePerProjectPerModel-FreeTier   15/min   -- gone for a few seconds
#
# Telling them apart is not optional. The first version of this memo blacklisted a model for the
# whole day on ANY 429, which is wrong and dangerous: measured on run 125, gemini-3.1-flash-lite
# reported 429 twenty-eight times AND served 491 successful calls in the same run. Those were
# per-minute limits. Blacklisting on the first one would have thrown away the model that did 90%
# of the day's useful work, then done the same to the other five in turn, and killed the run
# within minutes -- strictly worse than having no memo at all.
#
# So: only a PerDay violation is permanent. A per-minute limit means wait, not give up.
RATE_LIMIT_SLEEP_SECONDS = 20


def _is_daily_quota(message: str) -> bool:
    """True only for the per-DAY quota. Gemini names the exact quota it charged in the 429 body,
    and GeminiError carries that body verbatim -- so this reads the provider's own words rather
    than guessing from the status code."""
    return "PerDay" in message


def _is_exhausted(model: str) -> bool:
    from datetime import datetime
    return _EXHAUSTED.get(model) == datetime.utcnow().strftime("%Y-%m-%d")


def _mark_exhausted(model: str) -> None:
    from datetime import datetime
    _EXHAUSTED[model] = datetime.utcnow().strftime("%Y-%m-%d")


def _try_models(fn, prompt, db, tenant_id, max_tokens):
    """One pass over the live models. Returns (result, ok, last_error, hit_minute_limit)."""
    import time
    from app.gemini_client import DEFAULT_MODEL, FALLBACK_MODELS

    last = None
    tried = False
    minute_limited = False
    for model in [DEFAULT_MODEL, *FALLBACK_MODELS]:
        if _is_exhausted(model):
            continue  # daily allowance provably gone -- asking again only burns more quota
        tried = True
        try:
            return fn(prompt, db, tenant_id, max_tokens=max_tokens, model=model), True, None, False
        except GeminiError as e:
            last = e
            message = str(e)
            if "429" not in message:
                raise  # a real error, not a quota wall -- do not burn the other models on it
            if _is_daily_quota(message):
                _mark_exhausted(model)
                logger.warning("Gemini model %s is out of its DAILY quota, skipping it until UTC midnight", model)
            else:
                minute_limited = True
                logger.warning("Gemini model %s hit its per-minute rate limit, trying the next model", model)
    if not tried:
        raise GeminiError("all Gemini models are out of daily quota (free tier, 500/day each)")
    return None, False, last, minute_limited


def _gemini_with_model_fallback(fn, prompt, db, tenant_id, max_tokens):
    """Gemini's daily quota is per-MODEL, so an exhausted model is not an exhausted provider.

    Two passes: if the first pass failed ONLY because every live model was inside its 15/min
    window, that is a wait-and-retry condition, not a failure -- sleeping once and retrying costs
    20 seconds and saves the call, where giving up costs the whole stage.
    """
    import time

    result, ok, last, minute_limited = _try_models(fn, prompt, db, tenant_id, max_tokens)
    if ok:
        return result
    if not minute_limited:
        raise last

    logger.warning("All live Gemini models are rate-limited; waiting %ss and retrying once", RATE_LIMIT_SLEEP_SECONDS)
    time.sleep(RATE_LIMIT_SLEEP_SECONDS)
    result, ok, last, _ = _try_models(fn, prompt, db, tenant_id, max_tokens)
    if ok:
        return result
    raise last


def generate_text(prompt: str, db: Session, tenant_id: int, max_tokens: int = 2000) -> str:
    if PRIMARY == "gemini":
        try:
            return _gemini_with_model_fallback(call_gemini, prompt, db, tenant_id, max_tokens)
        except GeminiError as e:
            logger.warning("Gemini call failed, falling back to Claude Haiku: %s", e)
            return call_claude(prompt, db, tenant_id, max_tokens=max_tokens)
    try:
        return call_claude(prompt, db, tenant_id, max_tokens=max_tokens)
    except ClaudeError as e:
        logger.warning("Claude call failed, falling back to Gemini: %s", e)
        return _gemini_with_model_fallback(call_gemini, prompt, db, tenant_id, max_tokens)


def generate_json(prompt: str, db: Session, tenant_id: int, max_tokens: int = 2000) -> dict:
    if PRIMARY == "gemini":
        try:
            return _gemini_with_model_fallback(call_gemini_json, prompt, db, tenant_id, max_tokens)
        except GeminiError as e:
            logger.warning("Gemini call failed, falling back to Claude Haiku: %s", e)
            return call_claude_json(prompt, db, tenant_id, max_tokens=max_tokens)
    try:
        return call_claude_json(prompt, db, tenant_id, max_tokens=max_tokens)
    except ClaudeError as e:
        logger.warning("Claude call failed, falling back to Gemini: %s", e)
        return _gemini_with_model_fallback(call_gemini_json, prompt, db, tenant_id, max_tokens)
