"""Overrides & Evals -- the first real layer of the learning loop.

Reuses, never re-implements: MessageDraft's own review lifecycle (Phase 7's status/review_note/
reviewed_at/reviewed_by), CalendarBooking's own outcome fields (Meeting Outcomes/Revenue Pace),
and the meeting -> Opportunity attribution link added alongside this module. This file contains
no message-review engine and no outcome engine of its own -- it only reads what those already
recorded and composes a readout, plus one genuinely new thing: a deterministic, keyword-based
categorization of the free-text reasons humans already write, and a threshold-based detector that
notices when the same category keeps recurring.

get_overrides_evals() is pure read-only -- it NEVER creates, updates, or deletes a
ConfirmedPattern row. Candidate patterns shown by that function are computed fresh every call,
never persisted (Part 7's own instruction: "do not persist a generated queue/readout"). A
ConfirmedPattern row only comes into existence the moment a human calls confirm_pattern() or
dismiss_pattern() -- before that, a "candidate" is not a database row at all, just a live
computation, identified by its category (the only stable, meaningful key available for something
that doesn't exist as a row yet).

No LLM anywhere in this file. No message generation, no strategy generation, no autonomous
sending, no CRM mutation, no campaign execution -- confirming/dismissing a pattern only ever
writes to ConfirmedPattern itself; see confirm_pattern()/dismiss_pattern()'s own docstrings for
the explicit list of what they never touch."""

from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Session

from app.db.models import Base, Batch, CalendarBooking, Company
from app.gtm_os.learning.human_knowledge import list_human_knowledge
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.learning.pattern_detection_config import get_pattern_detection_config
from app.gtm_os.opportunity.opportunity import Opportunity

RECENT_LIMIT = 20

# Deterministic, inspectable keyword buckets (Part 4 -- explicitly no LLM). There is no real
# review/outcome text in this tenant's database yet to refine these against (0 MessageDrafts, 0
# CalendarBooking outcomes exist for tenant 2 as of this phase) -- these are exactly the
# categories the spec itself proposes, kept as-is rather than invented or narrowed without real
# text to check them against. First matching category wins; anything matching none stays
# "unknown" rather than being forced into a bucket.
CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("pricing", ["too expensive", "pricing", "price is", "cost too high", "discount", "cheaper"]),
    ("budget", ["no budget", "budget cut", "can't afford", "funding fell through", "not funded"]),
    ("timing", ["not now", "too early", "next quarter", "bad timing", "revisit later", "not ready yet"]),
    ("wrong_person", ["wrong person", "wrong contact", "not the right person", "not the decision maker", "wrong decision-maker"]),
    ("wrong_fit", ["not a fit", "wrong fit", "doesn't fit", "not relevant to us", "not applicable"]),
    ("competition", ["competitor", "already using", "went with another", "chose a competitor", "signed with"]),
    ("feature_gap", ["missing feature", "doesn't have", "lacks", "feature gap", "can't support"]),
    ("no_response", ["no response", "no reply", "went dark", "unresponsive", "ghosted"]),
    ("messaging", ["too salesy", "generic message", "irrelevant message", "bad timing on message", "tone was off"]),
]


