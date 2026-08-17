"""Topic candidate normalization/consolidation -- Step 16E-5. Answers exactly one question:

    "Do these TopicCandidate observations refer to the same underlying concept?"

Does NOT decide whether a concept is real/important/trending/valuable -- that's the
not-yet-built promotion layer, deliberately out of scope here (see
topic-candidate-normalization-design.md).

Two-stage, cost-bounded pipeline, deterministic-first (the approved design's Option D):

    TopicCandidate (cluster_key IS NULL)
        |
        +-- Stage 1: deterministic normalization -- extends the existing mechanical
        |   normalized_name (lowercase/whitespace-collapse) with hyphen/slash normalization and a
        |   narrow trailing-"s" strip. Pure function of candidate_name -- always assigns
        |   cluster_key = deterministic_normalized(candidate_name), so identical results are
        |   naturally idempotent across runs with zero tracking needed.
        |
        +-- Stage 2: lexical pre-filter -- only clusters that DON'T already share a
        |   deterministic bucket, and DO share at least one significant word, become an LLM
        |   comparison pair. This is what keeps the LLM stage far below O(n^2) (see
        |   _plausible_pairs()).
        |
        +-- Stage 3: bounded LLM comparison (reuses app.llm_client.generate_json exactly as
            candidate_extraction.py does) -- "true" merges two clusters (the older cluster's key
            wins, see _merge_clusters()); "false"/"uncertain"/malformed-output/API-failure all
            keep clusters separate, per the approved false-merge-protection rules.

No new table. A "cluster" is purely rows sharing the same TopicCandidate.cluster_key -- see
topic-candidate-normalization-design.md Section 8 for why. Original candidate_name/
normalized_name/evidence_excerpt/extraction_method/confidence/gtm_signal_id are NEVER modified by
anything in this module."""

import logging
import re

from sqlalchemy.orm import Session

from app.gtm_os.content.topic import TopicCandidate
from app.llm_client import generate_json

logger = logging.getLogger(__name__)

# Small, deliberately narrow stopword set for the lexical pre-filter -- "ai" is included because
# this feature's domain is saturated with "AI ___" candidate names, so treating it as significant
# would make nearly every candidate "plausibly related" to every other, defeating the point of
# the pre-filter (narrowing, not widening, the LLM comparison set).
STOPWORDS = {"a", "an", "the", "of", "for", "and", "or", "to", "with", "in", "on", "is", "are", "this", "that", "ai"}

VALID_SAME_CONCEPT_VALUES = {True, False, "uncertain"}

CLUSTER_COMPARISON_PROMPT = """We are determining whether two independently observed topic \
candidates describe the SAME underlying concept -- we are NOT deciding whether either is \
valuable, trending, or a business opportunity, and we are NOT judging their importance.

Candidate A: "{name_a}"
Evidence excerpt A: "{excerpt_a}"

Candidate B: "{name_b}"
Evidence excerpt B: "{excerpt_b}"

Considering both the proposed names AND their evidence excerpts (the excerpts show the actual \
context each candidate came from -- two candidates can share similar-looking names but describe \
different things in context, or vice versa), do these two observations describe the same \
underlying concept?

Be conservative: if you are not clearly confident they are the same concept, answer "uncertain" \
rather than guessing. A missed match is a much smaller problem than a false one.

Return JSON exactly:
{{"same_concept": true | false | "uncertain", "reason": "<one sentence>"}}"""


def _deterministic_normalize(candidate_name: str) -> tuple[str, str]:
    """Extends the existing mechanical normalized_name (lowercase + whitespace collapse, Step
    16E-4) with exactly the two additional low-risk transforms approved in the Step 16E-5 design:
    hyphen/slash -> space, and a single trailing "s" stripped from the END OF THE WHOLE
    (already-normalized) STRING when the result is still >=4 characters. Deliberately NOT
    per-word plural stripping -- applying it to the whole string is enough to satisfy every
    approved example ("AI Sales Agent"/"AI Sales Agents", "AI SDR"/"AI SDRs") while avoiding a
    per-word stemmer's larger false-merge surface area, and it correctly leaves short trailing
    words like "SDR" (3 chars) alone -- "AI SDRs" -> "ai sdr" (6 chars, stripped) still works
    because the check applies to the RESULT of stripping the whole string, not each word.

    Returns (normalized, reason) -- reason is always non-empty and describes exactly which
    transforms actually changed something, for normalization_reason's provenance requirement."""
    text = candidate_name.strip().lower()
    text = re.sub(r"[-/]", " ", text)
    text = " ".join(text.split())

    reason_parts = ["lowercase + whitespace normalization"]
    if "-" in candidate_name or "/" in candidate_name:
        reason_parts.append("hyphen/slash normalized to space")

    if text.endswith("s") and len(text[:-1]) >= 4:
        text = text[:-1]
        reason_parts.append("trailing 's' removed (singular form >= 4 chars)")

    return text, "; ".join(reason_parts)


