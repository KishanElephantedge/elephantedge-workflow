"""Structured partner ICP -- turns GTM Partners' own ICP text into fields the matcher can filter on.

THE PROBLEM. A partner's ICP has only ever existed as prose: LinkedinMonitorProfile.sells_to (a
sentence) and gtm_university_data["icp"] (a formatted block from GTM Partners' own directory).
_get_candidate_companies() therefore pre-filtered on nothing but "has an industry", and the LLM
was asked to match against a paragraph. Confirmed twice against real partners, both reaching real
people:

  - Remy Piazza: his stored ICP literally reads "$3M - $30M ARR with less than 100 employees",
    and the matcher still returned generic IT/security companies with NO revenue figure at all --
    a revenue band cannot be applied when it is a sentence, and none of the matched companies had
    revenue data to apply it to.
  - Abbas Shivji: rejected 2 of 3 companies for geography and headcount reasons that were both
    already stated in his ICP.

WHAT THIS DOES. Parses the ICP prose ONCE into real fields (industries, revenue band, headcount
band, geographies, stages, and explicit exclusions), stored on the profile, so the candidate query
can filter deterministically BEFORE the LLM ranks anything. The LLM keeps doing what it is good at
-- judging fit in language -- and stops being asked to enforce numeric bands it cannot enforce.

WHY AN LLM PARSES IT. The source is human-written prose with inconsistent shapes ("$7M-$150M ARR",
"Seed and Series A-B", "less than 100 employees"). Regex over that would silently mis-parse, and a
wrong revenue band is worse than none -- it would filter out good companies invisibly. So the model
parses, and every parsed value is checked back against the source text (_verify_parsed) before it
is stored: a band whose numbers do not appear in the text is dropped rather than trusted.

NOT A NEW TAXONOMY. Fields map onto columns that already exist on Company (industry,
estimated_revenue_*, employee_count, location) -- nothing here invents a new matching concept.
"""

import json
import logging
import re

from sqlalchemy.orm import Session

from app.db.models import LinkedinMonitorProfile
from app.llm_client import generate_json

logger = logging.getLogger(__name__)

PARSE_PROMPT = """Extract a structured ICP from this partner's own description.

PARTNER ICP TEXT:
{icp_text}

ALSO STATED (may add detail, may be empty):
{sells_to}

Rules:
- Extract ONLY what the text states. Never infer a band, industry or geography that is not written.
- Revenue in whole US dollars ("$7M-$150M ARR" -> 7000000 / 150000000). "less than 100 employees"
  -> employee_max 100, employee_min null.
- industries: short lowercase keywords a database LIKE-match would find (e.g. "fintech",
  "education", "saas", "healthcare"), not full sentences.
- exclusions: things the partner explicitly says are NOT a fit.
- Use null for anything not stated. Missing is fine; guessing is not.

Return JSON exactly:
{{"industries": ["..."], "geographies": ["..."], "stages": ["..."],
  "revenue_min_usd": <int|null>, "revenue_max_usd": <int|null>,
  "employee_min": <int|null>, "employee_max": <int|null>,
  "exclusions": ["..."]}}"""


def _verify_parsed(parsed: dict, source_text: str) -> dict:
    """Drops numeric bands whose digits do not appear in the source.

    A hallucinated revenue band is worse than no band: it silently excludes good companies and
    nobody can see why. Checked by looking for the number's leading digits in the text in any
    common form ($7M, 7 million, 7,000,000)."""
    text = source_text.lower().replace(",", "")

    def stated(value: int | None) -> bool:
        if value is None:
            return True
        millions = value / 1_000_000
        candidates = {str(value), f"{millions:g}m", f"{millions:g} million", f"{millions:g}"}
        return any(c in text for c in candidates)

    cleaned = dict(parsed)
    for field in ("revenue_min_usd", "revenue_max_usd", "employee_min", "employee_max"):
        value = cleaned.get(field)
        if isinstance(value, int) and not stated(value):
            logger.warning("partner_icp: dropped %s=%s -- not found in the source text", field, value)
            cleaned[field] = None
    return cleaned


def parse_partner_icp(db: Session, tenant_id: int, profile: LinkedinMonitorProfile) -> dict | None:
    """Parses one partner's ICP prose into fields. Returns None when there is nothing to parse."""
    gtm = profile.gtm_university_data or {}
    icp_text = (gtm.get("icp") or "").strip()
    sells_to = (profile.sells_to or "").strip()
    if not icp_text and not sells_to:
        return None

    try:
        parsed = generate_json(PARSE_PROMPT.format(icp_text=icp_text or "(none)", sells_to=sells_to or "(none)"), db, tenant_id, max_tokens=800)
    except Exception as e:  # noqa: BLE001 -- one partner's parse failure must not abort a sweep
        logger.warning("partner_icp: parse failed for %r -- %s", profile.name, e)
        return None
    return _verify_parsed(parsed, f"{icp_text}\n{sells_to}")


def get_structured_icp(profile: LinkedinMonitorProfile) -> dict | None:
    """The stored structured ICP, or None. Kept inside gtm_university_data under a distinct key so
    no schema migration is needed and the raw source stays alongside what was derived from it."""
    return (profile.gtm_university_data or {}).get("_structured_icp")


def store_structured_icp(db: Session, profile: LinkedinMonitorProfile, parsed: dict) -> None:
    data = dict(profile.gtm_university_data or {})
    data["_structured_icp"] = parsed
    profile.gtm_university_data = data
    db.add(profile)
    db.commit()


def run_partner_icp_parse_sweep(db: Session, tenant_id: int, limit: int = 200, force: bool = False) -> dict:
    """Parses every partner that has ICP prose but no structured ICP yet. Idempotent."""
    profiles = (
        db.query(LinkedinMonitorProfile)
        .filter(LinkedinMonitorProfile.tenant_id == tenant_id)
        .limit(limit)
        .all()
    )
    result = {"status": "succeeded", "evaluated": 0, "parsed": 0, "skipped_no_text": 0, "already_done": 0, "failed": 0}
    for profile in profiles:
        result["evaluated"] += 1
        if not force and get_structured_icp(profile) is not None:
            result["already_done"] += 1
            continue
        parsed = parse_partner_icp(db, tenant_id, profile)
        if parsed is None:
            result["skipped_no_text"] += 1
            continue
        # One retry: the write races the live sweep, which touches these same rows, and two
        # partners lost their parse to a transient deadlock on the first full run. Re-doing the
        # write costs nothing (the LLM call already happened and its result is in hand); losing it
        # means that partner silently keeps an unfiltered candidate pool.
        for attempt in (1, 2):
            try:
                store_structured_icp(db, profile, parsed)
                result["parsed"] += 1
                break
            except Exception as e:  # noqa: BLE001
                db.rollback()
                if attempt == 1:
                    logger.warning("partner_icp: store retry for %r -- %s", profile.name, e)
                    continue
                result["failed"] += 1
                logger.error("partner_icp: store failed for %r -- %s", profile.name, e)
    return result
