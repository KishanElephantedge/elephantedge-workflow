"""Candidate-worthy filtering -- Step 16E-3. The cheap deterministic gate that decides whether an
UNMATCHED GtmSignal is even eligible for a future LLM classification pass -- NOT the classifier
itself (no LLM call, no TopicCandidate write, no discovered-topic logic lives here; see
topic-extraction-normalization-design.md for where those belong).

    Unmatched signal -> is_candidate_worthy() -> {candidate_worthy, reason}
                                                        |
                                        (only if True) LLM classification (not built)
                                                        |
                                                  TopicCandidate (not built)

ALREADY-EVALUATED CHECK -- the design boundary called out explicitly in this step's spec: the
TopicCandidate TABLE already exists (Step 16E-1 built its persistence), even though nothing
writes to it yet (that's Step 16E-3's own explicit non-goal, and no later step has been approved
either). So "has this signal already been evaluated for topic candidacy" is answered by a
READ-ONLY query against the real, already-approved topic_candidates table -- not a new marker,
not a second evaluation table, not an in-memory/ad-hoc mechanism. Today this query always comes
back empty (nothing writes TopicCandidate yet), so this check is a no-op in practice until the
LLM step exists -- but it's wired to real, durable state now, not invented state, and needs zero
changes when that step ships."""

from sqlalchemy.orm import Session

from app.gtm_os.content.topic import ContentTopicEvidence, TopicCandidate
from app.gtm_os.content.topic_linking import extract_signal_text
from app.gtm_os.intelligence.signal import GtmSignal

# Calibration values, not derived from any real data yet -- smallest reasonable initial
# thresholds, to be revisited once real unmatched-signal text is actually observed flowing
# through this filter. Deliberately conservative (low bar) rather than guessed-tight: this gate's
# only job is to reject the OBVIOUS non-candidates (empty/trivial/boilerplate), not to judge
# whether content is interesting (explicitly not this layer's job, per the spec).
MIN_CANDIDATE_TEXT_LENGTH = 40  # characters -- shorter than a typical single sentence
MIN_CANDIDATE_WORD_COUNT = 5  # words -- a handful of words below this is a fragment, not content

# Narrow, literal markers for OBVIOUS navigation/system/feed artifacts -- not a content-quality
# classifier. Substring match, case-insensitive. Kept intentionally short; expand only when a
# real observed false-positive justifies it, not preemptively.
BOILERPLATE_MARKERS = (
    "enable javascript",
    "javascript is disabled",
    "page not found",
    "404 not found",
    "access denied",
    "accept cookies",
    "cookie policy",
    "subscribe to our newsletter",
    "click here to continue",
    "this page could not be found",
)


def _is_boilerplate(lowered_text: str) -> bool:
    return any(marker in lowered_text for marker in BOILERPLATE_MARKERS)


def is_candidate_worthy(db: Session, signal: GtmSignal) -> dict:
    """Evaluates one GtmSignal. Intended to be called on signals that already failed configured
    topic matching (Step 16E-2's sweep) -- but also checks that itself (via ContentTopicEvidence)
    as a cheap, real-state-backed safety net against being called on an already-matched signal by
    mistake, rather than trusting every caller to have filtered correctly upstream.

    Returns a structured, explainable result -- never a bare bool -- matching this codebase's
    established preference for explainable decisions (classifier_reason, confidence breakdowns,
    etc. elsewhere in gtm_os)."""
    already_matched = (
        db.query(ContentTopicEvidence.id)
        .filter(ContentTopicEvidence.gtm_signal_id == signal.id)
        .first()
    )
    if already_matched is not None:
        return {"candidate_worthy": False, "reason": "already_matched_configured_topic"}

    already_evaluated = (
        db.query(TopicCandidate.id)
        .filter(TopicCandidate.gtm_signal_id == signal.id)
        .first()
    )
    if already_evaluated is not None:
        return {"candidate_worthy": False, "reason": "already_evaluated"}

    text = extract_signal_text(signal).strip()
    if not text:
        return {"candidate_worthy": False, "reason": "insufficient_text"}

    if len(text) < MIN_CANDIDATE_TEXT_LENGTH or len(text.split()) < MIN_CANDIDATE_WORD_COUNT:
        return {"candidate_worthy": False, "reason": "insufficient_text"}

    if _is_boilerplate(text.lower()):
        return {"candidate_worthy": False, "reason": "boilerplate_or_system_content"}

    return {"candidate_worthy": True, "reason": None}
