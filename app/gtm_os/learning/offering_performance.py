"""Offering Performance -- 2026-08-26, explicit instruction: "the system should keep on track and
learning... how many we have pushed, which is getting better reply, better conversations, which
is getting better sold and which is not, and what to do... the system should itself make better
and suggest for us what to do."

Same discipline as app/gtm_os/learning/evaluation.py and app/gtm_os/governance.py (both
confirmed by reading): pure, read-only observation over real, already-existing data --
NOT ML optimization, NOT automated strategy changes, NOT revenue forecasting, NOT an LLM-
generated narrative. Every count is a real, tenant-scoped query; every "suggestion" is a
plain, rule-based sentence templated from those real counts, never invented reasoning --
same "governance.py has zero LLM calls" precedent, applied here to the follow-up ask.

Reuses the exact SalesOutcome -> Opportunity -> latest GtmStrategy join pattern
evaluate_learning_readout() already established, extended to also bucket
MessageSendAttempt (real send/push attempts) and CalendarBooking's real outcome_* columns
(won/lost deals, confirmed real and populated -- app/gtm_os/revenue/revenue_pace.py) by
offering, via GtmStrategy.matched_offering_name / CalendarBooking.outcome_offering_name."""

from sqlalchemy.orm import Session

from app.db.models import Batch, CalendarBooking, Company
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.learning.outcome import SalesOutcome
from app.gtm_os.opportunity.offering_config import get_offering_config
from app.gtm_os.send.send_state import MessageSendAttempt
from app.gtm_os.strategy.strategy import GtmStrategy

# The real status values app/gtm_os/send/channels.py's adapters actually return (confirmed by
# reading every adapter directly) -- NOT send_state.py's own SUCCESS_STATUSES constant, which
# is missing "sent" (send_via_smtp's real return value) and would silently undercount every
# successful email send. Flagged as its own real bug in send_state.py; not fixed here (out of
# this module's scope), worked around by not reusing that stale constant.
REAL_SEND_SUCCESS_STATUSES = {"enrolled", "sent", "request_submitted"}

# Real minimum sample size before a reply rate is treated as meaningful enough to compare or
# flag -- below this, "0% reply rate" from e.g. 2 pushes is noise, not a real signal. Matches
# evaluation.py/governance.py's own "don't fabricate confidence from insufficient data" rule.
MIN_SAMPLE_FOR_COMPARISON = 5


def _latest_offering_for_opportunity(db: Session, tenant_id: int, opportunity_id: int) -> str | None:
    """Same pattern evaluate_learning_readout() already uses for strategy_type -- the CURRENT
    (highest id) GtmStrategy version for this opportunity, since matched_offering_name can
    change across re-evaluations and only the latest is the real, current match."""
    latest_strategy = (
        db.query(GtmStrategy)
        .filter(GtmStrategy.tenant_id == tenant_id, GtmStrategy.opportunity_id == opportunity_id)
        .order_by(GtmStrategy.id.desc())
        .first()
    )
    return latest_strategy.matched_offering_name if latest_strategy else None


