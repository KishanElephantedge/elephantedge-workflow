"""Content Opportunity (2026-08-28) -- the piece Content Intelligence was missing: turning a
real, evidence-backed ContentTopic into an actual content idea a human can review and write from.

Two-layer pattern, same discipline as campaign_intelligence.py/channel_intelligence.py:
1. Real, deterministic gating -- generate_content_opportunity() only ever runs for a topic whose
   REAL trend state (evaluate_topic_trend(), unmodified) is NOT insufficient_evidence/declining.
   No LLM call is ever made for a topic without real evidence behind it.
2. Grounded LLM synthesis -- the LLM is given ONLY the real evidence excerpts already linked to
   this topic (ContentTopicEvidence -> GtmSignal), and must cite which real URLs it used. A
   response citing a URL never actually given is discarded, never trusted blind -- same
   fabrication guard as every other "real numbers + grounded LLM reasoning" feature in this
   codebase.

ORIGIN TAGGING (the 60/40 mix, 2026-08-28 explicit instruction): an opportunity is tagged
"competitor" if any of its real evidence came from the competitor_content source, else "trend" --
a factual readout of which real leg produced the evidence, never an enforced quota. A human
reviewing the real mix of candidate opportunities is what actually keeps the balance, not a
selection algorithm faking precision on something inherently editorial.

REVIEW LIFECYCLE: mirrors message_draft.py's approve/reject/request_changes functions exactly
(same status vocabulary, same reviewed_at/reviewed_by/review_note fields, same "only from
candidate" precondition) -- no new review pattern invented.

DRAFT GENERATION ("write this specific topic", the deck's second copilot mode): a second, later,
explicit action -- generate_content_draft() -- only callable on an approved opportunity, reusing
the exact same real evidence + why_now/angle as its own grounding. Never auto-triggered by
approval; a human always decides when to actually ask for a draft. Never auto-published anywhere
-- publishing remains a manual, off-platform human action, same as every other write boundary in
this codebase (this table has no `published_at`/`published_by` field at all, deliberately)."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Session

from app.db.models import Base
from app.gtm_os.content.topic import ContentTopic, ContentTopicEvidence
from app.gtm_os.content.trend_config import get_trend_config
from app.gtm_os.content.trend_intelligence import evaluate_topic_trend
from app.gtm_os.intelligence.signal import GtmSignal
from app.llm_client import generate_json

VALID_STATUSES = {"candidate", "approved", "rejected", "changes_requested"}
# Topics without enough real, recent, independent evidence (insufficient_evidence) or ones
# actively losing relevance (declining) never get an opportunity generated -- see module
# docstring's Layer 1.
ELIGIBLE_TREND_STATES = {"emerging", "accelerating", "persistent", "stable"}
MAX_EVIDENCE_ITEMS = 8  # bounds prompt size/cost -- most recent real evidence wins, never invented


class ContentOpportunity(Base):
    """One real, evidence-backed content idea for one ContentTopic. Never auto-published (no
    published_at/published_by field exists) -- see module docstring."""
    __tablename__ = "content_opportunities"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    content_topic_id = Column(Integer, ForeignKey("content_topics.id"), nullable=False)
    origin = Column(String, nullable=False)  # "trend" | "competitor" -- see module docstring
    trend_state = Column(String, nullable=False)  # the real state (evaluate_topic_trend) at generation time, never re-derived later

    why_now = Column(Text, nullable=False)
    suggested_angle = Column(Text, nullable=False)
    cited_urls = Column(JSON, nullable=False)  # real evidence URLs the LLM actually cited -- verified, never invented

    status = Column(String, nullable=False, default="candidate")
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(String, nullable=True)
    review_note = Column(Text, nullable=True)

    draft_text = Column(Text, nullable=True)
    draft_generated_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)


def _gather_evidence(db: Session, tenant_id: int, content_topic_id: int) -> list[dict]:
    """Real evidence excerpts for one topic, most recently-added first, bounded to
    MAX_EVIDENCE_ITEMS. Only title/summary/url -- the same fields topic_linking.py's own
    extract_signal_text() already treats as this schema's text-content convention."""
    rows = (
        db.query(ContentTopicEvidence, GtmSignal)
        .join(GtmSignal, ContentTopicEvidence.gtm_signal_id == GtmSignal.id)
        .filter(ContentTopicEvidence.tenant_id == tenant_id, ContentTopicEvidence.content_topic_id == content_topic_id)
        .order_by(ContentTopicEvidence.added_at.desc())
        .limit(MAX_EVIDENCE_ITEMS)
        .all()
    )
    evidence = []
    for _evidence_row, signal in rows:
        info = signal.extracted_info or {}
        url = info.get("url")
        if not url:
            continue  # this evidence layer only cites real, checkable URLs -- a source with none can't be cited
        evidence.append({
            "url": url,
            "title": info.get("title"),
            "summary": info.get("summary"),
            "source": signal.source,
            "competitor_name": info.get("competitor_name"),
        })
    return evidence


