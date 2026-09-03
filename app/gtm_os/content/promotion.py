"""Discovered ContentTopic promotion -- Step 16E-6, Batch 1. Implements exactly the approved
design (topic-promotion-evidence-threshold-design.md): evaluates whether a TopicCandidate cluster
(Step 16E-5's cluster_key grouping) has accumulated enough INDEPENDENT evidence to become a real
ContentTopic(origin="discovered").

    TopicCandidate cluster
        |
        +-- evaluate_cluster()      -- pure, read-only: observation/independence/recency facts
        +-- evaluate_eligibility()  -- pure: structured AND-decision over those facts, no score
        +-- check_configured_resemblance()  -- pure: deterministic match_topic() reuse, reports
        |                              needs_human_review, never merges/auto-transfers
        +-- promote_cluster()       -- the only function that writes: ContentTopic(discovered)
        |                              + ContentTopicEvidence(match_method="promoted_discovered_topic")
        +-- run_candidate_promotion_sweep()  -- tenant-scoped, bounded, idempotent, dry-run capable

This module NEVER modifies or deletes a TopicCandidate row, NEVER modifies GtmSignal, and NEVER
computes trend/velocity/opportunity/demand/offering-fit -- all explicitly out of scope for this
batch, per the approved design's own Sections 13-15."""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.gtm_os.content.promotion_config import get_promotion_config
from app.gtm_os.content.topic import ContentTopic, ContentTopicEvidence, TopicCandidate
from app.gtm_os.content.topics import get_content_topics, match_topic
from app.gtm_os.intelligence.signal import GtmSignal

logger = logging.getLogger(__name__)

PROMOTED_MATCH_METHOD = "promoted_discovered_topic"


def entity_key_for_signal(signal: GtmSignal) -> str:
    """Deterministic independent-entity identity for one GtmSignal, per the approved design
    Section 3 / gpt.txt Part 2's exact rule list. Prefixed by kind so different sources' identity
    spaces never collide. Never invents identity that isn't actually present -- a source with no
    usable identity field falls back to a per-signal-unique key (rule 5: "conservative
    per-observation fallback" -- zero independence credit beyond dedup_key, not a guess).

    Public (no leading underscore) -- reused as-is by trend_intelligence.py (Batch 2) rather than
    duplicated, per that batch's explicit "extract the smallest shared helper, don't duplicate
    this logic" instruction."""
    info = signal.extracted_info or {}

    if signal.source == "rss_article":
        feed_url = info.get("feed_url")
        publisher = info.get("publisher")
        if feed_url:
            return f"rss_feed:{feed_url}"
        if publisher:
            return f"rss_publisher:{publisher}"
        return f"solo:{signal.id}"

    if signal.source in ("linkedin_post", "linkedin_reply"):
        if signal.contact_id:
            return f"contact:{signal.contact_id}"
        if signal.person_name_raw:
            return f"person:{signal.person_name_raw.strip().lower()}"
        return f"solo:{signal.id}"

    if signal.source == "hackernews_story":
        if signal.person_name_raw:
            return f"person:{signal.person_name_raw.strip().lower()}"
        return f"solo:{signal.id}"

    # linkedin_job, theirstack_job, web_search, company_website, and any future/unknown source:
    # no usable identity field per the design's own inspection (Section 1) -- conservative
    # per-observation fallback, never a guess.
    return f"solo:{signal.id}"