def get_offering_performance(db: Session, tenant_id: int) -> dict:
    """Per-offering real counts: pushed, failed pushes, replies (by category), meetings booked
    with a recorded outcome, deals won/lost, revenue won. Every offering configured today (see
    offering_config.py) appears even at zero activity -- a real zero is still real information,
    never omitted to make the report look busier than it is."""
    offering_names = [o["name"] for o in get_offering_config(db, tenant_id)]
    performance = {
        name: {
            "pushed": 0, "failed_pushes": 0,
            "replies": 0, "positive_replies": 0, "not_interested": 0, "timing_deferrals": 0,
            "meetings_with_recorded_outcome": 0, "deals_won": 0, "deals_lost": 0, "revenue_won_usd": 0.0,
        }
        for name in offering_names
    }

    # ---- Sends (real push attempts) -- MessageSendAttempt -> MessageDraft.gtm_strategy_id -> GtmStrategy.matched_offering_name.
    attempts = (
        db.query(MessageSendAttempt.status, MessageDraft.gtm_strategy_id)
        .join(MessageDraft, MessageSendAttempt.message_draft_id == MessageDraft.id)
        .filter(MessageSendAttempt.tenant_id == tenant_id)
        .all()
    )
    strategy_ids = {gtm_strategy_id for _, gtm_strategy_id in attempts}
    offering_by_strategy_id: dict[int, str | None] = {}
    if strategy_ids:
        for strategy in db.query(GtmStrategy).filter(GtmStrategy.id.in_(strategy_ids)).all():
            offering_by_strategy_id[strategy.id] = strategy.matched_offering_name
    for status, gtm_strategy_id in attempts:
        offering_name = offering_by_strategy_id.get(gtm_strategy_id)
        if offering_name not in performance:
            continue  # strategy matched an offering not in the current config (renamed/removed) -- real, just not reportable against today's list
        if status in REAL_SEND_SUCCESS_STATUSES:
            performance[offering_name]["pushed"] += 1
        elif status == "failed":
            performance[offering_name]["failed_pushes"] += 1

    # ---- Replies (SalesOutcome, same join pattern as evaluate_learning_readout()).
    outcomes = (
        db.query(SalesOutcome)
        .filter(SalesOutcome.tenant_id == tenant_id, SalesOutcome.opportunity_id.isnot(None))
        .all()
    )
    offering_by_opportunity_id: dict[int, str | None] = {}
    for outcome in outcomes:
        if outcome.opportunity_id not in offering_by_opportunity_id:
            offering_by_opportunity_id[outcome.opportunity_id] = _latest_offering_for_opportunity(db, tenant_id, outcome.opportunity_id)
        offering_name = offering_by_opportunity_id[outcome.opportunity_id]
        if offering_name not in performance:
            continue
        if outcome.outcome_category == "reply":
            performance[offering_name]["replies"] += 1
        elif outcome.outcome_category == "positive_reply":
            performance[offering_name]["positive_replies"] += 1
        elif outcome.outcome_category == "not_interested":
            performance[offering_name]["not_interested"] += 1
        elif outcome.outcome_category == "timing_deferral":
            performance[offering_name]["timing_deferrals"] += 1

    # ---- Meetings/deals -- CalendarBooking's own real outcome_offering_name (recorded directly
    # by a human, see app/gtm_os/revenue/revenue_pace.py's record_meeting_outcome()). CalendarBooking
    # has no tenant_id of its own (confirmed pre-existing characteristic, see revenue_pace.py's
    # own comment on this) -- scoped the SAME way revenue_pace.py already does, via
    # outcome_company_id -> Company.batch_id -> Batch.tenant_id. No join needed/possible for a
    # booking whose outcome hasn't been recorded yet, which is why "meetings_with_recorded_outcome"
    # is named for exactly what it measures, not "meetings booked" -- an unattributed booking is
    # real but cannot be credited to an offering OR a tenant.
    bookings = (
        db.query(CalendarBooking)
        .join(Company, CalendarBooking.outcome_company_id == Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id, CalendarBooking.outcome_offering_name.isnot(None))
        .all()
    )
    unattributed_bookings = (
        db.query(CalendarBooking)
        .join(Company, CalendarBooking.outcome_company_id == Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id, CalendarBooking.outcome_status.is_(None))
        .count()
    )
    for booking in bookings:
        offering_name = booking.outcome_offering_name
        if offering_name not in performance:
            continue
        performance[offering_name]["meetings_with_recorded_outcome"] += 1
        if booking.outcome_status == "won":
            performance[offering_name]["deals_won"] += 1
            performance[offering_name]["revenue_won_usd"] += float(booking.outcome_amount_usd or 0)
        elif booking.outcome_status == "lost":
            performance[offering_name]["deals_lost"] += 1

    # ---- Reply rate -- only computed with enough real pushes to mean anything (see
    # MIN_SAMPLE_FOR_COMPARISON); otherwise explicitly None rather than a misleading 0%/100%.
    for name, stats in performance.items():
        stats["reply_rate"] = round(stats["replies"] / stats["pushed"], 3) if stats["pushed"] >= MIN_SAMPLE_FOR_COMPARISON else None

    return {"by_offering": performance, "unattributed_bookings_count": unattributed_bookings}


def generate_offering_suggestions(performance: dict) -> list[str]:
    """Plain, rule-based sentences templated from get_offering_performance()'s own real counts --
    never an LLM call, never invented reasoning (see module docstring). Every sentence names the
    real number(s) it's based on, so it can be checked against the source data directly."""
    by_offering = performance["by_offering"]
    suggestions: list[str] = []

    comparable = {name: stats for name, stats in by_offering.items() if stats["reply_rate"] is not None}

    never_pushed = [name for name, stats in by_offering.items() if stats["pushed"] == 0]
    if never_pushed:
        suggestions.append(f"Not yet pushed to any contacts: {', '.join(sorted(never_pushed))}.")

    insufficient = [name for name, stats in by_offering.items() if 0 < stats["pushed"] < MIN_SAMPLE_FOR_COMPARISON]
    if insufficient:
        insufficient_desc = ", ".join(f"{n} ({by_offering[n]['pushed']} pushed)" for n in sorted(insufficient))
        suggestions.append(
            f"Not enough real sends yet to judge reply rate for: {insufficient_desc} "
            f"(need at least {MIN_SAMPLE_FOR_COMPARISON})."
        )

    zero_reply = [name for name, stats in comparable.items() if stats["replies"] == 0]
    if zero_reply:
        for name in sorted(zero_reply):
            suggestions.append(f"{name}: 0 replies from {by_offering[name]['pushed']} real pushes -- worth reviewing messaging or targeting for this offering.")

    if comparable:
        best_name = max(comparable, key=lambda n: comparable[n]["reply_rate"])
        best = comparable[best_name]
        if best["reply_rate"] > 0:
            suggestions.append(f"{best_name} has the highest real reply rate ({best['reply_rate']:.1%}, {best['replies']}/{best['pushed']}) -- consider prioritizing contacts/budget toward it.")

    sold = {name: stats for name, stats in by_offering.items() if stats["deals_won"] > 0}
    for name in sorted(sold):
        stats = sold[name]
        suggestions.append(f"{name} has {stats['deals_won']} real deal(s) won worth ${stats['revenue_won_usd']:,.0f} -- proven to convert, not just reply.")

    lost_only = {name for name, stats in by_offering.items() if stats["deals_lost"] > 0 and stats["deals_won"] == 0}
    for name in sorted(lost_only):
        suggestions.append(f"{name} has {by_offering[name]['deals_lost']} real deal(s) lost and 0 won so far.")

    if performance["unattributed_bookings_count"] > 0:
        suggestions.append(f"{performance['unattributed_bookings_count']} booked meeting(s) have no recorded offering/outcome yet -- record their outcome to make this report count them.")

    if not suggestions:
        suggestions.append("Not enough real activity yet across any offering to suggest anything -- check back once contacts have actually been pushed.")

    return suggestions
