"""Trend Intelligence -- Batch 2, Parts D-K. The layer Step 16E-6's design explicitly deferred:

    "Promotion establishes that the topic exists.
     Trend Intelligence evaluates whether activity is increasing, persistent, or declining."

Deliberately NOT Kleinberg-style burst-state modeling or statistical forecasting -- there isn't
remotely enough production evidence volume yet to calibrate that against (same reasoning already
used in the Step 16E-6 design doc and promotion_config.py). Instead: a simple, interpretable
two-window comparison (recent vs. previous, both `window_days` long), classified into a small set
of named, explainable states via deterministic thresholds -- never a numeric "trend score".

Operates on ContentTopic + ContentTopicEvidence + GtmSignal ONLY -- no new persistent Trend model.
A trend evaluation is a pure, read-only computation at query time (same "read, don't recompute"
discipline as ContentTopic.evidence_count, and the same reasoning ProblemHypothesis/
DemandHypothesis already established for confidence: derive from evidence, don't store a
snapshot that can go stale). This also means idempotency/dry-run are trivial here: nothing is
ever written, so a rerun with the same underlying data always produces the same result, and
`dry_run` exists purely for interface consistency with this feature's other sweeps.

Works identically for origin="configured" and origin="discovered" ContentTopics -- both are
populated via the exact same ContentTopicEvidence table (Step 16E-2's deterministic_alias_match
rows for configured topics, Batch 1's promoted_discovered_topic rows for discovered ones), so
trend evaluation never needs to know or care which kind of topic it's looking at (Part I/J)."""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.gtm_os.content.promotion import entity_key_for_signal
from app.gtm_os.content.topic import ContentTopic, ContentTopicEvidence
from app.gtm_os.content.trend_config import get_trend_config
from app.gtm_os.intelligence.signal import GtmSignal

logger = logging.getLogger(__name__)

VALID_STATES = {"insufficient_evidence", "emerging", "accelerating", "persistent", "stable", "declining"}


def _effective_time(signal: GtmSignal, evidence: ContentTopicEvidence) -> datetime | None:
    """Prefers the real event time (GtmSignal.observed_at); falls back to when this system
    recorded the evidence link (ContentTopicEvidence.added_at) when the underlying signal has no
    observed_at -- same "use the best real timestamp available, never invent one" discipline
    topic_linking.py and promotion.py both already established."""
    return signal.observed_at or evidence.added_at


def evaluate_topic_trend(db: Session, tenant_id: int, content_topic: ContentTopic, config: dict, now: datetime | None = None) -> dict:
    """Pure, read-only trend evaluation for one ContentTopic. `now` is injectable for
    deterministic testing; defaults to the real current time."""
    now = now or datetime.utcnow()
    window = timedelta(days=config["window_days"])
    recent_start = now - window
    previous_start = now - (2 * window)

    rows = (
        db.query(ContentTopicEvidence, GtmSignal)
        .join(GtmSignal, ContentTopicEvidence.gtm_signal_id == GtmSignal.id)
        .filter(ContentTopicEvidence.tenant_id == tenant_id, ContentTopicEvidence.content_topic_id == content_topic.id)
        .all()
    )

    # dedup_key collapse -- Rule 1, reused unmodified from promotion.py's evaluate_cluster(): the
    # same underlying event linked as evidence twice (via two different GtmSignal rows sharing a
    # dedup_key) must never be counted as two independent observations.
    by_dedup_key: dict[str, tuple[GtmSignal, datetime | None]] = {}
    for evidence, signal in rows:
        if signal.dedup_key in by_dedup_key:
            continue
        by_dedup_key[signal.dedup_key] = (signal, _effective_time(signal, evidence))

    all_time_times = [t for _s, t in by_dedup_key.values() if t is not None]
    first_seen_at = min(all_time_times) if all_time_times else None
    last_seen_at = max(all_time_times) if all_time_times else None
    observation_span_days = (last_seen_at - first_seen_at).days if (first_seen_at and last_seen_at) else 0

    def _window_facts(window_start: datetime, window_end: datetime, *, inclusive_end: bool) -> dict:
        if inclusive_end:
            in_window = [(s, t) for s, t in by_dedup_key.values() if t is not None and window_start <= t <= window_end]
        else:
            in_window = [(s, t) for s, t in by_dedup_key.values() if t is not None and window_start <= t < window_end]
        signals = [s for s, _t in in_window]
        return {
            "observation_count": len(signals),
            "independent_entity_count": len({entity_key_for_signal(s) for s in signals}),
            "distinct_source_types": sorted({s.source for s in signals}),
        }

    # "now" is inclusive for the recent window (an event timestamped exactly at evaluation time
    # must still count) -- but the recent/previous boundary (recent_start) stays exclusive on the
    # previous side, so an event exactly on that boundary is counted once, in the recent window,
    # never double-counted across both.
    recent = _window_facts(recent_start, now, inclusive_end=True)
    previous = _window_facts(previous_start, recent_start, inclusive_end=False)

    state, reason = _classify(recent, previous, observation_span_days, config)

    return {
        "content_topic_id": content_topic.id,
        "canonical_name": content_topic.canonical_name,
        "origin": content_topic.origin,
        "evaluation_time": now,
        "recent_observation_count": recent["observation_count"],
        "previous_observation_count": previous["observation_count"],
        "recent_independent_entity_count": recent["independent_entity_count"],
        "recent_source_diversity": recent["distinct_source_types"],
        "observation_span_days": observation_span_days,
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "state": state,
        "explanation": reason,
    }