class ConfirmedPattern(Base):
    """A human-confirmed (or human-dismissed) recurring pattern -- the one thing in this whole
    feature worth permanently remembering, same reasoning GtmStrategy's own versioning already
    applies elsewhere: a human deciding something is itself a real, durable fact. Never
    auto-created -- see confirm_pattern()/dismiss_pattern() below, the only writers of this
    table."""
    __tablename__ = "confirmed_patterns"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    category = Column(String, nullable=False)
    trigger_description = Column(Text, nullable=False)  # e.g. "pricing appeared 4 times in the last 30 days"
    pattern_description = Column(Text, nullable=False)  # human-readable candidate statement
    status = Column(String, nullable=False, default="candidate")  # candidate | confirmed | dismissed
    source_event_refs = Column(JSON, nullable=False)  # [{"source_type": "message_draft"|"calendar_booking", "source_id": int}, ...]

    confirmed_by = Column(String, nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def categorize_reason(text: str | None) -> str:
    """Deterministic, inspectable -- no LLM. First matching keyword wins; no match -> 'unknown'.
    Never forces a category onto text that doesn't clearly indicate one."""
    if not text or not text.strip():
        return "unknown"
    lowered = text.lower()
    for category, keywords in CATEGORY_KEYWORDS:
        for keyword in keywords:
            if keyword in lowered:
                return category
    return "unknown"


def _override_events(db: Session, tenant_id: int, since: datetime | None = None) -> list[MessageDraft]:
    query = (
        db.query(MessageDraft)
        .filter(MessageDraft.tenant_id == tenant_id)
        .filter(MessageDraft.status.in_(("rejected", "changes_requested")))
    )
    if since is not None:
        query = query.filter(MessageDraft.reviewed_at >= since)
    return query.order_by(MessageDraft.reviewed_at.desc()).all()


def _tenant_scoped_bookings(db: Session, tenant_id: int):
    """CalendarBooking itself has no tenant_id column (a pre-existing architectural
    characteristic -- see revenue_pace.py). Unlike Revenue Pace (which only ever reads real
    tenant 2 in production, so this went unnoticed), Overrides & Evals needs REAL tenant
    isolation to be testable and correct -- achieved here by joining through
    outcome_company_id -> Company.batch_id -> Batch.tenant_id, the same real chain
    record_meeting_outcome() already validates against when a company_id is supplied. A booking
    with no outcome_company_id set is excluded entirely (unattributable to any tenant, so it
    must never leak into one)."""
    return (
        db.query(CalendarBooking)
        .join(Company, CalendarBooking.outcome_company_id == Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id)
    )


def _loss_events(db: Session, tenant_id: int, since: datetime | None = None) -> list[CalendarBooking]:
    query = _tenant_scoped_bookings(db, tenant_id).filter(CalendarBooking.outcome_status == "lost")
    if since is not None:
        query = query.filter(CalendarBooking.outcome_recorded_at >= since)
    return query.order_by(CalendarBooking.outcome_recorded_at.desc()).all()


def _company_name_for_opportunity(db: Session, opportunity: Opportunity | None) -> str | None:
    if opportunity is None:
        return None
    if opportunity.company_name_raw:
        return opportunity.company_name_raw
    company = db.get(Company, opportunity.company_id) if opportunity.company_id else None
    return company.name if company else None


def _serialize_override(db: Session, draft: MessageDraft) -> dict:
    opportunity = db.get(Opportunity, draft.opportunity_id)
    return {
        "source_type": "message_draft",
        "source_id": draft.id,
        "opportunity_id": draft.opportunity_id,
        "company_name": _company_name_for_opportunity(db, opportunity),
        "status": draft.status,
        "reason": draft.review_note,
        "category": categorize_reason(draft.review_note),
        "reviewed_at": draft.reviewed_at,
        "reviewed_by": draft.reviewed_by,
    }


def _serialize_loss(db: Session, booking: CalendarBooking) -> dict:
    company = db.get(Company, booking.outcome_company_id) if booking.outcome_company_id else None
    return {
        "source_type": "calendar_booking",
        "source_id": booking.id,
        "opportunity_id": booking.outcome_opportunity_id,
        "company_name": company.name if company else None,
        "reason": booking.outcome_reason,
        "category": categorize_reason(booking.outcome_reason),
        "outcome_recorded_at": booking.outcome_recorded_at,
    }


def _existing_pattern_rows(db: Session, tenant_id: int) -> dict[str, ConfirmedPattern]:
    """Latest row per category (a category can theoretically be confirmed, later dismissed, and
    later re-confirmed -- last decision wins for display, same "current state, not history"
    reasoning ICPMatch already uses elsewhere)."""
    rows = (
        db.query(ConfirmedPattern)
        .filter(ConfirmedPattern.tenant_id == tenant_id)
        .order_by(ConfirmedPattern.id.desc())
        .all()
    )
    by_category: dict[str, ConfirmedPattern] = {}
    for row in rows:
        if row.category not in by_category:
            by_category[row.category] = row
    return by_category


def get_overrides_evals(db: Session, tenant_id: int, month: str | None = None) -> dict:
    """Pure read-only. Never creates, updates, or deletes anything -- including ConfirmedPattern
    rows. Candidate patterns are computed fresh every call from real, currently-persisted
    override/loss evidence within the configured lookback window; they are not read from any
    "candidates" storage because none exists (see this module's own docstring)."""
    now = datetime.utcnow()
    if month:
        year, mon = (int(p) for p in month.split("-"))
        period_start = datetime(year, mon, 1)
        period_end = datetime(year + (1 if mon == 12 else 0), 1 if mon == 12 else mon + 1, 1)
        month_label = f"{year:04d}-{mon:02d}"
    else:
        period_start = datetime(now.year, now.month, 1)
        period_end = datetime(now.year + (1 if now.month == 12 else 0), 1 if now.month == 12 else now.month + 1, 1)
        month_label = f"{now.year:04d}-{now.month:02d}"

    # ---- Part 3 metrics, this month ----
    rejected_this_month = (
        db.query(MessageDraft)
        .filter(MessageDraft.tenant_id == tenant_id, MessageDraft.status == "rejected")
        .filter(MessageDraft.reviewed_at >= period_start, MessageDraft.reviewed_at < period_end)
        .count()
    )
    changes_requested_this_month = (
        db.query(MessageDraft)
        .filter(MessageDraft.tenant_id == tenant_id, MessageDraft.status == "changes_requested")
        .filter(MessageDraft.reviewed_at >= period_start, MessageDraft.reviewed_at < period_end)
        .count()
    )
    reviewed_with_reason_this_month = (
        db.query(MessageDraft)
        .filter(MessageDraft.tenant_id == tenant_id)
        .filter(MessageDraft.status.in_(("rejected", "changes_requested")))
        .filter(MessageDraft.review_note.isnot(None))
        .filter(MessageDraft.reviewed_at >= period_start, MessageDraft.reviewed_at < period_end)
        .count()
    )
    lost_with_reason_this_month = (
        _tenant_scoped_bookings(db, tenant_id)
        .filter(CalendarBooking.outcome_status == "lost", CalendarBooking.outcome_reason.isnot(None))
        .filter(CalendarBooking.outcome_recorded_at >= period_start, CalendarBooking.outcome_recorded_at < period_end)
        .count()
    )
    won_this_month = (
        _tenant_scoped_bookings(db, tenant_id)
        .filter(CalendarBooking.outcome_status == "won")
        .filter(CalendarBooking.outcome_recorded_at >= period_start, CalendarBooking.outcome_recorded_at < period_end)
        .count()
    )
    # V2 Phase 9 -- total lost count (mirrors won_this_month exactly), not just the "with a real
    # reason" subset already computed above -- Outcome Summary needs the real total to sit
    # alongside "Won", the same way meetings_lost_with_reason already sits inside Failure Reasons.
    lost_this_month = (
        _tenant_scoped_bookings(db, tenant_id)
        .filter(CalendarBooking.outcome_status == "lost")
        .filter(CalendarBooking.outcome_recorded_at >= period_start, CalendarBooking.outcome_recorded_at < period_end)
        .count()
    )
    attributable_outcomes_this_month = (
        _tenant_scoped_bookings(db, tenant_id)
        .filter(CalendarBooking.outcome_status.in_(("won", "lost")))
        .filter(CalendarBooking.outcome_opportunity_id.isnot(None))
        .filter(CalendarBooking.outcome_recorded_at >= period_start, CalendarBooking.outcome_recorded_at < period_end)
        .count()
    )
    total_outcomes_this_month = (
        _tenant_scoped_bookings(db, tenant_id)
        .filter(CalendarBooking.outcome_status.in_(("won", "lost")))
        .filter(CalendarBooking.outcome_recorded_at >= period_start, CalendarBooking.outcome_recorded_at < period_end)
        .count()
    )

    # ---- Part 3: "accuracy %" -- deliberately NOT computed. No ground truth exists anywhere in
    # this codebase for "was this recommendation correct" (a rejected draft reflects a human
    # judgment call, not a provably wrong system output) -- shown as unavailable, not fabricated.
    accuracy = {
        "available": False,
        "value": None,
        "reason": "No defensible numerator/denominator exists -- a rejected or changes-requested "
        "MessageDraft reflects a human judgment call, not a provable system error, and nothing "
        "in this codebase records a ground-truth 'correct recommendation' concept to measure "
        "against.",
    }

    # ---- recent overrides / losses (not month-bound -- most recent N, real, capped) ----
    recent_overrides = [_serialize_override(db, d) for d in _override_events(db, tenant_id)[:RECENT_LIMIT]]
    recent_losses = [_serialize_loss(db, b) for b in _loss_events(db, tenant_id)[:RECENT_LIMIT]]

    # ---- Part 5: candidate pattern detection, within the configured lookback window ----
    config = get_pattern_detection_config(db, tenant_id)
    lookback_start = now - timedelta(days=config["lookback_days"])
    window_overrides = _override_events(db, tenant_id, since=lookback_start)
    window_losses = _loss_events(db, tenant_id, since=lookback_start)

    by_category: dict[str, list[dict]] = {}
    for draft in window_overrides:
        category = categorize_reason(draft.review_note)
        by_category.setdefault(category, []).append({"source_type": "message_draft", "source_id": draft.id, "reason": draft.review_note})
    for booking in window_losses:
        category = categorize_reason(booking.outcome_reason)
        by_category.setdefault(category, []).append({"source_type": "calendar_booking", "source_id": booking.id, "reason": booking.outcome_reason})

    existing_by_category = _existing_pattern_rows(db, tenant_id)

    recurring_categories = [
        {"category": cat, "count": len(events)}
        for cat, events in sorted(by_category.items(), key=lambda kv: -len(kv[1]))
        if cat != "unknown"
    ]

    candidate_patterns = []
    confirmed_patterns = []
    dismissed_patterns = []

    for cat, existing in existing_by_category.items():
        row = {
            "id": existing.id,
            "category": existing.category,
            "trigger_description": existing.trigger_description,
            "pattern_description": existing.pattern_description,
            "source_event_refs": existing.source_event_refs,
            "confirmed_by": existing.confirmed_by,
            "confirmed_at": existing.confirmed_at,
        }
        if existing.status == "confirmed":
            confirmed_patterns.append(row)
        elif existing.status == "dismissed":
            dismissed_patterns.append(row)

    for cat, events in by_category.items():
        if cat == "unknown":
            continue
        if len(events) < config["min_occurrences"]:
            continue
        existing = existing_by_category.get(cat)
        if existing is not None and existing.status == "confirmed":
            # Already institutional knowledge -- no need to re-surface as a candidate.
            continue
        candidate_patterns.append({
            "category": cat,
            "trigger_description": f"'{cat}' appeared {len(events)} time(s) in the last {config['lookback_days']} days "
            f"(minimum {config['min_occurrences']} required)",
            "pattern_description": f"{cat.replace('_', ' ').capitalize()} is recurring in recent message overrides "
            f"and/or lost meeting outcomes.",
            "occurrence_count": len(events),
            "lookback_days": config["lookback_days"],
            "source_event_refs": events,
            "previously_dismissed": existing is not None and existing.status == "dismissed",
        })

    return {
        "month": month_label,
        "metrics": {
            "messages_rejected": rejected_this_month,
            "messages_changes_requested": changes_requested_this_month,
            "messages_reviewed_with_reason": reviewed_with_reason_this_month,
            "meetings_lost_with_reason": lost_with_reason_this_month,
            "meetings_lost": lost_this_month,
            "meetings_won": won_this_month,
            "outcomes_linked_to_opportunity": attributable_outcomes_this_month,
            "outcomes_total": total_outcomes_this_month,
            "accuracy": accuracy,
        },
        "recent_overrides": recent_overrides,
        "recent_losses": recent_losses,
        "recurring_categories": recurring_categories,
        "candidate_patterns": candidate_patterns,
        "confirmed_patterns": confirmed_patterns,
        "dismissed_patterns": dismissed_patterns,
        "detection_config": config,
        # Part 7 -- human-provided knowledge as another learning input, alongside overrides/
        # outcomes/confirmed patterns. Read-only surface only: this never feeds back into ICP/
        # offering/motion config (see human_knowledge.py's own governance boundary).
        "human_knowledge": list_human_knowledge(db, tenant_id, limit=RECENT_LIMIT),
    }


def _recompute_pattern_evidence(db: Session, tenant_id: int, category: str) -> tuple[str, str, list[dict]]:
    config = get_pattern_detection_config(db, tenant_id)
    lookback_start = datetime.utcnow() - timedelta(days=config["lookback_days"])
    events = []
    for draft in _override_events(db, tenant_id, since=lookback_start):
        if categorize_reason(draft.review_note) == category:
            events.append({"source_type": "message_draft", "source_id": draft.id, "reason": draft.review_note})
    for booking in _loss_events(db, tenant_id, since=lookback_start):
        if categorize_reason(booking.outcome_reason) == category:
            events.append({"source_type": "calendar_booking", "source_id": booking.id, "reason": booking.outcome_reason})

    trigger_description = f"'{category}' appeared {len(events)} time(s) in the last {config['lookback_days']} days"
    pattern_description = f"{category.replace('_', ' ').capitalize()} is recurring in recent message overrides and/or lost meeting outcomes."
    return trigger_description, pattern_description, events


def _get_or_build_pattern_row(db: Session, tenant_id: int, category: str) -> ConfirmedPattern:
    trigger_description, pattern_description, events = _recompute_pattern_evidence(db, tenant_id, category)
    if not events:
        raise ValueError(f"category {category!r} has no real supporting evidence to confirm or dismiss")

    row = (
        db.query(ConfirmedPattern)
        .filter(ConfirmedPattern.tenant_id == tenant_id, ConfirmedPattern.category == category)
        .order_by(ConfirmedPattern.id.desc())
        .first()
    )
    if row is None:
        row = ConfirmedPattern(tenant_id=tenant_id, category=category, status="candidate", source_event_refs=[])
        db.add(row)

    row.trigger_description = trigger_description
    row.pattern_description = pattern_description
    row.source_event_refs = events
    return row


def confirm_pattern(db: Session, tenant_id: int, category: str, confirmed_by: str | None) -> ConfirmedPattern:
    """The ONLY thing this function ever writes is the ConfirmedPattern row itself. It never
    touches ICP config, offering config, GTM motion config, GtmStrategy, MessageDraft, the
    revenue goal, or any autonomous-run behavior -- a confirmed pattern is institutional
    knowledge/readout only in this phase (Part 6/9's explicit boundary)."""
    row = _get_or_build_pattern_row(db, tenant_id, category)
    row.status = "confirmed"
    row.confirmed_by = confirmed_by
    row.confirmed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def dismiss_pattern(db: Session, tenant_id: int, category: str, confirmed_by: str | None) -> ConfirmedPattern:
    """Same write-boundary as confirm_pattern() above -- only ever writes to ConfirmedPattern."""
    row = _get_or_build_pattern_row(db, tenant_id, category)
    row.status = "dismissed"
    row.confirmed_by = confirmed_by
    row.confirmed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row