CONTENT_OPPORTUNITY_PROMPT = """You are helping Elephant Edge (a fractional/outsourced sales \
leadership company -- see real positioning below) decide whether and why to write content about \
one real, trending topic.

Elephant Edge's real positioning: most competitors in this space RENT you sales capacity (they \
do the selling, capability leaves when the engagement ends); Elephant Edge builds a sales system \
the client's own team ends up owning and keeping.

Topic: {topic_name}
Real trend state: {trend_state} -- {trend_explanation}

Real evidence for this topic (nothing below is invented -- if a fact isn't here, it isn't known):
{evidence_block}

Based ONLY on the real evidence above, write:
1. why_now -- 2-3 sentences on why this topic is worth writing about right now, grounded in the \
specific real evidence above (cite what's actually happening, not a generic claim).
2. suggested_angle -- 1-2 sentences on the specific angle Elephant Edge should take, consistent \
with its real "build vs. rent" positioning above.

Return JSON exactly:
{{"why_now": "<2-3 sentences>", "suggested_angle": "<1-2 sentences>", "cited_urls": ["<url from \
the evidence above that you actually grounded your reasoning in>", ...]}}

Only cite a URL that appears in the evidence above. Never invent a URL, a statistic, or a claim \
not grounded in the real evidence given."""


def generate_content_opportunity(db: Session, tenant_id: int, content_topic_id: int) -> dict:
    """Layer 1 (real gating) + Layer 2 (grounded synthesis) -- see module docstring. Returns
    {"status": "insufficient_evidence" | "already_exists" | "llm_unavailable" | "discarded" | "ok",
    ...}. Never raises for a routine ineligibility reason."""
    topic = db.get(ContentTopic, content_topic_id)
    if topic is None or topic.tenant_id != tenant_id:
        return {"status": "not_found"}

    existing = (
        db.query(ContentOpportunity)
        .filter(ContentOpportunity.tenant_id == tenant_id, ContentOpportunity.content_topic_id == content_topic_id)
        .filter(ContentOpportunity.status != "rejected")
        .first()
    )
    if existing is not None:
        return {"status": "already_exists", "content_opportunity_id": existing.id}

    trend_config = get_trend_config(db, tenant_id)
    trend = evaluate_topic_trend(db, tenant_id, topic, trend_config)
    if trend["state"] not in ELIGIBLE_TREND_STATES:
        return {"status": "insufficient_evidence", "trend_state": trend["state"], "reason": trend["explanation"]}

    evidence = _gather_evidence(db, tenant_id, content_topic_id)
    if not evidence:
        return {"status": "insufficient_evidence", "trend_state": trend["state"], "reason": "no citable evidence (with a real URL) linked to this topic yet"}

    valid_urls = {e["url"] for e in evidence}
    evidence_block = "\n".join(
        f"- [{e['source']}{f', competitor: ' + e['competitor_name'] if e.get('competitor_name') else ''}] "
        f"{e['title'] or '(no title)'} -- {e['summary'] or '(no summary)'} ({e['url']})"
        for e in evidence
    )
    prompt = CONTENT_OPPORTUNITY_PROMPT.format(
        topic_name=topic.canonical_name, trend_state=trend["state"], trend_explanation=trend["explanation"], evidence_block=evidence_block,
    )

    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=500)
    except Exception as e:  # noqa: BLE001 -- an LLM outage must never crash the caller
        return {"status": "llm_unavailable", "error": str(e)}

    cited = response.get("cited_urls") if isinstance(response, dict) else None
    if not cited or not isinstance(cited, list) or not all(u in valid_urls for u in cited):
        return {"status": "discarded", "reason": "cited a URL never actually given as real evidence -- discarded, never trusted blind"}
    if not response.get("why_now") or not response.get("suggested_angle"):
        return {"status": "discarded", "reason": "missing why_now or suggested_angle"}

    origin = "competitor" if any(e["source"] == "competitor_content" for e in evidence) else "trend"

    opportunity = ContentOpportunity(
        tenant_id=tenant_id,
        content_topic_id=content_topic_id,
        origin=origin,
        trend_state=trend["state"],
        why_now=response["why_now"],
        suggested_angle=response["suggested_angle"],
        cited_urls=cited,
    )
    db.add(opportunity)
    db.commit()
    db.refresh(opportunity)
    return {"status": "ok", "content_opportunity_id": opportunity.id, "origin": origin}


