"""Deterministic configured-topic linking -- Step 16E-2. The first functional Topic Intelligence
layer:

    GtmSignal -> enabled ContentTopic -> match_topic() -> ContentTopicEvidence

Reuses get_enabled_content_topics()/match_topic() exactly as established in Step 16A/16C --
nothing here upgrades matching to embeddings/fuzzy/LLM/semantic search, and nothing here creates
TopicCandidate rows or calls an LLM (that's a later, separate step; see
topic-extraction-normalization-design.md).

CONFIGURED-TOPIC SYNC / RENAME BEHAVIOR (a real design decision, documented rather than invented
silently): content_intelligence_topics (topics.py) has no stable topic_id -- a configured topic
is only ever identified by its name. So ContentTopic sync below matches/reuses an existing row by
(tenant_id, canonical_name) case-insensitively; there is no other stable key available. This means
renaming a configured topic is NOT tracked as a rename -- it looks like retiring the old name
(its ContentTopic row and all ContentTopicEvidence remain exactly as they are, permanently
queryable, simply stop receiving new evidence) and introducing a new one under the new name. This
is a direct consequence of topics.py's own existing shape (Step 16A), not a limitation invented
here, and is the non-destructive, conservative choice: nothing is ever deleted, merged, or
silently reattributed. A stable topic_id in the Parameter config is the only way to do better,
and that's a change to topics.py's own schema -- out of scope for this step."""

from sqlalchemy.orm import Session

from app.gtm_os.content.topic import ContentTopic, ContentTopicEvidence
from app.gtm_os.content.topics import get_enabled_content_topics, match_topic
from app.gtm_os.intelligence.signal import GtmSignal

# The extracted_info keys that may carry human-readable text, across every source currently
# sensed (linkedin_job/linkedin_post/linkedin_reply/theirstack_job/hackernews_story/rss_article/
# web_search/company_website -- see sensing.py). Deliberately field-name-based, not
# source-name-based: every source's extracted_info already uses one or more of these exact key
# names for its text content (confirmed by inspection), so this needs zero per-source branching
# and needs no changes when a future source adapter is added, as long as it reuses these same
# field names (title/text/summary/content) for its own text -- the established convention already
# in use throughout sensing.py.
TEXT_FIELDS = ("title", "text", "summary", "content")


def extract_signal_text(signal: GtmSignal) -> str:
    """Smallest possible source-agnostic text extraction: concatenates whatever of
    title/text/summary/content is present in extracted_info. Returns "" (not None) when nothing
    is present -- match_topic() already treats falsy text as "no match", so callers don't need a
    separate empty-text branch.

    Public (no leading underscore) -- reused as-is by candidate_filter.py (Step 16E-3) rather
    than duplicated, per that step's explicit instruction."""
    info = signal.extracted_info or {}
    parts = [info[key] for key in TEXT_FIELDS if info.get(key)]
    return " ".join(parts)


def _matched_term(text: str, topic: dict) -> str | None:
    """Determines WHICH candidate (the topic's name or one of its aliases) actually matched --
    match_topic() itself only reports whether a topic matched, not which term, so this recovers
    it using the exact same case-insensitive substring predicate match_topic() uses. Not a second
    matching engine: it makes no match/no-match decision of its own, it only explains a decision
    match_topic() already made, for provenance (ContentTopicEvidence.matched_term)."""
    lowered = text.lower()
    for candidate in [topic["name"], *topic.get("aliases", [])]:
        if candidate.lower() in lowered:
            return candidate
    return None