def _classify(recent: dict, previous: dict, observation_span_days: int, config: dict) -> tuple[str, str]:
    """Deterministic, explainable, no LLM. Conservative ordering: the "no strong claim" states
    (insufficient_evidence, then persistent/stable) are only reached once the more assertive
    states (emerging/accelerating/declining) have been explicitly ruled out -- per Part G's
    instruction to prefer the less-assertive state whenever the data can't reliably distinguish
    two possibilities."""
    min_obs = config["min_recent_observations"]
    min_entities = config["min_recent_independent_entities"]
    ratio = config["acceleration_ratio"]
    persistent_span = config["persistent_span_days"]

    if recent["observation_count"] < min_obs or recent["independent_entity_count"] < min_entities:
        return "insufficient_evidence", (
            f"recent window has {recent['observation_count']} observation(s) from "
            f"{recent['independent_entity_count']} independent entity/entities -- below the "
            f"configured minimum ({min_obs} observations / {min_entities} entities) to make a trend claim"
        )

    if previous["observation_count"] < min_obs:
        return "emerging", (
            f"recent window shows {recent['observation_count']} observation(s) from "
            f"{recent['independent_entity_count']} independent entities, while the previous "
            f"window had essentially none ({previous['observation_count']}) -- newly appearing activity"
        )

    if recent["observation_count"] >= previous["observation_count"] * ratio:
        return "accelerating", (
            f"recent observation_count ({recent['observation_count']}) is at least {ratio}x the "
            f"previous window's ({previous['observation_count']}) -- activity materially increasing"
        )

    if recent["observation_count"] <= previous["observation_count"] / ratio:
        return "declining", (
            f"recent observation_count ({recent['observation_count']}) is at most 1/{ratio} of the "
            f"previous window's ({previous['observation_count']}) -- activity materially decreasing"
        )

    if observation_span_days >= persistent_span:
        return "persistent", (
            f"activity is steady between the recent ({recent['observation_count']}) and previous "
            f"({previous['observation_count']}) windows, and evidence spans {observation_span_days} "
            f"day(s) overall (>= {persistent_span}) -- a longer-lived, recurring topic"
        )

    return "stable", (
        f"activity is steady between the recent ({recent['observation_count']}) and previous "
        f"({previous['observation_count']}) windows, but the overall evidence span "
        f"({observation_span_days} day(s)) is not yet long enough to call this persistent"
    )


def _distinct_topic_ids(db: Session, tenant_id: int, limit: int) -> list[int]:
    return [
        row[0]
        for row in db.query(ContentTopic.id)
        .filter(ContentTopic.tenant_id == tenant_id)
        .order_by(ContentTopic.id)
        .limit(limit)
        .all()
    ]