def run_content_opportunity_generation_sweep(db: Session, tenant_id: int, limit: int = 50) -> dict:
    """Attempts generation for up to `limit` ContentTopics. One topic's failure never aborts the
    sweep -- same per-item error-isolation style as promotion.py/trend_intelligence.py's own
    sweeps (broad except, logged via the returned counts, never re-raised)."""
    topic_ids = [row[0] for row in db.query(ContentTopic.id).filter(ContentTopic.tenant_id == tenant_id).order_by(ContentTopic.id).limit(limit).all()]

    counts = {"evaluated": 0, "generated": 0, "insufficient_evidence": 0, "already_exists": 0, "discarded": 0, "llm_unavailable": 0, "failed": 0}
    for topic_id in topic_ids:
        counts["evaluated"] += 1
        try:
            result = generate_content_opportunity(db, tenant_id, topic_id)
            status = result["status"]
            if status == "ok":
                counts["generated"] += 1
            elif status in counts:
                counts[status] += 1
        except Exception:  # noqa: BLE001 -- one topic's failure must never block the rest of the sweep
            counts["failed"] += 1
            db.rollback()

    return counts


def _get_owned_opportunity(db: Session, tenant_id: int, content_opportunity_id: int) -> ContentOpportunity:
    row = db.get(ContentOpportunity, content_opportunity_id)
    if row is None or row.tenant_id != tenant_id:
        raise LookupError(f"ContentOpportunity {content_opportunity_id} not found")
    return row


def approve_content_opportunity(db: Session, tenant_id: int, content_opportunity_id: int, reviewed_by: str) -> ContentOpportunity:
    """Mirrors message_draft.py's approve_message_draft() exactly -- only from 'candidate'."""
    opportunity = _get_owned_opportunity(db, tenant_id, content_opportunity_id)
    if opportunity.status != "candidate":
        raise ValueError(f"cannot approve an opportunity with status {opportunity.status!r} -- must be 'candidate'")
    opportunity.status = "approved"
    opportunity.reviewed_at = datetime.utcnow()
    opportunity.reviewed_by = reviewed_by
    db.commit()
    return opportunity


def reject_content_opportunity(db: Session, tenant_id: int, content_opportunity_id: int, reviewed_by: str, note: str | None = None) -> ContentOpportunity:
    opportunity = _get_owned_opportunity(db, tenant_id, content_opportunity_id)
    if opportunity.status != "candidate":
        raise ValueError(f"cannot reject an opportunity with status {opportunity.status!r} -- must be 'candidate'")
    opportunity.status = "rejected"
    opportunity.reviewed_at = datetime.utcnow()
    opportunity.reviewed_by = reviewed_by
    opportunity.review_note = note
    db.commit()
    return opportunity


def request_content_opportunity_changes(db: Session, tenant_id: int, content_opportunity_id: int, reviewed_by: str, note: str) -> ContentOpportunity:
    opportunity = _get_owned_opportunity(db, tenant_id, content_opportunity_id)
    if opportunity.status != "candidate":
        raise ValueError(f"cannot request changes on an opportunity with status {opportunity.status!r} -- must be 'candidate'")
    opportunity.status = "changes_requested"
    opportunity.reviewed_at = datetime.utcnow()
    opportunity.reviewed_by = reviewed_by
    opportunity.review_note = note
    db.commit()
    return opportunity


DRAFT_PROMPT = """Write a real, publishable LinkedIn/blog post for Elephant Edge on the topic \
below, grounded ONLY in the real evidence and angle already established -- never invent a \
statistic, quote, or claim not present below.

Topic: {topic_name}
Why now: {why_now}
Angle: {suggested_angle}

Real evidence this is grounded in:
{evidence_block}

Write a genuine draft (250-400 words), in Elephant Edge's real voice: direct, no fluff, grounded \
in real evidence, consistent with their "build vs. rent" positioning. Return JSON exactly:
{{"draft_text": "<the full draft>"}}"""


def generate_content_draft(db: Session, tenant_id: int, content_opportunity_id: int) -> dict:
    """The deck's "write this specific topic" mode -- only callable on an approved opportunity.
    Never auto-triggered by approve_content_opportunity(); a human always explicitly asks for
    this. Regenerating overwrites the previous draft_text (same "latest wins, no version history"
    simplicity as this codebase's other single-draft fields where no separate review lifecycle
    exists for the draft text itself)."""
    opportunity = _get_owned_opportunity(db, tenant_id, content_opportunity_id)
    if opportunity.status != "approved":
        return {"status": "not_approved", "reason": f"opportunity status is {opportunity.status!r}, must be 'approved'"}

    topic = db.get(ContentTopic, opportunity.content_topic_id)
    evidence = _gather_evidence(db, tenant_id, opportunity.content_topic_id)
    evidence_block = "\n".join(f"- {e['title'] or '(no title)'} -- {e['summary'] or '(no summary)'} ({e['url']})" for e in evidence)
    prompt = DRAFT_PROMPT.format(topic_name=topic.canonical_name, why_now=opportunity.why_now, suggested_angle=opportunity.suggested_angle, evidence_block=evidence_block)

    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=900)
    except Exception as e:  # noqa: BLE001
        return {"status": "llm_unavailable", "error": str(e)}

    draft_text = response.get("draft_text") if isinstance(response, dict) else None
    if not draft_text:
        return {"status": "discarded", "reason": "no draft_text returned"}

    opportunity.draft_text = draft_text
    opportunity.draft_generated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok", "draft_text": draft_text}
