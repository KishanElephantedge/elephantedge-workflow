"""LLM-assisted topic candidate extraction -- Step 16E-4. The only step that ever calls an LLM in
this feature, and the only step that ever writes a TopicCandidate row.

    GtmSignal -> is_candidate_worthy() [reused, unmodified] -> LLM -> TopicCandidate

CORE RULE, restated in code (not just docs): this module NEVER creates or modifies ContentTopic
or ContentTopicEvidence, never touches ProblemHypothesis/DemandHypothesis, never determines
trends or Content Opportunity. A TopicCandidate is only ever a proposal -- normalization/
promotion is later, unbuilt work (see topic-extraction-normalization-design.md).

LLM infrastructure reused, not reinvented: app.llm_client.generate_json() (Gemini-first,
Claude-Haiku-fallback, both hand-rolled REST clients already in this repo -- no new SDK, no new
provider). Prompt/validation shape directly follows app/phases/linkedin_monitor.py's
classify_relevance() -- the one existing precedent in this codebase for "single-shot LLM
classification, plain-JSON-in-prompt, hardened caller" -- except failures here are NOT collapsed
to one safe default (classify_relevance's own choice, appropriate for its own use case): a
TopicCandidate is optional, speculative data, so every failure/rejection reason is preserved and
reported instead, and nothing is silently created."""

import logging

from sqlalchemy.orm import Session

from app.gtm_os.content.candidate_filter import is_candidate_worthy
from app.gtm_os.content.topic import TopicCandidate
from app.gtm_os.content.topic_linking import extract_signal_text
from app.gtm_os.intelligence.signal import GtmSignal
from app.llm_client import generate_json

logger = logging.getLogger(__name__)

EXTRACTION_METHOD = "llm_topic_extraction"

# Same 2000-char truncation precedent as classify_relevance() (linkedin_monitor.py) -- bounds
# prompt cost/length without inventing a new convention. The excerpt-verification check below
# runs against this SAME truncated text, since that's genuinely all the model was given.
MAX_INPUT_TEXT_CHARS = 2000

VALID_CONFIDENCE_VALUES = {"high", "medium", "low"}

CANDIDATE_EXTRACTION_PROMPT = """We are identifying the single main topic or concept discussed \
in the following piece of evidence -- we are NOT deciding whether it is valuable, trending, or a \
business opportunity. This evidence did not match any of our currently configured topics.

Source type: {source_type}

Evidence text:
\"\"\"{text}\"\"\"

Propose the single main topic/concept this evidence is genuinely about, if one is clearly \
identifiable. The topic name must be:
- concise (a short phrase, not a sentence)
- concept-oriented (a subject/theme, not a summary of the text)
- NOT a generic term like "business", "technology", or "news"
- NOT a company name, unless the company itself is genuinely the topic
- NOT a person's name, unless the person themselves is genuinely the topic

If no clear, meaningful topic can be identified, return null for topic_name -- do not force one.

If you propose a topic_name, you must also provide evidence_excerpt: a short phrase copied \
EXACTLY (verbatim, word-for-word) from the evidence text above that justifies the proposed \
topic. Never paraphrase or invent this excerpt -- it must be a real substring of the evidence \
text.

confidence reflects ONLY how confident you are that this is genuinely the MAIN topic of this \
evidence -- it does NOT reflect how valuable, trending, or important the topic itself is.

Return JSON exactly:
{{"topic_name": "<concise topic name, or null>", "evidence_excerpt": "<exact phrase from the \
evidence text, or null>", "confidence": "high" | "medium" | "low", "reason": "<one sentence>"}}"""


def _normalize_candidate_name(name: str) -> str:
    """Deterministic lowercase/whitespace-collapse grouping key -- NOT semantic, matching
    TopicCandidate.normalized_name's own documented purpose (Step 16E-1)."""
    return " ".join(name.strip().split()).lower()


