"""MessageDraft -- Batch 7 Parts F-K. Controlled, LLM-ASSISTED message generation with an
explicit human-approval lifecycle. NEVER sends, NEVER auto-approves, NEVER executes anything.

EXISTING PersonalizedMessage ASSESSED, NOT REUSED (Part G) -- app/phases/personalized_outreach.py
(Phase 13) was inspected directly:
  1. Inputs: a Score-pipeline Contact/Company (batch-discovered, ICP-scored) -- NOT an
     Opportunity/GtmStrategy at all.
  2. Paid enrichment: YES -- run_contact_research() calls execute_tool("aviato_get_person_posts",
     ...), a real paid API call, on every invocation.
  3. LLM calls: YES -- FOUR separate generate_json/generate_text calls per message
     (run_company_research, run_contact_research, run_fit_analysis, run_message_synthesis).
  4. Persistent writes: yes, to PersonalizedMessage (Score pipeline's own table).
  5/6. NOT safe to reuse here: reusing it would (a) silently trigger real paid Aviato calls and
     four LLM calls per Opportunity the moment this batch's flow touches it, and (b) couple the
     new GTM-OS Opportunity/Strategy architecture back to the older Score/batch pipeline's own
     data shape and cost model -- exactly what Part G says to avoid. This module is the "thin new
     abstraction" Part G's own fallback instructs building instead: ONE bounded generate_json()
     call, using ONLY data this batch's own pipeline (Batch 5/6) already assembled for free.

LLM BOUNDARY (Part H): reuses app.llm_client.generate_json() unmodified -- no new provider/SDK.
The prompt receives only prepare_message()'s own structured output (Batch 6) -- no raw signals,
no credentials, no cross-tenant data. The prompt explicitly prohibits invented company facts,
contact facts, pain points, budgets, timelines, initiatives, metrics, customer stories, or product
capabilities, and explicitly allows the model to return insufficient context rather than guess.

MESSAGE LIFECYCLE (Part I): insufficient_context -> draft -> ready_for_review -> approved.
"sent" is deliberately NOT a state in this batch. ready_for_review means the deterministic
quality gate (Part J) passed -- NOT that a human has looked at it. approved is set ONLY by
approve_message_draft(), NEVER inferred from generation success, readiness, or any prior state
(Part K)."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Session

from app.db.models import Base
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.sales.sales_agent import evaluate_decision_maker, gather_account_research, prepare_message
from app.gtm_os.strategy.strategy import GtmStrategy
from app.llm_client import generate_json

MESSAGE_STATUSES = {"insufficient_context", "draft", "ready_for_review", "approved", "rejected", "changes_requested"}

# Phase 7 -- the three terminal outcomes of human review. "approved"/"rejected"/"changes_requested"
# all require the SAME precondition (status == "ready_for_review", i.e. the deterministic quality
# gate already passed) and all write the SAME generalized reviewed_at/reviewed_by/review_note
# fields -- see approve_message_draft()/reject_message_draft()/request_changes_message_draft()
# below, which share that precondition/field-writing shape rather than each reinventing it.
REVIEW_OUTCOMES = {"approved", "rejected", "changes_requested"}

MESSAGE_GENERATION_PROMPT = """You are drafting a short, professional outbound LinkedIn/email \
message on behalf of a B2B GTM team, using ONLY the structured information provided below.

Objective: {objective}
Channel: {channel}
Target role: {target_role}
Positioning angle: {positioning_angle}
Known, real facts you may reference (nothing else): {personalization_inputs}

STRICT RULES -- you must not violate any of these:
- Do NOT invent or assume any company fact, contact fact, pain point, budget, timeline, \
initiative, metric, customer story, or product capability that is not explicitly listed above.
- Do NOT reference specific numbers, dates, or claims unless they appear above.
- If the information above is not enough to write a genuinely grounded, specific message, return \
null for message_text rather than writing something generic or invented.
- Keep the message short (3-5 sentences), professional, and directly grounded in the facts above.