def evaluate_cluster(db: Session, tenant_id: int, cluster_key: str) -> dict:
    """Pure, read-only evaluation of one candidate cluster. Returns the descriptive facts from
    the approved design Section 8 -- computed at evaluation time, never stored redundantly (same
    "read, don't recompute" discipline as ContentTopic.evidence_count)."""
    rows = (
        db.query(TopicCandidate)
        .filter(TopicCandidate.tenant_id == tenant_id, TopicCandidate.cluster_key == cluster_key)
        .order_by(TopicCandidate.id)
        .all()
    )
    signal_ids = [r.gtm_signal_id for r in rows]
    signals_by_id = {
        s.id: s
        for s in db.query(GtmSignal).filter(GtmSignal.tenant_id == tenant_id, GtmSignal.id.in_(signal_ids)).all()
    }

    # Rule 1 (design Section 3): dedup_key prevents duplicate observations from counting twice --
    # collapse to one representative signal per distinct dedup_key BEFORE computing independence,
    # so two GtmSignal rows tracing to the same underlying event never inflate the count.
    by_dedup_key: dict[str, GtmSignal] = {}
    for row in rows:
        signal = signals_by_id.get(row.gtm_signal_id)
        if signal is None:
            continue
        if signal.dedup_key not in by_dedup_key:
            by_dedup_key[signal.dedup_key] = signal

    representative_signals = list(by_dedup_key.values())
    observation_count = len(representative_signals)

    entity_keys = {entity_key_for_signal(s) for s in representative_signals}
    independent_entity_count = len(entity_keys)

    distinct_source_types = sorted({s.source for s in representative_signals})

    observed_ats = [s.observed_at for s in representative_signals if s.observed_at is not None]
    if observed_ats:
        first_seen_at = min(observed_ats)
        last_seen_at = max(observed_ats)
    else:
        # No signal in this cluster has a real observed_at -- fall back to TopicCandidate.created_at
        # rather than inventing a timestamp (same discipline as topic_linking.py's own
        # first_seen_at/last_seen_at handling, Step 16E-2).
        created_ats = [r.created_at for r in rows if r.created_at is not None]
        first_seen_at = min(created_ats) if created_ats else None
        last_seen_at = max(created_ats) if created_ats else None

    observation_span_days = (last_seen_at - first_seen_at).days if (first_seen_at and last_seen_at) else 0

    canonical_row = rows[0] if rows else None  # lowest id = the cluster's anchor, same convention as candidate_normalization.py
    alias_names = sorted({r.candidate_name for r in rows if canonical_row and r.candidate_name != canonical_row.candidate_name})

    evidence_strength = (
        f"{observation_count} observation(s) from {independent_entity_count} independent "
        f"source(s) ({', '.join(distinct_source_types) or 'none'}) spanning "
        f"{observation_span_days} day(s), last seen "
        f"{last_seen_at.date().isoformat() if last_seen_at else 'unknown'}"
    )

    return {
        "cluster_key": cluster_key,
        "candidate_rows": rows,
        "canonical_name": canonical_row.candidate_name if canonical_row else None,
        "aliases": alias_names,
        "observation_count": observation_count,
        "independent_entity_count": independent_entity_count,
        "distinct_source_types": distinct_source_types,
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "observation_span_days": observation_span_days,
        "evidence_strength": evidence_strength,
        "signal_ids": sorted({r.gtm_signal_id for r in rows}),
    }


def evaluate_eligibility(facts: dict, config: dict, now: datetime | None = None) -> dict:
    """Pure structured AND-decision (approved design Section 8) -- never a single opaque score.
    `now` is injectable for deterministic testing; defaults to the real current time."""
    now = now or datetime.utcnow()

    reasons_failed = []

    if facts["independent_entity_count"] == 1:
        # Hard, non-calibration floor (design Section 8): a logical consequence of the
        # independence definition itself, not a guessable threshold -- checked before, and
        # independently of, the configured minimum.
        reasons_failed.append("independent_entity_count == 1 (single-entity evidence can never promote)")
    elif facts["independent_entity_count"] < config["min_independent_entities"]:
        reasons_failed.append(
            f"independent_entity_count {facts['independent_entity_count']} < required {config['min_independent_entities']}"
        )

    if facts["observation_count"] < config["min_observations"]:
        reasons_failed.append(f"observation_count {facts['observation_count']} < required {config['min_observations']}")

    if facts["last_seen_at"] is None:
        reasons_failed.append("no observed_at/created_at timestamp available to evaluate recency")
    else:
        age_days = (now - facts["last_seen_at"]).days
        if age_days > config["recent_window_days"]:
            reasons_failed.append(f"last_seen_at is {age_days} day(s) old, outside the {config['recent_window_days']}-day recent window")

    eligible = len(reasons_failed) == 0
    reason = "sufficient independent, recent evidence" if eligible else "; ".join(reasons_failed)

    return {**facts, "eligible": eligible, "reason": reason}