def run_trend_intelligence_sweep(db: Session, tenant_id: int, limit: int = 200, dry_run: bool = False) -> dict:
    """Evaluates up to `limit` ContentTopics (both configured and discovered) for one tenant.
    Makes ZERO writes regardless of `dry_run` -- trend evaluation is inherently read-only (see
    module docstring); `dry_run` is accepted purely for interface/orchestrator consistency with
    this feature's other sweeps, which do write. No LLM calls anywhere in this module.

    One topic's failure never aborts the sweep -- same per-item error-isolation style as
    promotion.py's run_candidate_promotion_sweep (broad `except Exception`, logged, counted,
    never re-raised)."""
    config = get_trend_config(db, tenant_id)

    counts = {
        "evaluated": 0,
        "insufficient_evidence": 0,
        "emerging": 0,
        "accelerating": 0,
        "persistent": 0,
        "stable": 0,
        "declining": 0,
        "failed": 0,
        "dry_run": dry_run,
    }

    topic_ids = _distinct_topic_ids(db, tenant_id, limit)

    for topic_id in topic_ids:
        try:
            content_topic = db.get(ContentTopic, topic_id)
            if content_topic is None or content_topic.tenant_id != tenant_id:
                continue
            result = evaluate_topic_trend(db, tenant_id, content_topic, config)
            counts["evaluated"] += 1
            state = result["state"]
            if state in counts:
                counts[state] += 1
        except Exception as e:  # noqa: BLE001 -- one topic's failure must never block the others, see docstring
            counts["failed"] += 1
            logger.error("trend_intelligence: topic id %s failed to evaluate: %s", topic_id, e)

    return counts


def get_market_intelligence_overview(db: Session, tenant_id: int, limit: int = 200) -> dict:
    """V2 Market Intelligence page (Phase 4, Parts 3-5) -- per-topic detail this module's own
    evaluate_topic_trend() already computes, plus the Batch 11 account-bridge coverage count for
    that SAME topic. Never a second trend algorithm, never a numeric trend score, never an
    inferred account relationship -- see module docstring and market_account_context.py's own
    "Case B only" boundary, both reused verbatim.

    ACCOUNT-BRIDGE COUNT PER TOPIC (Part 5): the number of DISTINCT companies for which a
    GtmSignal is simultaneously (a) linked to that company via company_id and (b) linked to this
    ContentTopic via ContentTopicEvidence -- the exact same Case B relationship
    market_account_context.py already defines, just aggregated per-topic here instead of
    per-company. This is NOT "how many companies discussed this topic" in any inferred sense --
    it's a literal count of accounts with that one, real, row-level join.

    PERFORMANCE (Part 14): ONE bulk query fetches every (topic_id, company_id) bridge pair for
    the whole tenant up front; evaluate_topic_trend() is called once per CONFIGURED topic (a
    small, tenant-owned set -- 6 in real production today), never once per company. No N+1."""
    trend_config = get_trend_config(db, tenant_id)

    topics = (
        db.query(ContentTopic)
        .filter(ContentTopic.tenant_id == tenant_id)
        .order_by(ContentTopic.id)
        .limit(limit)
        .all()
    )
    if not topics:
        return {"topics": []}

    topic_ids = [t.id for t in topics]
    bridge_rows = (
        db.query(ContentTopicEvidence.content_topic_id, GtmSignal.company_id)
        .join(GtmSignal, ContentTopicEvidence.gtm_signal_id == GtmSignal.id)
        .filter(
            ContentTopicEvidence.tenant_id == tenant_id,
            GtmSignal.tenant_id == tenant_id,
            GtmSignal.company_id.isnot(None),
            ContentTopicEvidence.content_topic_id.in_(topic_ids),
        )
        .distinct()
        .all()
    )
    linked_companies_by_topic: dict[int, set[int]] = {}
    for topic_id, company_id in bridge_rows:
        linked_companies_by_topic.setdefault(topic_id, set()).add(company_id)

    items = []
    for topic in topics:
        trend = evaluate_topic_trend(db, tenant_id, topic, trend_config)
        linked_count = len(linked_companies_by_topic.get(topic.id, ()))
        items.append({
            "content_topic_id": topic.id,
            "canonical_name": topic.canonical_name,
            "aliases": topic.aliases,
            "origin": topic.origin,
            "state": trend["state"],
            "explanation": trend["explanation"],
            "recent_observation_count": trend["recent_observation_count"],
            "previous_observation_count": trend["previous_observation_count"],
            "recent_independent_entity_count": trend["recent_independent_entity_count"],
            "recent_source_diversity": trend["recent_source_diversity"],
            "observation_span_days": trend["observation_span_days"],
            "first_seen_at": trend["first_seen_at"],
            "last_seen_at": trend["last_seen_at"],
            "account_bridge": {
                "linked_account_count": linked_count,
                "note": None if linked_count > 0 else "Trending in market, but no verified account-level evidence yet.",
            },
        })

    return {"topics": items}