def _significant_words(candidate_name: str) -> set[str]:
    """Small deterministic stopword-filtered word set for the lexical pre-filter -- NOT the same
    as _deterministic_normalize (that's for exact-bucket clustering; this is for deciding whether
    two DIFFERENT buckets are even plausibly worth an LLM comparison)."""
    words = re.split(r"\W+", candidate_name.lower())
    return {w for w in words if w and w not in STOPWORDS and len(w) >= 2}


def _run_deterministic_stage(db: Session, tenant_id: int, limit: int) -> tuple[int, list[TopicCandidate]]:
    """Assigns cluster_key to every currently-unclustered candidate for this tenant, up to
    `limit`. Never touches a candidate that already has a cluster_key (from a prior deterministic
    or LLM-confirmed assignment) -- satisfies both idempotency and the "don't unnecessarily
    rewrite existing assignments" requirement, since this is the ONLY thing that decides whether a
    row is touched at all."""
    unclustered = (
        db.query(TopicCandidate)
        .filter(TopicCandidate.tenant_id == tenant_id, TopicCandidate.cluster_key.is_(None))
        .order_by(TopicCandidate.id)
        .limit(limit)
        .all()
    )
    for candidate in unclustered:
        dnorm, reason = _deterministic_normalize(candidate.candidate_name)
        candidate.cluster_key = dnorm
        candidate.normalization_method = "deterministic"
        candidate.normalization_reason = reason
    if unclustered:
        db.commit()
    return len(unclustered), unclustered


def _cluster_representatives_and_words(db: Session, tenant_id: int) -> tuple[dict[str, TopicCandidate], dict[str, set[str]]]:
    """One representative row per distinct cluster_key currently in use for this tenant -- the
    lowest-id row in each cluster, used both as the LLM comparison subject (its candidate_name/
    evidence_excerpt stand in for the whole cluster in the prompt) and as the merge target (see
    _merge_clusters -- the older cluster's key always wins, giving stable, order-independent
    merge results).

    Also returns each cluster's significant-word set as the UNION of every member row's own
    significant words -- not just the representative's. A cluster can grow via an LLM merge to
    include a member whose wording doesn't overlap with the representative's own name (e.g.
    representative "revenue operations tooling" merged with member "revops tooling stack" --
    a later candidate "revops platform selection" shares a word with the MEMBER, not the
    representative). Using only the representative's words would silently miss that a new
    candidate is plausibly related to an existing cluster, purely because of which row happened
    to be oldest -- an accident of arrival order, not a real signal. The prompt itself still only
    ever uses the representative's own name/excerpt (see _compare_clusters) -- this union is only
    used to decide WHETHER to compare at all."""
    rows = (
        db.query(TopicCandidate)
        .filter(TopicCandidate.tenant_id == tenant_id, TopicCandidate.cluster_key.isnot(None))
        .order_by(TopicCandidate.id)
        .all()
    )
    representatives: dict[str, TopicCandidate] = {}
    words: dict[str, set[str]] = {}
    for row in rows:
        if row.cluster_key not in representatives:
            representatives[row.cluster_key] = row
        words.setdefault(row.cluster_key, set()).update(_significant_words(row.candidate_name))
    return representatives, words


def _plausible_pairs(new_keys: set[str], cluster_words: dict[str, set[str]]) -> list[tuple[str, str]]:
    """Every (new_cluster_key, other_cluster_key) pair where the two clusters' significant-word
    sets (union across all current members, see _cluster_representatives_and_words) share at
    least one word -- new-vs-{everything}, never old-vs-old (old pairs were already either merged
    or ruled out in whichever earlier run first made one of them "new"; see module docstring and
    the design doc's cost-control section). This is what keeps this stage far below O(n^2): a
    brand-new tenant with N existing clusters and 1 new candidate costs at most N comparisons, not
    N^2, and most of those N are usually eliminated by the word-overlap check before ever reaching
    the LLM."""
    pairs = []
    seen = set()
    for new_key in new_keys:
        new_words = cluster_words.get(new_key, set())
        if not new_words:
            continue
        for other_key, other_words in cluster_words.items():
            if other_key == new_key:
                continue
            pair = tuple(sorted((new_key, other_key)))
            if pair in seen:
                continue
            if new_words & other_words:
                seen.add(pair)
                pairs.append(pair)
    return pairs