def check_configured_resemblance(db: Session, tenant_id: int, facts: dict) -> dict:
    """Reuses match_topic() (the existing deterministic configured-topic matcher, Step
    16C/16E-2) against the cluster's own canonical_name and every alias -- exactly the existing
    mechanism, no new/fuzzy/LLM matching introduced for this. If matching is inconclusive
    (nothing matches), conservatively reports no flag rather than guessing, per the approved
    design's own explicit instruction. Never merges, never transfers evidence -- purely a
    reporting flag for a human to act on later."""
    configured_topics = get_content_topics(db, tenant_id)  # all configured topics, not just enabled -- a
    # disabled configured topic can still represent something the business once cared about and
    # would want surfaced for review, unlike matching for actual evidence-linking (Step 16E-2),
    # which correctly only ever uses enabled topics.
    if not configured_topics:
        return {"needs_human_review": False, "resemblance_reason": None}

    names_to_check = [facts["canonical_name"], *facts["aliases"]]
    for name in names_to_check:
        if not name:
            continue
        matched = match_topic(name, configured_topics)
        if matched:
            return {
                "needs_human_review": True,
                "resemblance_reason": (
                    f"candidate name {name!r} matches configured topic {matched['name']!r} "
                    "-- review before treating as a separate discovered topic"
                ),
            }
    return {"needs_human_review": False, "resemblance_reason": None}


def _already_promoted_content_topic_id(db: Session, tenant_id: int, signal_ids: list[int]) -> int | None:
    """Idempotency check: has ANY signal in this cluster already been linked to a discovered
    topic via a prior promotion run? No new field/table needed -- this reuses
    ContentTopicEvidence.match_method, the exact value Step 16E-1 named for this purpose from the
    start."""
    if not signal_ids:
        return None
    existing = (
        db.query(ContentTopicEvidence.content_topic_id)
        .filter(
            ContentTopicEvidence.tenant_id == tenant_id,
            ContentTopicEvidence.gtm_signal_id.in_(signal_ids),
            ContentTopicEvidence.match_method == PROMOTED_MATCH_METHOD,
        )
        .first()
    )
    return existing[0] if existing else None


def promote_cluster(db: Session, tenant_id: int, facts: dict) -> ContentTopic:
    """Creates ContentTopic(origin="discovered") + ContentTopicEvidence rows for every GtmSignal
    referenced by the cluster (approved design Sections 10/11, gpt.txt Parts 4/5). Never modifies
    or deletes any TopicCandidate row or GtmSignal row. Idempotent at the evidence-row level via
    the existing (content_topic_id, gtm_signal_id) unique constraint from Step 16E-1 -- checked
    before insert here too, same discipline as topic_linking.py, so a re-run never even attempts
    a duplicate insert."""
    content_topic = ContentTopic(
        tenant_id=tenant_id,
        canonical_name=facts["canonical_name"],
        aliases=facts["aliases"],
        origin="discovered",
        first_seen_at=facts["first_seen_at"],
        last_seen_at=facts["last_seen_at"],
    )
    db.add(content_topic)
    db.flush()  # need content_topic.id for evidence rows

    already_linked = {
        row[0]
        for row in db.query(ContentTopicEvidence.gtm_signal_id)
        .filter(ContentTopicEvidence.content_topic_id == content_topic.id)
        .all()
    }
    for signal_id in facts["signal_ids"]:
        if signal_id in already_linked:
            continue
        evidence = ContentTopicEvidence(
            tenant_id=tenant_id,
            content_topic_id=content_topic.id,
            gtm_signal_id=signal_id,
            matched_term=None,  # this signal wasn't matched by an alias -- it's evidence for a NEWLY discovered topic, not a configured one
            match_method=PROMOTED_MATCH_METHOD,
        )
        db.add(evidence)

    db.commit()
    return content_topic


