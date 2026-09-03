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

from app.db.models import Base, Contact
from app.gtm_os.opportunity.offering_config import get_offering_messaging_reference
from app.gtm_os.opportunity.opportunity import TERMINAL_STATUSES, Opportunity
from app.gtm_os.sales.contact_discovery import get_eligible_contacts
from app.gtm_os.sales.sales_agent import evaluate_decision_maker, evaluate_sales_readiness, gather_account_research, prepare_message
from app.gtm_os.strategy.strategy import GtmStrategy
from app.llm_client import generate_json

# Autonomous-pipeline batch entry point (GTM-OS end-to-end wiring) -- until now,
# generate_message_draft() had ZERO callers anywhere in this codebase (confirmed by a full-repo
# grep): nothing ever looped over eligible Opportunities and invoked it, so no MessageDraft row
# had ever been created automatically. This is the missing wrapper, same shape as every other
# `run_*_sweep` in this feature (bounded `limit`, per-item failure isolation, idempotent).
#
# COST CONTAINMENT (real LLM spend per call): this codebase's BudgetGuard (app/budget_guard.py)
# is Deepline-credit-specific -- it re-checks a real Deepline balance, which has no meaning for
# Claude/Gemini token spend, so it cannot be reused here without inventing a mismatched concept
# (violating "do not create a second spend-control mechanism" the other direction). Instead this
# reuses the exact bounding convention every other sweep in this codebase already uses for cost
# containment: a per-cycle `limit`, same as PROFILES_PER_BATCH/run_gtm_strategy_sweep's own
# `limit=200`. Default kept intentionally small (20) specifically because this is the one stage
# in the chain that spends real money per item, unlike its siblings.

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
Target contact's real name: {target_name}
Target role: {target_role}
Positioning angle: {positioning_angle}
Known, real facts you may reference (nothing else): {personalization_inputs}

{messaging_reference_section}

STRICT RULES -- you must not violate any of these:
- Do NOT invent or assume any company fact, contact fact, pain point, budget, timeline, \
initiative, metric, customer story, or product capability that is not explicitly listed above.
- Do NOT reference specific numbers, dates, or claims unless they appear above.
- If a real target contact name is given above, greet THAT PERSON by their first name only. \
NEVER greet or address a company name as if it were a person (the personalization facts above \
may mention a company name -- that is never who you are writing to).
- If no real target contact name is given above (it says "unknown"), use a generic greeting \
("Hi," or "Hi there,") -- do not invent a name.
- Do NOT include any closing signature, sign-off line, or placeholder (no "Best,", no \
"[Your Name]", no "Regards," followed by a blank/bracketed name) -- no real sender identity is \
available to you, so end the message with its final substantive sentence instead.
- If the information above is not enough to write a genuinely grounded, specific message, return \
null for both subject and message_text rather than writing something generic or invented.
- Keep the message short (3-5 sentences), professional, and directly grounded in the facts above.
- subject is a short email subject line grounded in the same facts (a few words, no invented \
claims) -- only meaningful when channel is "email"; still provide a short one even when channel \
is not email, it just won't be used for sending.

Return JSON exactly:
{{"subject": "<the email subject line, or null>", "message_text": "<the drafted message, or null>", "reason": "<one sentence explaining your decision>"}}"""

# 2026-08-26, explicit instruction -- "feed all of them into the system and the system should
# generate based on that pattern and tone, either those might be exact or similar." Reused by
# both the primary-message and follow-up prompts below -- same reference material, same
# instruction not to fabricate around it. Returns "" when the matched offering has no real
# historical examples on file (e.g. Sales OS today), so the prompt section simply doesn't appear
# rather than inventing placeholder guidance.
def _messaging_reference_section(messaging_reference: dict) -> str:
    examples = messaging_reference.get("proven_message_examples") or []
    note = messaging_reference.get("messaging_pattern_note")
    if not examples and not note:
        return ""
    lines = ["Real historical messages for this offering, with what's known about how they performed — use these to match the TONE and PATTERN (warm, personal, specific beats stats/scarcity/credentials-first, per the note below), not as a script to copy verbatim; your version may be exact or similar, but must stay grounded in the real facts given above, never in facts only these examples mention:"]
    for example in examples:
        lines.append(f"- \"{example}\"")
    if note:
        lines.append(f"Pattern note: {note}")
    return "\n".join(lines)


FOLLOWUP_GENERATION_PROMPT = """You are drafting a SHORT follow-up message, to be sent only if \
the person did not reply to an earlier outbound message. Using ONLY the structured information \
provided below.