def extract_topic_candidate(db: Session, tenant_id: int, signal: GtmSignal) -> dict:
    """Runs the full candidate-worthy -> LLM -> TopicCandidate pipeline for ONE signal. Always
    returns a structured result, never raises for an LLM-side failure (matching this codebase's
    established discipline -- see problem/demand hypothesis's own confidence dicts, and
    classify_relevance's own try/except-and-return-structured-result shape).

    Returns one of:
      {"status": "skipped", "reason": <candidate_filter reason>}                -- LLM never called
      {"status": "failed", "reason": <str>}                                     -- LLM called, unusable
      {"status": "rejected", "reason": <str>}                                   -- LLM called, validly declined/discarded
      {"status": "created", "topic_candidate_id": <int>, "candidate_name": <str>}
    """
    worthy = is_candidate_worthy(db, signal)
    if not worthy["candidate_worthy"]:
        return {"status": "skipped", "reason": worthy["reason"], "llm_attempted": False}

    text = extract_signal_text(signal).strip()[:MAX_INPUT_TEXT_CHARS]

    prompt = CANDIDATE_EXTRACTION_PROMPT.format(source_type=signal.source, text=text)

    try:
        result = generate_json(prompt, db, tenant_id, max_tokens=300)
    except Exception as e:  # noqa: BLE001 -- an LLM outage must never crash the sweep, see module docstring
        logger.warning("candidate_extraction: LLM call failed for signal %s: %s", signal.id, e)
        return {"status": "failed", "reason": f"llm_call_failed: {e}", "llm_attempted": True}

    if not isinstance(result, dict) or "topic_name" not in result:
        return {"status": "failed", "reason": "malformed_output", "llm_attempted": True}

    topic_name = result.get("topic_name")
    if topic_name is None:
        return {"status": "rejected", "reason": "no_topic_extracted", "llm_attempted": True}
    if not isinstance(topic_name, str) or not topic_name.strip():
        return {"status": "failed", "reason": "malformed_output", "llm_attempted": True}

    confidence = result.get("confidence")
    if confidence not in VALID_CONFIDENCE_VALUES:
        return {"status": "failed", "reason": "invalid_confidence", "llm_attempted": True}

    evidence_excerpt = result.get("evidence_excerpt")
    if not isinstance(evidence_excerpt, str) or not evidence_excerpt.strip():
        return {"status": "failed", "reason": "malformed_output", "llm_attempted": True}

    if evidence_excerpt.strip().lower() not in text.lower():
        return {"status": "rejected", "reason": "evidence_excerpt_not_verified", "llm_attempted": True}

    # A low-confidence proposal is discarded, not persisted -- same conservative default already
    # established by the candidate-worthy filter itself (Step 16E-3): a speculative row this
    # uncertain isn't worth keeping even as a raw, unpromoted candidate.
    if confidence == "low":
        return {"status": "rejected", "reason": "low_confidence_discarded", "llm_attempted": True}

    candidate = TopicCandidate(
        tenant_id=tenant_id,
        candidate_name=topic_name.strip(),
        normalized_name=_normalize_candidate_name(topic_name),
        gtm_signal_id=signal.id,
        evidence_excerpt=evidence_excerpt.strip(),
        extraction_method=EXTRACTION_METHOD,
        confidence=confidence,
    )
    db.add(candidate)
    db.commit()

    return {
        "status": "created",
        "topic_candidate_id": candidate.id,
        "candidate_name": candidate.candidate_name,
        "llm_attempted": True,
    }


def run_candidate_extraction_sweep(
    db: Session,
    tenant_id: int,
    sources: list[str] | None = None,
    limit: int = 200,
) -> dict:
    """Iterates GtmSignal rows for one tenant (optionally narrowed by `sources`) and runs
    extract_topic_candidate() on each. Source-agnostic, same shape as
    run_content_topic_linking_sweep()/candidate_filter -- no per-source branching.

    Only reports counts (no persistent observability table, per this step's explicit scope)."""
    counts = {
        "signals_considered": 0,
        "candidate_worthy": 0,
        "llm_attempted": 0,
        "candidates_created": 0,
        "rejected": 0,
        "failures": 0,
    }

    query = db.query(GtmSignal).filter(GtmSignal.tenant_id == tenant_id)
    if sources is not None:
        query = query.filter(GtmSignal.source.in_(sources))
    query = query.order_by(GtmSignal.id).limit(limit)

    for signal in query:
        counts["signals_considered"] += 1
        outcome = extract_topic_candidate(db, tenant_id, signal)

        if outcome["status"] == "skipped":
            continue
        counts["candidate_worthy"] += 1
        if outcome.get("llm_attempted"):
            counts["llm_attempted"] += 1
        if outcome["status"] == "created":
            counts["candidates_created"] += 1
        elif outcome["status"] == "rejected":
            counts["rejected"] += 1
        elif outcome["status"] == "failed":
            counts["failures"] += 1

    return counts
