"""Content Intelligence topic configuration -- Step 16A. Foundation only: no topic extraction,
no clustering, no trend detection, no source sensing. Defines the source-agnostic canonical
topic/alias structure future Reddit/X/YouTube/News/Hacker News/... adapters will eventually
consume (per content-intelligence-architecture.md's own Part 2/8 design).

Deliberately SEPARATE from app/phases/linkedin_monitor.py's KEYWORDS_PARAMETER_KEY/
get_keyword_tiers()/set_keyword_tiers() -- that configuration is a different feature (competitor/
partner post watching: 4 fixed tiers, plain keyword strings, no canonical-topic/alias concept)
and is NOT read, modified, or migrated by anything in this module. Same underlying `Parameter`
storage mechanism is reused (same pattern as linkedin_monitor.py and business_context.py), but
under its own key, with its own shape.

A topic is the canonical concept ("AI Sales Automation"); aliases are the different ways that
concept may appear in raw source content ("AI SDR", "AI sales agent", "AI outbound",
"automated SDR", "AI prospecting"). Deliberately NO source-specific fields anywhere in this
structure (no `linkedin_keywords`/`reddit_keywords`/etc.) -- a topic must remain meaningful to
every future source adapter without being rewritten per source, per the Step 16 design doc's own
explicit instruction. Deliberately NOT implementing semantic/embedding matching here either --
only `aliases` (plain strings) exist today; a later `canonical topic -> aliases -> semantic
cluster` evolution should be able to build on this shape without redesigning it (a semantic
cluster ID would be an additive field on the same topic object, not a structural change)."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

TOPICS_PARAMETER_KEY = "content_intelligence_topics"

# Historical note: this used to be a hardcoded empty list, on the reasoning that no real starter
# topic list had been provided and an invented example list would misrepresent itself as real
# configuration. That reasoning still holds -- nothing below invents topics -- but it left
# sense_hackernews_stories()/sense_rss_articles() permanently senseless (zero enabled topics ->
# zero signals, forever, confirmed against production 2026-08-18) even after real RSS feeds were
# configured. get_content_topics() below now computes a DERIVED default from this tenant's own
# real ICP/offering/business-context config when no human override has been saved yet -- same
# derivation module (business_terms.py) and same "no override -> compute fresh from live config"
# pattern already proven by linkedin_search_config.py's get_linkedin_search_config(). Kept as an
# empty list here only as the argument-signature fallback for callers that don't pass db/tenant_id.
DEFAULT_TOPICS: list[dict] = []


class TopicConfigError(ValueError):
    """Raised when a topic configuration fails validation. Never silently coerced or dropped --
    an invalid config is rejected outright, matching this project's own established discipline
    of failing loudly rather than guessing (e.g. GtmSignal interpreters returning None rather
    than forcing a guess)."""


def _validate_topics(topics: list[dict]) -> None:
    if not isinstance(topics, list):
        raise TopicConfigError("topics must be a list")

    seen_names: set[str] = set()
    for i, topic in enumerate(topics):
        if not isinstance(topic, dict):
            raise TopicConfigError(f"topic at index {i} must be an object")

        name = topic.get("name")
        if not isinstance(name, str) or not name.strip():
            raise TopicConfigError(f"topic at index {i} has an empty or missing name")
        normalized_name = name.strip().lower()
        if normalized_name in seen_names:
            raise TopicConfigError(f"duplicate topic name: {name!r}")
        seen_names.add(normalized_name)

        aliases = topic.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases):
            raise TopicConfigError(f"topic {name!r} has malformed aliases -- must be a list of non-empty strings")

        enabled = topic.get("enabled", True)
        if not isinstance(enabled, bool):
            raise TopicConfigError(f"topic {name!r} has a malformed 'enabled' value -- must be true/false")

        description = topic.get("description")
        if description is not None and not isinstance(description, str):
            raise TopicConfigError(f"topic {name!r} has a malformed description -- must be a string or null")

        category = topic.get("category")
        if category is not None and not isinstance(category, str):
            raise TopicConfigError(f"topic {name!r} has a malformed category -- must be a string or null")


def _normalize_topic(topic: dict) -> dict:
    """Dedupes aliases within one topic (case-insensitive, first-seen order preserved) -- a
    trivial cleanup, not treated as a validation error (unlike a duplicate TOPIC name, which is a
    real configuration mistake worth rejecting outright)."""
    seen: set[str] = set()
    deduped_aliases = []
    for alias in topic.get("aliases", []):
        key = alias.strip().lower()
        if key not in seen:
            seen.add(key)
            deduped_aliases.append(alias.strip())
    return {
        "name": topic["name"].strip(),
        "aliases": deduped_aliases,
        "enabled": topic.get("enabled", True),
        "description": topic.get("description"),
        "category": topic.get("category"),
    }


def derive_default_topics(icp_config: list[dict], offering_config: list[dict], business_context: dict) -> list[dict]:
    """Pure function -- one Topic per real, deduped business term from ICP names, offering
    names, and business-context icp_band profiles (app/gtm_os/context/business_terms.py, the
    exact same derivation linkedin_search_config.py uses for its own search phrases -- same real
    source data, reused rather than re-derived). No aliases generated (an alias would mean
    guessing a paraphrase of the lead's own words, which this derivation deliberately does not
    do) -- a human can add aliases later via set_content_topics() once real matching results show
    which paraphrases are actually worth adding. `category`/`description` left null for the same
    reason: no invented classification, only the real term itself as both name and match target."""
    from app.gtm_os.context.business_terms import all_business_topic_terms

    terms = all_business_topic_terms(icp_config, offering_config, business_context)
    return [{"name": term, "aliases": [], "enabled": True, "description": None, "category": None} for term in terms]


def get_content_topics(db: Session, tenant_id: int) -> list[dict]:
    """No stored override yet -> topics are computed FRESH from this tenant's live ICP/offering/
    business-context config (derive_default_topics() above), same "no override -> compute fresh"
    pattern as linkedin_search_config.py's get_linkedin_search_config() -- not a frozen snapshot,
    so editing ICP/offering config also updates what HN/RSS sensing looks for, without a separate
    manual sync step. Once a human edits and saves via set_content_topics(), that saved list is
    the source of truth going forward, exactly like the search-phrase config."""
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == TOPICS_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, list) and param.value:
        return param.value

    # Same "empty saved value doesn't count as a real override" convention as
    # linkedin_search_config.py's get_linkedin_search_config() -- falls through to the derived
    # default rather than treating an empty list as "sensing intentionally disabled".
    from app.gtm_os.context.business_context import get_business_context
    from app.gtm_os.icp.icp_config import get_icp_config
    from app.gtm_os.opportunity.offering_config import get_offering_config

    return derive_default_topics(get_icp_config(db, tenant_id), get_offering_config(db, tenant_id), get_business_context(db, tenant_id))


def get_enabled_content_topics(db: Session, tenant_id: int) -> list[dict]:
    """Convenience helper for future sensing/matching code, which will only ever care about
    enabled topics -- avoids every future caller re-implementing the same filter."""
    return [t for t in get_content_topics(db, tenant_id) if t.get("enabled", True)]


def set_content_topics(db: Session, tenant_id: int, topics: list[dict]) -> None:
    """Validates, normalizes (alias dedup only), then upserts -- same get-or-create-then-commit
    pattern already proven by linkedin_monitor.py's set_keyword_tiers() and
    business_context.py's set_business_context(), reused here rather than invented fresh."""
    _validate_topics(topics)
    normalized = [_normalize_topic(t) for t in topics]

    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == TOPICS_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = normalized
    else:
        param = Parameter(
            tenant_id=tenant_id,
            key=TOPICS_PARAMETER_KEY,
            value=normalized,
            description="Content Intelligence topic/alias configuration -- source-agnostic, consumed by future content-sensing adapters (see content-intelligence-architecture.md)",
        )
        db.add(param)
    db.commit()


def match_topic(text: str, topics: list[dict]) -> dict | None:
    """Deterministic, case-insensitive substring match against each topic's name + aliases --
    the same discipline already proven by app/phases/linkedin_monitor.py's match_keywords(), not
    semantic/embedding matching (explicitly out of scope, see content-intelligence-architecture.md
    Part 7's own "deterministic keyword matching first" recommendation).

    Deliberately lives here, not inside any one source adapter -- topic matching is generic
    across every future source (Hacker News, RSS, GDELT, ...), so it's written once and reused,
    not duplicated per adapter. Returns the FIRST matching topic (name or any alias found as a
    substring of `text`), or None if nothing matches. Callers decide what "no match" means for
    their own source (e.g. skip sensing, per hackernews_client.py's sensing adapter)."""
    if not text:
        return None
    lowered = text.lower()
    for topic in topics:
        candidates = [topic["name"], *topic.get("aliases", [])]
        if any(candidate.lower() in lowered for candidate in candidates):
            return topic
    return None