def _distinct_cluster_keys(db: Session, tenant_id: int, limit: int) -> list[str]:
    rows = (
        db.query(TopicCandidate.cluster_key, TopicCandidate.id)
        .filter(TopicCandidate.tenant_id == tenant_id, TopicCandidate.cluster_key.isnot(None))
        .order_by(TopicCandidate.id)
        .all()
    )
    seen: list[str] = []
    seen_set = set()
    for cluster_key, _id in rows:
        if cluster_key not in seen_set:
            seen_set.add(cluster_key)
            seen.append(cluster_key)
        if len(seen) >= limit:
            break
    return seen


def run_candidate_promotion_sweep(
    db: Session,
    tenant_id: int,
    limit: int = 200,
    dry_run: bool = False,
) -> dict:
    """Evaluates up to `limit` distinct candidate clusters for one tenant and promotes every
    eligible, not-yet-promoted one to a discovered ContentTopic. `dry_run=True` evaluates and
    reports exactly what WOULD happen but makes zero database writes (Part 8) -- achieved simply
    by never calling promote_cluster() in that mode, since evaluate_cluster()/evaluate_eligibility()/
    check_configured_resemblance() are all pure reads already.

    One cluster's failure never aborts the sweep -- same error-isolation style as
    app/gtm_os/orchestration/sweep.py's own per-source try/except (each cluster gets its own
    boundary, broad `except Exception`, logged and counted, never re-raised)."""
    config = get_promotion_config(db, tenant_id)

    counts = {
        "evaluated": 0,
        "eligible": 0,
        "promoted": 0,
        "not_yet_eligible": 0,
        "already_promoted_skipped": 0,
        "needs_human_review": 0,
        "failures": 0,
        "dry_run": dry_run,
    }

    cluster_keys = _distinct_cluster_keys(db, tenant_id, limit)

    for cluster_key in cluster_keys:
        try:
            facts = evaluate_cluster(db, tenant_id, cluster_key)
            counts["evaluated"] += 1

            already_topic_id = _already_promoted_content_topic_id(db, tenant_id, facts["signal_ids"])
            if already_topic_id is not None:
                counts["already_promoted_skipped"] += 1
                continue

            eligibility = evaluate_eligibility(facts, config)
            resemblance = check_configured_resemblance(db, tenant_id, facts)
            if resemblance["needs_human_review"]:
                counts["needs_human_review"] += 1
                logger.info(
                    "candidate_promotion: cluster %r flagged for human review -- %s",
                    cluster_key, resemblance["resemblance_reason"],
                )

            if not eligibility["eligible"]:
                counts["not_yet_eligible"] += 1
                continue

            counts["eligible"] += 1
            if dry_run:
                continue

            promote_cluster(db, tenant_id, facts)
            counts["promoted"] += 1

        except Exception as e:  # noqa: BLE001 -- one cluster's failure must never block the others, see docstring
            # Roll back before anything else: a per-item failure must not poison the SHARED session.
            # Neon drops idle connections, and this sweep idles ~40s per LLM call under Gemini rate
            # limiting -- when that lands mid-loop the transaction is left invalid and EVERY later item
            # AND stage dies with PendingRollbackError. Confirmed live: run 122 lost its connection
            # during topic_linking, then burned 233 minutes failing everything after it. The stage-level
            # handler already rolled back; these per-item ones did not.
            db.rollback()
            counts["failures"] += 1
            logger.error("candidate_promotion: cluster %r failed to evaluate/promote: %s", cluster_key, e)

    return counts