def _get_or_create_content_topic(db: Session, tenant_id: int, configured_topic: dict, existing_by_name: dict) -> ContentTopic:
    """Reuses an existing ContentTopic keyed by (tenant_id, canonical_name) case-insensitively --
    the only stable key available, see module docstring. Keeps aliases in sync with the current
    Parameter configuration (a non-destructive field update, never a delete) so ContentTopic
    remains an accurate mirror of what's currently configured."""
    key = configured_topic["name"].strip().lower()
    existing = existing_by_name.get(key)
    if existing is not None:
        current_aliases = configured_topic.get("aliases", [])
        if existing.aliases != current_aliases:
            existing.aliases = current_aliases
        return existing

    content_topic = ContentTopic(
        tenant_id=tenant_id,
        canonical_name=configured_topic["name"].strip(),
        aliases=configured_topic.get("aliases", []),
        origin="configured",
    )
    db.add(content_topic)
    db.flush()  # need content_topic.id for evidence rows created in this same sweep
    existing_by_name[key] = content_topic
    return content_topic


def run_content_topic_linking_sweep(
    db: Session,
    tenant_id: int,
    sources: list[str] | None = None,
    limit: int = 500,
) -> dict:
    """Links GtmSignal rows for one tenant against that tenant's enabled configured topics.
    Source-agnostic: `sources` optionally narrows which GtmSignal.source values are considered
    (defaults to every source, no hardcoded list -- this layer doesn't need to know source names
    to work). Safe to re-run: an already-linked (topic, signal) pair is looked up before any
    insert is attempted, and the unique index on (content_topic_id, gtm_signal_id) from Step
    16E-1 is the final backstop.

    Does nothing with signals that match no configured topic -- no TopicCandidate, no LLM call,
    no discovered topic. That's later, separate work (see module docstring)."""
    configured_topics = get_enabled_content_topics(db, tenant_id)

    existing_topics = db.query(ContentTopic).filter(ContentTopic.tenant_id == tenant_id).all()
    existing_by_name = {t.canonical_name.strip().lower(): t for t in existing_topics}

    content_topic_by_config_name: dict[str, ContentTopic] = {}
    for configured_topic in configured_topics:
        content_topic = _get_or_create_content_topic(db, tenant_id, configured_topic, existing_by_name)
        content_topic_by_config_name[configured_topic["name"].strip().lower()] = content_topic
    db.commit()

    result = {
        "signals_seen": 0,
        "configured_topics_seen": len(configured_topics),
        "matches_found": 0,
        "evidence_created": 0,
        "already_linked": 0,
        "unmatched_signals": 0,
    }

    if not configured_topics:
        return result

    query = db.query(GtmSignal).filter(GtmSignal.tenant_id == tenant_id)
    if sources is not None:
        query = query.filter(GtmSignal.source.in_(sources))
    query = query.order_by(GtmSignal.id).limit(limit)

    for signal in query:
        result["signals_seen"] += 1
        text = extract_signal_text(signal)

        already_linked_topic_ids = {
            row[0]
            for row in db.query(ContentTopicEvidence.content_topic_id)
            .filter(ContentTopicEvidence.gtm_signal_id == signal.id)
            .all()
        }

        signal_matched_any = False
        if text:
            for configured_topic in configured_topics:
                if not match_topic(text, [configured_topic]):
                    continue
                signal_matched_any = True
                result["matches_found"] += 1

                content_topic = content_topic_by_config_name[configured_topic["name"].strip().lower()]

                if content_topic.id in already_linked_topic_ids:
                    result["already_linked"] += 1
                    continue

                term = _matched_term(text, configured_topic)
                evidence = ContentTopicEvidence(
                    tenant_id=tenant_id,
                    content_topic_id=content_topic.id,
                    gtm_signal_id=signal.id,
                    matched_term=term,
                    match_method="deterministic_alias_match",
                )
                db.add(evidence)
                already_linked_topic_ids.add(content_topic.id)
                result["evidence_created"] += 1

                if signal.observed_at is not None:
                    if content_topic.first_seen_at is None or signal.observed_at < content_topic.first_seen_at:
                        content_topic.first_seen_at = signal.observed_at
                    if content_topic.last_seen_at is None or signal.observed_at > content_topic.last_seen_at:
                        content_topic.last_seen_at = signal.observed_at

        if not signal_matched_any:
            result["unmatched_signals"] += 1

    db.commit()
    return result