Return JSON exactly:
{{"message_text": "<the drafted message, or null>", "reason": "<one sentence explaining your decision>"}}"""


class MessageDraft(Base):
    __tablename__ = "message_drafts"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False)
    gtm_strategy_id = Column(Integer, ForeignKey("gtm_strategies.id"), nullable=False)  # pins to one immutable Strategy version -- see unique index in ensure_indexes()
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)

    channel = Column(String, nullable=True)
    objective = Column(Text, nullable=True)
    target_role = Column(String, nullable=True)
    positioning_angle = Column(Text, nullable=True)
    evidence_basis = Column(JSON, nullable=True)
    personalization_inputs = Column(JSON, nullable=True)

    message_text = Column(Text, nullable=True)  # null whenever status is insufficient_context
    generation_method = Column(String, nullable=False)  # "llm:generate_json" | "deterministic:insufficient_context"
    missing_information = Column(JSON, nullable=True)

    status = Column(String, nullable=False, default="insufficient_context")
    quality_gate_reasons = Column(JSON, nullable=True)  # why NOT ready_for_review, when applicable

    approved_at = Column(DateTime, nullable=True)  # set ONLY by approve_message_draft() -- never inferred, never renamed (Phase 3's V2 Messages tab already reads this)
    approved_by = Column(String, nullable=True)

    # Phase 7 -- generalized fields covering all three terminal review outcomes (approved,
    # rejected, changes_requested), not just approval. reviewed_by is supplied by the caller from
    # the real gateway-authenticated session identity (see api.py's review route docstring for
    # why this backend has no independent user-identity channel of its own) -- never invented.
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(String, nullable=True)
    review_note = Column(Text, nullable=True)  # optional human explanation, only used for rejected/changes_requested

    created_at = Column(DateTime, default=datetime.utcnow)
    last_updated_at = Column(DateTime, default=datetime.utcnow)


def _existing_draft(db: Session, tenant_id: int, opportunity_id: int, gtm_strategy_id: int) -> MessageDraft | None:
    return (
        db.query(MessageDraft)
        .filter(MessageDraft.tenant_id == tenant_id, MessageDraft.opportunity_id == opportunity_id, MessageDraft.gtm_strategy_id == gtm_strategy_id)
        .first()
    )


PLACEHOLDER_MARKERS = ("[insert", "[company name]", "[your name]", "[placeholder]", "{{", "}}")


def _run_quality_gate(opportunity: Opportunity, strategy: GtmStrategy, contact_id: int | None, prep: dict, message_text: str) -> list[str]:
    """Deterministic safety checks (Part J) -- no scoring, just structured reasons."""
    reasons = []
    if opportunity.company_id is None:
        reasons.append("no company identity on the Opportunity")
    if contact_id is None:
        reasons.append("no known contact -- personalized outreach requires a real contact identity")
    if not strategy.strategy_type or strategy.strategy_type == "insufficient_context":
        reasons.append("strategy does not exist or is insufficient_context")
    if not prep.get("objective"):
        reasons.append("no objective available")
    if not prep.get("evidence_basis"):
        reasons.append("no evidence basis available")
    if strategy.offering_fit_status != "candidate_match":
        reasons.append(f"offering_fit_status is {strategy.offering_fit_status!r} -- offering context is not confirmed, message must not imply a specific offering")
    if prep.get("missing_information"):
        reasons.append(f"unresolved missing_information from message preparation: {prep['missing_information']}")
    lowered = message_text.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        reasons.append("generated message contains an unfilled placeholder marker")
    return reasons


def generate_message_draft(db: Session, tenant_id: int, opportunity: Opportunity, strategy: GtmStrategy) -> MessageDraft:
    """The only function that calls the LLM. Idempotent: an existing draft for this exact
    (opportunity, strategy version) is reused/skipped -- never regenerated, and NEVER overwritten
    once approved (Part N/K)."""
    existing = _existing_draft(db, tenant_id, opportunity.id, strategy.id)
    if existing is not None:
        return existing

    research = gather_account_research(db, tenant_id, opportunity)
    decision_maker = evaluate_decision_maker(db, tenant_id, opportunity.company_id)
    prep = prepare_message(db, tenant_id, opportunity, strategy, research, decision_maker)

    contact_id = decision_maker["contacts"][0]["id"] if decision_maker["status"] == "known" and decision_maker["contacts"] else None

    if prep["status"] != "ready":
        draft = MessageDraft(
            tenant_id=tenant_id, opportunity_id=opportunity.id, gtm_strategy_id=strategy.id, contact_id=contact_id,
            channel=prep["channel"], objective=prep["objective"], target_role=prep["target_role"],
            positioning_angle=prep["positioning_angle"], evidence_basis=prep["evidence_basis"],
            personalization_inputs=prep["personalization_inputs"], message_text=None,
            generation_method="deterministic:insufficient_context", missing_information=prep["missing_information"],
            status="insufficient_context", quality_gate_reasons=None,
        )
        db.add(draft)
        db.commit()
        return draft

    prompt = MESSAGE_GENERATION_PROMPT.format(
        objective=prep["objective"], channel=prep["channel"], target_role=prep["target_role"] or "unknown",
        positioning_angle=prep["positioning_angle"] or "none provided",
        personalization_inputs="; ".join(prep["personalization_inputs"]) or "none",
    )

    try:
        result = generate_json(prompt, db, tenant_id, max_tokens=300)
    except Exception as e:  # noqa: BLE001 -- an LLM outage must never crash the caller
        draft = MessageDraft(
            tenant_id=tenant_id, opportunity_id=opportunity.id, gtm_strategy_id=strategy.id, contact_id=contact_id,
            channel=prep["channel"], objective=prep["objective"], target_role=prep["target_role"],
            positioning_angle=prep["positioning_angle"], evidence_basis=prep["evidence_basis"],
            personalization_inputs=prep["personalization_inputs"], message_text=None,
            generation_method="llm:generate_json", missing_information=[f"llm_call_failed: {e}"],
            status="insufficient_context", quality_gate_reasons=None,
        )
        db.add(draft)
        db.commit()
        return draft

    message_text = result.get("message_text") if isinstance(result, dict) else None

    if not message_text or not isinstance(message_text, str):
        draft = MessageDraft(
            tenant_id=tenant_id, opportunity_id=opportunity.id, gtm_strategy_id=strategy.id, contact_id=contact_id,
            channel=prep["channel"], objective=prep["objective"], target_role=prep["target_role"],
            positioning_angle=prep["positioning_angle"], evidence_basis=prep["evidence_basis"],
            personalization_inputs=prep["personalization_inputs"], message_text=None,
            generation_method="llm:generate_json",
            missing_information=prep["missing_information"] + [f"LLM declined to generate: {result.get('reason', 'no reason given') if isinstance(result, dict) else 'malformed output'}"],
            status="insufficient_context", quality_gate_reasons=None,
        )
        db.add(draft)
        db.commit()
        return draft

    quality_gate_reasons = _run_quality_gate(opportunity, strategy, contact_id, prep, message_text)
    status = "draft" if quality_gate_reasons else "ready_for_review"

    draft = MessageDraft(
        tenant_id=tenant_id, opportunity_id=opportunity.id, gtm_strategy_id=strategy.id, contact_id=contact_id,
        channel=prep["channel"], objective=prep["objective"], target_role=prep["target_role"],
        positioning_angle=prep["positioning_angle"], evidence_basis=prep["evidence_basis"],
        personalization_inputs=prep["personalization_inputs"], message_text=message_text,
        generation_method="llm:generate_json", missing_information=prep["missing_information"],
        status=status, quality_gate_reasons=quality_gate_reasons or None,
    )
    db.add(draft)
    db.commit()
    return draft


def approve_message_draft(db: Session, tenant_id: int, message_draft_id: int, approved_by: str) -> MessageDraft:
    """The ONLY function that ever sets status='approved'. Requires the draft to already be
    ready_for_review -- cannot approve a draft/insufficient_context message (forces the
    deterministic quality gate to have passed first, Part J/K). Does not send, execute, or write
    to any external system -- purely a state transition on this row.

    Phase 7: also sets the generalized reviewed_at/reviewed_by (see MessageDraft's own column
    comments) so "approved" reads as one of three review outcomes, not a special case -- the
    original approved_at/approved_by are still set too, unchanged, since Phase 3's V2 Messages
    tab already reads those two verbatim."""
    draft = db.get(MessageDraft, message_draft_id)
    if draft is None or draft.tenant_id != tenant_id:
        raise ValueError(f"no MessageDraft {message_draft_id} for tenant {tenant_id}")
    if draft.status != "ready_for_review":
        raise ValueError(f"cannot approve a draft with status {draft.status!r} -- must be 'ready_for_review'")

    now = datetime.utcnow()
    draft.status = "approved"
    draft.approved_at = now
    draft.approved_by = approved_by
    draft.reviewed_at = now
    draft.reviewed_by = approved_by
    draft.last_updated_at = now
    db.commit()
    return draft


def reject_message_draft(db: Session, tenant_id: int, message_draft_id: int, reviewed_by: str, note: str | None = None) -> MessageDraft:
    """Phase 7. Mirrors approve_message_draft()'s exact precondition (must be ready_for_review)
    and safety guarantee (pure state transition, no external write, no execution) -- the only
    difference is the resulting status and that approved_at/approved_by are never set, since this
    draft was never approved."""
    draft = db.get(MessageDraft, message_draft_id)
    if draft is None or draft.tenant_id != tenant_id:
        raise ValueError(f"no MessageDraft {message_draft_id} for tenant {tenant_id}")
    if draft.status != "ready_for_review":
        raise ValueError(f"cannot reject a draft with status {draft.status!r} -- must be 'ready_for_review'")

    now = datetime.utcnow()
    draft.status = "rejected"
    draft.reviewed_at = now
    draft.reviewed_by = reviewed_by
    draft.review_note = note
    draft.last_updated_at = now
    db.commit()
    return draft


def request_changes_message_draft(db: Session, tenant_id: int, message_draft_id: int, reviewed_by: str, note: str | None = None) -> MessageDraft:
    """Phase 7. Same shape as reject_message_draft() -- a human decided this specific draft
    version is not ready, without claiming it was approved or generating a replacement. Producing
    a new draft after changes_requested is a separate, explicit, later action (generate_message_draft()
    itself, unmodified) -- never triggered automatically by this function."""
    draft = db.get(MessageDraft, message_draft_id)
    if draft is None or draft.tenant_id != tenant_id:
        raise ValueError(f"no MessageDraft {message_draft_id} for tenant {tenant_id}")
    if draft.status != "ready_for_review":
        raise ValueError(f"cannot request changes on a draft with status {draft.status!r} -- must be 'ready_for_review'")

    now = datetime.utcnow()
    draft.status = "changes_requested"
    draft.reviewed_at = now
    draft.reviewed_by = reviewed_by
    draft.review_note = note
    draft.last_updated_at = now
    db.commit()
    return draft


def list_messages_for_company(db: Session, tenant_id: int, company_id: int) -> list[dict]:
    """Phase 3 (V2 Account 360 Messages API) -- read-only, tenant-scoped list of every
    MessageDraft belonging to one company's Opportunities. Pure query, no writes, no LLM call
    (never calls generate_message_draft()), no approval (never calls approve_message_draft()) --
    this function's entire job is exposing the existing MessageDraft rows/lifecycle, not acting
    on them. A single bounded join (MessageDraft -> Opportunity, filtered by company_id and
    tenant_id on both sides), not a per-opportunity loop.

    Phase 7 addition: `is_current_strategy_version` -- whether this draft's gtm_strategy_id is
    still the LATEST GtmStrategy for its opportunity. This is the stale-approval protection Part
    12 asks for, made visible rather than invented: MessageDraft has always been pinned to one
    immutable (opportunity_id, gtm_strategy_id) pair (Batch 7's own unique index), so an approval
    tied to a superseded strategy version was already structurally impossible to confuse with a
    current one -- this field just surfaces that existing fact instead of leaving it implicit.
    Computed via one bulk query (latest GtmStrategy id per opportunity_id in this result set),
    never one query per draft."""
    rows = (
        db.query(MessageDraft)
        .join(Opportunity, MessageDraft.opportunity_id == Opportunity.id)
        .filter(
            MessageDraft.tenant_id == tenant_id,
            Opportunity.tenant_id == tenant_id,
            Opportunity.company_id == company_id,
        )
        .order_by(MessageDraft.id.desc())
        .all()
    )

    opportunity_ids = list({d.opportunity_id for d in rows})
    latest_strategy_id_by_opportunity: dict[int, int] = {}
    if opportunity_ids:
        latest_rows = (
            db.query(GtmStrategy.opportunity_id, GtmStrategy.id)
            .filter(GtmStrategy.tenant_id == tenant_id, GtmStrategy.opportunity_id.in_(opportunity_ids))
            .distinct(GtmStrategy.opportunity_id)
            .order_by(GtmStrategy.opportunity_id, GtmStrategy.id.desc())
            .all()
        )
        latest_strategy_id_by_opportunity = dict(latest_rows)

    return [
        {
            "id": d.id,
            "opportunity_id": d.opportunity_id,
            "gtm_strategy_id": d.gtm_strategy_id,
            "contact_id": d.contact_id,
            "channel": d.channel,
            "objective": d.objective,
            "target_role": d.target_role,
            "positioning_angle": d.positioning_angle,
            "message_text": d.message_text,
            "status": d.status,
            "missing_information": d.missing_information,
            "quality_gate_reasons": d.quality_gate_reasons,
            "approved_at": d.approved_at,
            "approved_by": d.approved_by,
            "reviewed_at": d.reviewed_at,
            "reviewed_by": d.reviewed_by,
            "review_note": d.review_note,
            "is_current_strategy_version": latest_strategy_id_by_opportunity.get(d.opportunity_id) == d.gtm_strategy_id,
            "created_at": d.created_at,
        }
        for d in rows
    ]