Channel: {channel}
Target contact's real name: {target_name}
The earlier message this is following up on: {primary_message_text}

{messaging_reference_section}

STRICT RULES -- you must not violate any of these:
- This is a brief, low-pressure check-in, NOT a second full pitch -- real historical top follow-ups \
in this system are one short sentence (e.g. "Did you get a chance to check this out?"). Do not \
repeat the full pitch from the earlier message.
- Do NOT invent or assume any fact not present in the earlier message or the reference examples above.
- If a real target contact name is given above, address THAT PERSON by their first name only.
- Do NOT include any closing signature, sign-off line, or placeholder.
- Keep it to 1-2 short sentences.

Return JSON exactly:
{{"message_text": "<the short follow-up message, or null if a genuine follow-up cannot be written from the given information>", "reason": "<one sentence explaining your decision>"}}"""


class MessageDraft(Base):
    __tablename__ = "message_drafts"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False)
    gtm_strategy_id = Column(Integer, ForeignKey("gtm_strategies.id"), nullable=False)  # pins to one immutable Strategy version -- see unique index in ensure_indexes()
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)

    channel = Column(String, nullable=True)
    # 2026-08-26, explicit instruction -- SalesRobot campaigns created this session have a real
    # Step 3 ("did they reply? no -> send a follow-up") that needs its OWN generated content,
    # distinct from the primary pitch (see FOLLOWUP_GENERATION_PROMPT below) -- "primary" for
    # every draft created before this column existed and every ordinary draft since (backward
    # compatible default), "followup" for the new second row generate_message_draft() now also
    # creates per contact/channel. Widened the (opportunity, strategy, contact, channel) unique
    # indexes in ensure_indexes() to include this, so a primary and a followup row can coexist.
    message_role = Column(String, nullable=False, default="primary")
    objective = Column(Text, nullable=True)
    target_role = Column(String, nullable=True)
    positioning_angle = Column(Text, nullable=True)
    evidence_basis = Column(JSON, nullable=True)
    personalization_inputs = Column(JSON, nullable=True)

    # V2 Phase 7 follow-up (2026-08-21) -- generated in the SAME LLM call as message_text (see
    # MESSAGE_GENERATION_PROMPT), never a second call. Nullable for backward compatibility --
    # every draft created before this field existed has subject=NULL and stays fully readable;
    # nothing re-generates or invalidates them. Null whenever message_text is also null
    # (insufficient_context) or when the model itself returned null.
    subject = Column(Text, nullable=True)
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
    """ANY draft for this (opportunity, strategy) pair, regardless of which contact it targets
    -- used only where "has generation been attempted for this strategy version at all" is the
    real question (run_message_generation_sweep's own gate, unchanged since Phase 5/6/7). Since
    V2 Phase 8, more than one row can exist here (one per contact in the sequence) -- this
    function intentionally does not care which one it returns for that use case."""
    return (
        db.query(MessageDraft)
        .filter(MessageDraft.tenant_id == tenant_id, MessageDraft.opportunity_id == opportunity_id, MessageDraft.gtm_strategy_id == gtm_strategy_id)
        .first()
    )


def _existing_draft_for_contact(db: Session, tenant_id: int, opportunity_id: int, gtm_strategy_id: int, contact_id: int | None, channel: str | None = None, message_role: str = "primary") -> MessageDraft | None:
    """V2 Phase 8 -- the real idempotency check generate_message_draft() itself uses: is there
    ALREADY a draft for this exact (opportunity, strategy, contact, channel) combination?
    Distinguishes contact_id IS NULL (the insufficient_context/no-contact-yet case, still capped
    at one draft per strategy version -- see the partial unique index in ensure_indexes()) from a
    specific contact (one draft per contact, once contact discovery/sequencing has a real person
    to target).

    channel (2026-08-25, explicit instruction): a real contact with BOTH a LinkedIn URL and an
    email now gets a draft PER channel (matching V1's own dual-channel behavior, see
    generate_message_draft()'s own docstring) -- so the idempotency check must also be scoped to
    channel, or generating the email draft would incorrectly be treated as "already exists"
    because the LinkedIn draft for this same contact already does. channel=None preserves the
    original (opportunity, strategy, contact)-only lookup for callers that don't care which
    channel (e.g. _existing_draft's own "any draft at all" use case)."""
    query = db.query(MessageDraft).filter(
        MessageDraft.tenant_id == tenant_id, MessageDraft.opportunity_id == opportunity_id, MessageDraft.gtm_strategy_id == gtm_strategy_id,
        MessageDraft.message_role == message_role,
    )
    if contact_id is None:
        query = query.filter(MessageDraft.contact_id.is_(None))
    else:
        query = query.filter(MessageDraft.contact_id == contact_id)
    if channel is not None:
        query = query.filter(MessageDraft.channel == channel)
    return query.first()


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


def _draft_message_for_channel(db: Session, tenant_id: int, opportunity: Opportunity, strategy: GtmStrategy, contact_id: int | None, prep: dict, channel: str | None) -> MessageDraft:
    """Generates and persists ONE MessageDraft for ONE specific channel, reusing the SAME shared
    `prep` (objective/target_role/target_name/positioning_angle/personalization_inputs/
    evidence_basis are identical across channels for the same contact -- only the channel itself,
    and therefore the drafted text, differs). Extracted 2026-08-25 so generate_message_draft()
    can call this once per available channel (V1 parity -- see that function's own docstring)
    instead of being hardcoded to exactly one channel."""
    if prep["status"] != "ready":
        draft = MessageDraft(
            tenant_id=tenant_id, opportunity_id=opportunity.id, gtm_strategy_id=strategy.id, contact_id=contact_id,
            channel=channel, message_role="primary", objective=prep["objective"], target_role=prep["target_role"],
            positioning_angle=prep["positioning_angle"], evidence_basis=prep["evidence_basis"],
            personalization_inputs=prep["personalization_inputs"], message_text=None,
            generation_method="deterministic:insufficient_context", missing_information=prep["missing_information"],
            status="insufficient_context", quality_gate_reasons=None,
        )
        db.add(draft)
        db.commit()
        return draft

    messaging_reference = get_offering_messaging_reference(db, tenant_id, strategy.matched_offering_name)
    prompt = MESSAGE_GENERATION_PROMPT.format(
        objective=prep["objective"], channel=channel, target_role=prep["target_role"] or "unknown",
        target_name=prep.get("target_name") or "unknown",
        positioning_angle=prep["positioning_angle"] or "none provided",
        personalization_inputs="; ".join(prep["personalization_inputs"]) or "none",
        messaging_reference_section=_messaging_reference_section(messaging_reference),
    )

    try:
        result = generate_json(prompt, db, tenant_id, max_tokens=300)
    except Exception as e:  # noqa: BLE001 -- an LLM outage must never crash the caller
        draft = MessageDraft(
            tenant_id=tenant_id, opportunity_id=opportunity.id, gtm_strategy_id=strategy.id, contact_id=contact_id,
            channel=channel, message_role="primary", objective=prep["objective"], target_role=prep["target_role"],
            positioning_angle=prep["positioning_angle"], evidence_basis=prep["evidence_basis"],
            personalization_inputs=prep["personalization_inputs"], message_text=None,
            generation_method="llm:generate_json", missing_information=[f"llm_call_failed: {e}"],
            status="insufficient_context", quality_gate_reasons=None,
        )
        db.add(draft)
        db.commit()
        return draft

    message_text = result.get("message_text") if isinstance(result, dict) else None
    subject = result.get("subject") if isinstance(result, dict) and isinstance(result.get("subject"), str) else None

    if not message_text or not isinstance(message_text, str):
        draft = MessageDraft(
            tenant_id=tenant_id, opportunity_id=opportunity.id, gtm_strategy_id=strategy.id, contact_id=contact_id,
            channel=channel, message_role="primary", objective=prep["objective"], target_role=prep["target_role"],
            positioning_angle=prep["positioning_angle"], evidence_basis=prep["evidence_basis"],
            personalization_inputs=prep["personalization_inputs"], subject=None, message_text=None,
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
        channel=channel, message_role="primary", objective=prep["objective"], target_role=prep["target_role"],
        positioning_angle=prep["positioning_angle"], evidence_basis=prep["evidence_basis"],
        personalization_inputs=prep["personalization_inputs"], subject=subject, message_text=message_text,
        generation_method="llm:generate_json", missing_information=prep["missing_information"],
        status=status, quality_gate_reasons=quality_gate_reasons or None,
    )
    db.add(draft)
    db.commit()
    return draft


def _draft_followup_message(db: Session, tenant_id: int, opportunity: Opportunity, strategy: GtmStrategy, contact_id: int | None, prep: dict, channel: str | None, primary_message_text: str) -> MessageDraft:
    """The real Step-3 ("no reply yet") content the SalesRobot campaigns created 2026-08-26
    expect (see FOLLOWUP_GENERATION_PROMPT) -- a second, short, low-pressure MessageDraft row
    (message_role="followup") distinct from the primary pitch. Only called once a primary draft
    with real message_text exists (see generate_message_draft()) -- there is nothing to follow up
    on otherwise."""
    messaging_reference = get_offering_messaging_reference(db, tenant_id, strategy.matched_offering_name)
    prompt = FOLLOWUP_GENERATION_PROMPT.format(
        channel=channel, target_name=prep.get("target_name") or "unknown",
        primary_message_text=primary_message_text,
        messaging_reference_section=_messaging_reference_section(messaging_reference),
    )

    common_fields = dict(
        tenant_id=tenant_id, opportunity_id=opportunity.id, gtm_strategy_id=strategy.id, contact_id=contact_id,
        channel=channel, message_role="followup", objective=prep["objective"], target_role=prep["target_role"],
        positioning_angle=prep["positioning_angle"], evidence_basis=prep["evidence_basis"],
        personalization_inputs=prep["personalization_inputs"],
    )

    try:
        result = generate_json(prompt, db, tenant_id, max_tokens=150)
    except Exception as e:  # noqa: BLE001 -- an LLM outage must never crash the caller
        draft = MessageDraft(**common_fields, message_text=None, generation_method="llm:generate_json",
                              missing_information=[f"llm_call_failed: {e}"], status="insufficient_context", quality_gate_reasons=None)
        db.add(draft)
        db.commit()
        return draft

    message_text = result.get("message_text") if isinstance(result, dict) else None
    if not message_text or not isinstance(message_text, str):
        draft = MessageDraft(**common_fields, message_text=None, generation_method="llm:generate_json",
                              missing_information=[f"LLM declined to generate: {result.get('reason', 'no reason given') if isinstance(result, dict) else 'malformed output'}"],
                              status="insufficient_context", quality_gate_reasons=None)
        db.add(draft)
        db.commit()
        return draft

    quality_gate_reasons = _run_quality_gate(opportunity, strategy, contact_id, prep, message_text)
    draft = MessageDraft(**common_fields, message_text=message_text, generation_method="llm:generate_json",
                          missing_information=None, status="draft" if quality_gate_reasons else "ready_for_review",
                          quality_gate_reasons=quality_gate_reasons or None)
    db.add(draft)
    db.commit()
    return draft


def generate_message_draft(db: Session, tenant_id: int, opportunity: Opportunity, strategy: GtmStrategy, exclude_contact_ids=None) -> MessageDraft:
    """The only function that calls the LLM. Idempotent: an existing draft for this exact
    (opportunity, strategy version, TARGET CONTACT, CHANNEL) is reused/skipped -- never
    regenerated, and NEVER overwritten once approved (Part N/K).

    2026-08-25, explicit instruction -- V1 parity: a contact with BOTH a LinkedIn URL and an
    email gets a draft for EACH channel (matching personalized_outreach.py's real
    generate_personalized_message(), which always attempts LinkedIn and additionally drafts an
    email whenever contact.email is populated -- confirmed by reading that code). This function's
    RETURN CONTRACT is unchanged (still returns exactly one MessageDraft, the primary/first
    available channel) so every existing caller (recover_message_draft_if_unblocked,
    run_message_generation_sweep, regenerate_message_draft) keeps working exactly as before;
    any additional channel is generated as a best-effort side effect, same "a failure here must
    never wipe out the primary message" discipline V1's own email-generation branch already uses.

    exclude_contact_ids (V2 Phase 8, optional, default None -- every pre-Phase-8 caller passes
    nothing and resolves the exact same primary contact as before): threaded straight into
    evaluate_decision_maker(), so contacts[0] of what's left after exclusion becomes the target
    -- this is how outreach_sequencing.py gets a real draft for "the next fallback contact"
    without any separate contact-selection logic living here or there.

    The target contact must be resolved BEFORE the idempotency check now (existence depends on
    WHICH contact, not just the opportunity+strategy pair) -- this costs one extra read
    (evaluate_decision_maker's own Contact query) on the steady-state "draft already exists"
    path, compared to before Phase 8; still zero LLM/paid calls on that path."""
    decision_maker = evaluate_decision_maker(db, tenant_id, opportunity.company_id, opportunity.affected_function, exclude_contact_ids=exclude_contact_ids)
    contact_id = decision_maker["contacts"][0]["id"] if decision_maker["status"] == "known" and decision_maker["contacts"] else None

    research = gather_account_research(db, tenant_id, opportunity)
    prep = prepare_message(db, tenant_id, opportunity, strategy, research, decision_maker)
    primary_channel = prep.get("channel")

    # Real bug fix (2026-08-25, confirmed live): this used to return immediately when the
    # PRIMARY channel's draft already existed, which meant the extra-channel loop below never
    # even ran for an opportunity whose LinkedIn draft was generated before the email-drafting
    # feature existed -- confirmed live, re-running this for all 4 real opportunities produced
    # zero new email drafts because every primary (LinkedIn) draft already existed. Now only
    # SKIPS re-generating the primary when it already exists (still zero LLM calls on that path);
    # it always still checks/generates any other available channel.
    existing_primary = _existing_draft_for_contact(db, tenant_id, opportunity.id, strategy.id, contact_id, channel=primary_channel)
    primary_draft = existing_primary if existing_primary is not None else _draft_message_for_channel(db, tenant_id, opportunity, strategy, contact_id, prep, primary_channel)

    # Best-effort additional channel(s) -- V1 parity (see docstring). Never affects the returned
    # value or raises: a failure drafting the email must never look like a failure of the whole
    # (already-succeeded) primary draft.
    for extra_channel in (prep.get("available_channels") or [])[1:]:
        try:
            if _existing_draft_for_contact(db, tenant_id, opportunity.id, strategy.id, contact_id, channel=extra_channel) is None:
                _draft_message_for_channel(db, tenant_id, opportunity, strategy, contact_id, prep, extra_channel)
        except Exception:  # noqa: BLE001 -- see docstring, must never affect the primary draft
            pass

    # 2026-08-26, explicit instruction -- a real Step-3 follow-up (see FOLLOWUP_GENERATION_PROMPT),
    # only for the primary channel (SalesRobot's own campaign sequence is what "step 3" belongs
    # to; email has no equivalent concept here). Only when the primary draft actually has content
    # to follow up on -- best-effort, same "never affects the primary draft" discipline as above.
    if primary_draft.message_text:
        try:
            if _existing_draft_for_contact(db, tenant_id, opportunity.id, strategy.id, contact_id, channel=primary_channel, message_role="followup") is None:
                _draft_followup_message(db, tenant_id, opportunity, strategy, contact_id, prep, primary_channel, primary_draft.message_text)
        except Exception:  # noqa: BLE001 -- see docstring, must never affect the primary draft
            pass

    return primary_draft


def recover_message_draft_if_unblocked(
    db: Session, tenant_id: int, opportunity: Opportunity, strategy: GtmStrategy, existing: MessageDraft
) -> tuple[MessageDraft, bool]:
    """V2 Phase 5 -- message-draft recovery. `existing` must be a real, already-found MessageDraft
    for this exact (opportunity, strategy) pair (see _existing_draft) -- this function never
    looks one up itself, to keep its precondition explicit at every call site.

    Returns (draft, was_recovered). was_recovered=True ONLY when the stale draft was actually
    deleted and regenerated by this call.

    THE ONE RECOVERY CONDITION IMPLEMENTED (see module docstring's "First inspect" findings for
    why not more): `existing.status == "insufficient_context"` AND `existing.contact_id IS NULL`
    -- the exact, already-persisted, deterministic signal that decision_maker["status"] was NOT
    "known" at draft-creation time (see generate_message_draft()'s own contact_id ternary: it is
    set to None in EXACTLY that case, never for any other insufficient_context reason). This is
    the literal "missing contact -> retry when an eligible Contact now exists" case the phase
    asked for, using data the system already persists -- no new field, no string-matching
    free-text missing_information.

    Every OTHER insufficient_context reason (offering fit not confirmed, strategy not ready, an
    already-selected contact simply lacking a channel, an LLM decline/failure) leaves contact_id
    set or is a different generation_method entirely -- those are explicitly NOT treated as
    recoverable here, per "do NOT guess new retry rules for unknown reasons." Every
    non-insufficient_context status (draft, ready_for_review, approved, rejected,
    changes_requested) is also returned completely untouched -- this function's very first
    check already excludes them.

    Re-checks evaluate_decision_maker() FRESH (never a cached/stale read) -- a suppressed-only
    contact set, or a company that still has zero eligible contacts, correctly leaves the draft
    exactly as it was. Deletes the stale row and calls generate_message_draft() -- completely
    UNMODIFIED, zero business-logic changes beyond this recovery condition -- to regenerate,
    rather than updating fields in place, so the existing (opportunity_id, gtm_strategy_id)
    UNIQUE index is never violated by two rows coexisting even momentarily (delete, commit,
    THEN insert)."""
    if existing.status != "insufficient_context" or existing.contact_id is not None:
        return existing, False

    decision_maker = evaluate_decision_maker(db, tenant_id, opportunity.company_id, opportunity.affected_function)
    if decision_maker["status"] != "known":
        return existing, False

    db.delete(existing)
    db.commit()
    return generate_message_draft(db, tenant_id, opportunity, strategy), True


def regenerate_message_draft(db: Session, tenant_id: int, draft_id: int, contact_id: int | None = None) -> MessageDraft:
    """V2 Frontend Phase (Message Workspace) -- the real single-draft regeneration this project
    never had (V1's own "Regenerate" is just a full re-run of /generate-message overwriting the
    one PersonalizedMessage row; V2 had no regenerate path at all before this).

    Adapted from recover_message_draft_if_unblocked()'s already-proven delete-then-generate
    pattern: delete the existing row, commit, THEN call generate_message_draft() completely
    UNMODIFIED -- so every real constraint it already enforces (idempotency, the
    (opportunity_id, gtm_strategy_id[, contact_id]) unique index, the same contact-ranking
    (contact_ranking.py) and suppression rules (Contact.excluded_from_push) via
    evaluate_decision_maker(), the same deterministic quality gate, the same subject/message_text
    generation) is preserved exactly. Zero new business logic beyond a controlled re-invocation of
    what already exists.

    Does NOT touch MessageSendAttempt history -- a separate table, untouched here; regenerating a
    draft's content has no bearing on what was already (attempted to be) sent under a prior
    version of that draft.

    contact_id, if given, MUST already be one of get_eligible_contacts()'s real results for this
    opportunity's company -- checked here, not left to the caller, so a suppressed/ineligible
    contact can never be forced onto a regenerated draft even if a stale id reaches this
    function. To force evaluate_decision_maker() to resolve to exactly that contact, every OTHER
    eligible contact is passed as exclude_contact_ids -- the exact same real exclusion mechanism
    outreach_sequencing.py already relies on for its own fallback-contact selection, not a second
    contact-selection path."""
    draft = db.get(MessageDraft, draft_id)
    if draft is None or draft.tenant_id != tenant_id:
        raise ValueError(f"no MessageDraft {draft_id} for tenant {tenant_id}")

    opportunity = db.get(Opportunity, draft.opportunity_id)
    if opportunity is None or opportunity.tenant_id != tenant_id:
        raise ValueError(f"no Opportunity for MessageDraft {draft_id}")

    strategy = db.get(GtmStrategy, draft.gtm_strategy_id)
    if strategy is None or strategy.tenant_id != tenant_id:
        raise ValueError(f"no GtmStrategy for MessageDraft {draft_id}")

    exclude_contact_ids = None
    if contact_id is not None:
        eligible = get_eligible_contacts(db, opportunity.company_id)
        eligible_ids = {c.id for c in eligible}
        if contact_id not in eligible_ids:
            raise ValueError(f"contact {contact_id} is not an eligible contact for this opportunity's company")
        exclude_contact_ids = [c.id for c in eligible if c.id != contact_id]

    db.delete(draft)
    db.commit()
    return generate_message_draft(db, tenant_id, opportunity, strategy, exclude_contact_ids=exclude_contact_ids)


def update_message_draft_content(db: Session, tenant_id: int, message_draft_id: int, subject: str | None = None, message_text: str | None = None) -> MessageDraft:
    """2026-08-25, explicit instruction -- real human editing of AI-generated content: the
    system drafts subject/message_text, a human can then directly correct it (not just
    request-changes-and-regenerate) before approval. Never touches an already-approved draft --
    same immutability rule approve_message_draft() itself establishes (Part N/K); once approved,
    the record is a fact about what was actually sent/is being sent, not a draft to keep editing.
    Only updates the field(s) actually passed (None means "leave unchanged"), so a caller editing
    just the body doesn't have to also resend the subject."""
    draft = db.get(MessageDraft, message_draft_id)
    if draft is None or draft.tenant_id != tenant_id:
        raise ValueError(f"no MessageDraft {message_draft_id} for tenant {tenant_id}")
    if draft.status == "approved":
        raise ValueError("cannot edit an already-approved draft")

    if subject is not None:
        draft.subject = subject
    if message_text is not None:
        draft.message_text = message_text
    draft.last_updated_at = datetime.utcnow()
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
            # 2026-08-26 -- the new message_role="followup" row (see MessageDraft.message_role)
            # is a real, distinct draft, but the V2 Messages tab/Message Workspace UI has no
            # concept of it yet and would render it as an unexplained duplicate card next to its
            # primary. Scoped out here rather than changing this API's shape; revisit if/when the
            # UI is built to show a follow-up alongside its primary message.
            MessageDraft.message_role == "primary",
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

    # 2026-08-25, real Message Workspace UI need: the frontend must show WHO a draft is actually
    # addressed to (real name + email), not just an opaque contact_id. One bulk query for every
    # distinct contact_id in this result set -- same "bulk fetch, never per-row" discipline
    # already used above for latest_strategy_id_by_opportunity.
    contact_ids = list({d.contact_id for d in rows if d.contact_id is not None})
    contact_by_id: dict[int, Contact] = {}
    if contact_ids:
        contact_by_id = {c.id: c for c in db.query(Contact).filter(Contact.id.in_(contact_ids)).all()}

    def _contact_fields(contact_id):
        contact = contact_by_id.get(contact_id) if contact_id is not None else None
        if contact is None:
            return {"contact_name": None, "contact_email": None, "contact_title": None}
        return {
            "contact_name": f"{contact.first_name or ''} {contact.last_name or ''}".strip() or None,
            "contact_email": contact.email,
            "contact_title": contact.title,
        }

    return [
        {
            "id": d.id,
            "opportunity_id": d.opportunity_id,
            "gtm_strategy_id": d.gtm_strategy_id,
            "contact_id": d.contact_id,
            **_contact_fields(d.contact_id),
            "channel": d.channel,
            "objective": d.objective,
            "target_role": d.target_role,
            "positioning_angle": d.positioning_angle,
            "subject": d.subject,  # V2 Frontend Phase (Message Workspace) -- real column, was never selected here before
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


def _latest_strategy_for_opportunity(db: Session, tenant_id: int, opportunity_id: int) -> GtmStrategy | None:
    return (
        db.query(GtmStrategy)
        .filter(GtmStrategy.tenant_id == tenant_id, GtmStrategy.opportunity_id == opportunity_id)
        .order_by(GtmStrategy.id.desc())
        .first()
    )


def run_message_generation_sweep(db: Session, tenant_id: int, limit: int = 20) -> dict:
    """Evaluates up to `limit` Opportunities whose LATEST GtmStrategy has reached
    evaluate_sales_readiness()'s "ready_for_message" state (the same readiness gate
    account_agent.py/execution_readiness.py already use -- not re-derived here) and generates a
    draft via generate_message_draft() (unmodified) for each.

    IDEMPOTENT BY CONSTRUCTION: generate_message_draft() itself already looks up
    (opportunity_id, gtm_strategy_id) before writing (see _existing_draft()) and returns the
    existing row instead of creating a duplicate -- this sweep adds no additional dedup logic of
    its own, it only decides WHICH opportunities are worth calling that already-idempotent
    function for. A rerun against unchanged data creates zero new rows and spends zero additional
    tokens (existing drafts are returned immediately, no LLM call).

    Never regenerates an ALREADY-drafted/reviewed/approved message just because the strategy
    hasn't changed -- only opportunities with no MessageDraft row for their current strategy
    version are counted as "eligible" and passed to generate_message_draft(). The ONE exception
    (V2 Phase 5): an existing draft that's specifically stuck on a missing contact gets a
    recovery check via recover_message_draft_if_unblocked() -- see that function's own
    docstring for the exact, narrow condition; every other existing-draft case still just
    counts as already_drafted, completely unchanged from before.

    One opportunity's failure never aborts the sweep. Makes real LLM calls (see
    generate_message_draft()'s own module docstring) -- bounded by `limit`, see this module's own
    cost-containment note above."""
    counts = {"evaluated": 0, "eligible": 0, "drafted": 0, "recovered": 0, "already_drafted": 0, "not_ready": 0, "failed": 0}

    opportunity_ids = [
        row[0]
        for row in db.query(Opportunity.id)
        .filter(Opportunity.tenant_id == tenant_id)
        # Finished opportunities are not re-worked -- see TERMINAL_STATUSES. Without this every
        # stage re-processed every opportunity ever created, on every run.
        .filter(Opportunity.status.notin_(TERMINAL_STATUSES))
        .order_by(Opportunity.id)
        .all()
    ]

    for opportunity_id in opportunity_ids:
        if counts["drafted"] >= limit:
            break
        try:
            opportunity = db.get(Opportunity, opportunity_id)
            if opportunity is None or opportunity.tenant_id != tenant_id:
                continue
            counts["evaluated"] += 1

            strategy = _latest_strategy_for_opportunity(db, tenant_id, opportunity.id)
            if strategy is None:
                counts["not_ready"] += 1
                continue

            readiness = evaluate_sales_readiness(strategy)
            if readiness["status"] != "ready_for_message":
                counts["not_ready"] += 1
                continue

            counts["eligible"] += 1
            existing = _existing_draft(db, tenant_id, opportunity.id, strategy.id)
            if existing is not None:
                _recovered_draft, was_recovered = recover_message_draft_if_unblocked(db, tenant_id, opportunity, strategy, existing)
                if was_recovered:
                    counts["recovered"] += 1
                else:
                    counts["already_drafted"] += 1
                continue

            generate_message_draft(db, tenant_id, opportunity, strategy)
            counts["drafted"] += 1

        except Exception:  # noqa: BLE001 -- one opportunity's failure must never block the others, same pattern as every other sweep in this feature
            # Roll back before anything else: a per-item failure must not poison the SHARED session.
            # Neon drops idle connections, and this sweep idles ~40s per LLM call under Gemini rate
            # limiting -- when that lands mid-loop the transaction is left invalid and EVERY later item
            # AND stage dies with PendingRollbackError. Confirmed live: run 122 lost its connection
            # during topic_linking, then burned 233 minutes failing everything after it. The stage-level
            # handler already rolled back; these per-item ones did not.
            db.rollback()
            counts["failed"] += 1

    return counts