def _compare_clusters(db: Session, tenant_id: int, rep_a: TopicCandidate, rep_b: TopicCandidate) -> dict:
    """One bounded LLM comparison between two cluster representatives. Always returns a
    structured result -- never raises. The prompt includes BOTH representatives' real
    evidence_excerpt (not just their names), per the approved false-merge-protection rule."""
    prompt = CLUSTER_COMPARISON_PROMPT.format(
        name_a=rep_a.candidate_name,
        excerpt_a=rep_a.evidence_excerpt or "",
        name_b=rep_b.candidate_name,
        excerpt_b=rep_b.evidence_excerpt or "",
    )
    try:
        result = generate_json(prompt, db, tenant_id, max_tokens=200)
    except Exception as e:  # noqa: BLE001 -- an LLM outage must never crash the sweep, see module docstring
        logger.warning("candidate_normalization: LLM comparison failed for %r vs %r: %s", rep_a.candidate_name, rep_b.candidate_name, e)
        return {"outcome": "failed", "reason": f"llm_call_failed: {e}"}

    if not isinstance(result, dict) or "same_concept" not in result:
        return {"outcome": "failed", "reason": "malformed_output"}

    same_concept = result.get("same_concept")
    if same_concept not in VALID_SAME_CONCEPT_VALUES:
        return {"outcome": "failed", "reason": "malformed_output"}

    reason = result.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "no reason provided"

    if same_concept is True:
        return {"outcome": "merge", "reason": reason.strip()}
    if same_concept == "uncertain":
        return {"outcome": "uncertain", "reason": reason.strip()}
    return {"outcome": "kept_separate", "reason": reason.strip()}


def _merge_clusters(db: Session, tenant_id: int, rep_a: TopicCandidate, rep_b: TopicCandidate, reason: str) -> None:
    """Merges the cluster represented by the NEWER candidate (higher id) into the cluster
    represented by the OLDER one (lower id) -- the older cluster's key is the stable "first
    candidate observed" anchor per the approved design. Every row currently in the losing cluster
    is updated in place: cluster_key -> winning key, normalization_method -> "llm_comparison",
    normalization_reason -> the LLM's own reason (this row-level field can only hold one
    explanation, so it reflects the reason THIS row's cluster_key changed just now -- rows already
    in the winning cluster are untouched, their own deterministic/earlier-LLM provenance stands).
    candidate_name/normalized_name/evidence_excerpt/extraction_method/confidence/gtm_signal_id
    are never touched -- only cluster_key/normalization_method/normalization_reason change."""
    winner, loser = (rep_a, rep_b) if rep_a.id < rep_b.id else (rep_b, rep_a)
    losing_key = loser.cluster_key
    winning_key = winner.cluster_key

    losing_rows = (
        db.query(TopicCandidate)
        .filter(TopicCandidate.tenant_id == tenant_id, TopicCandidate.cluster_key == losing_key)
        .all()
    )
    for row in losing_rows:
        row.cluster_key = winning_key
        row.normalization_method = "llm_comparison"
        row.normalization_reason = reason
    db.commit()


def run_candidate_normalization_sweep(db: Session, tenant_id: int, limit: int = 200) -> dict:
    """Runs Stage 1 (deterministic bucketing) then Stage 2/3 (lexical pre-filter + bounded LLM
    comparison) for one tenant. `limit` bounds how many currently-unclustered TopicCandidate rows
    Stage 1 considers this call -- matches the established sweep convention elsewhere in
    app/gtm_os (run_content_topic_linking_sweep, run_candidate_extraction_sweep).

    Never compares across tenants (every query here is tenant_id-scoped) and never touches
    ContentTopic/ContentTopicEvidence -- this sweep only ever reads/writes TopicCandidate rows."""
    counts = {
        "candidates_considered": 0,
        "deterministic_assignments": 0,
        "llm_comparisons_attempted": 0,
        "llm_merges": 0,
        "kept_separate": 0,
        "uncertain": 0,
        "failures": 0,
    }

    before_representatives, _before_words = _cluster_representatives_and_words(db, tenant_id)
    before_keys = set(before_representatives.keys())

    assigned_count, assigned_rows = _run_deterministic_stage(db, tenant_id, limit)
    counts["candidates_considered"] = assigned_count
    counts["deterministic_assignments"] = assigned_count

    all_representatives, cluster_words = _cluster_representatives_and_words(db, tenant_id)
    new_keys = set(all_representatives.keys()) - before_keys
    if not new_keys:
        return counts

    pairs = _plausible_pairs(new_keys, cluster_words)

    for key_a, key_b in pairs:
        rep_a = all_representatives.get(key_a)
        rep_b = all_representatives.get(key_b)
        if rep_a is None or rep_b is None or rep_a.cluster_key == rep_b.cluster_key:
            # A prior merge in this same loop may have already unified these two keys --
            # re-check live cluster_key values rather than trusting the pre-computed snapshot.
            continue

        counts["llm_comparisons_attempted"] += 1
        outcome = _compare_clusters(db, tenant_id, rep_a, rep_b)

        if outcome["outcome"] == "merge":
            _merge_clusters(db, tenant_id, rep_a, rep_b, outcome["reason"])
            counts["llm_merges"] += 1
        elif outcome["outcome"] == "uncertain":
            counts["uncertain"] += 1
        elif outcome["outcome"] == "kept_separate":
            counts["kept_separate"] += 1
        else:
            counts["failures"] += 1

    return counts
