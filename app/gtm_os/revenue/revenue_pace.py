"""Meeting Outcomes / Revenue Pace.

record_meeting_outcome() lets a human record what actually happened after a booked meeting --
won (with an amount), lost (with a reason), or reset back to pending. Every value is stored
exactly as supplied; the one exception is `outcome_icp_snapshot`, captured server-side from the
company's CURRENT ICPMatch rows (Batch 8) at the moment the outcome is recorded -- never
client-supplied, so it reflects real, already-computed ICP fit rather than a guess, and it's a
snapshot (not a live join) so it doesn't silently change later if ICP config is edited.

get_revenue_pace() is a pure read-only aggregation over those recorded outcomes plus
business_context's revenue_goal (app/gtm_os/context/business_context.py). If revenue_goal isn't
a real number, this returns target_configured=False rather than guessing -- there is no other
revenue-target concept anywhere in this backend to fall back on.

Explicitly NOT here: cost-per-meeting, forecasting, any generated narrative/reasoning text, and
any automatic adjustment of ICP/offering/GTM-motion configuration from win/loss patterns -- see
this package's __init__.py for why."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Batch, CalendarBooking, CampaignPush, Company, Contact
from app.gtm_os.context.business_context import get_business_context
from app.gtm_os.icp.icp_config import get_icp_config
from app.gtm_os.icp.icp_matching import ICPMatch
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.opportunity.offering_config import get_offering_config
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.send.send_state import MessageSendAttempt

VALID_OUTCOME_STATUSES = {"won", "lost", None}

# 2026-08-27, explicit instruction -- real revenue-by-channel attribution ("Channels
# Intelligence"). Only "outbound" has a real, automatic detection path (see
# detect_real_outbound_activity() below) -- every other value here is a human self-report,
# recorded the same honest way outcome_reason/outcome_notes already are, because no real
# mechanism exists anywhere in this system to auto-detect a personal-network referral, an
# inbound-website-visit-turned-deal, LinkedIn-content engagement, or a webinar signup (confirmed
# by direct investigation -- see this module's own git history/PR description).
OUTCOME_CHANNELS = {"personal_network", "linkedin_content", "inbound", "webinar", "outbound", "other", None}

# The real, already-existing "a send actually happened" status values across BOTH of this
# system's real outbound mechanisms -- CampaignPush (V1's/manually-tagged-batch pathway) and
# MessageSendAttempt (V2's real automated send path, app/gtm_os/send/send_state.py). Reused
# verbatim, not re-derived: CampaignPush's real values are set in campaign_execution.py;
# MessageSendAttempt's real "sent" value was a confirmed live bug fix (send_state.py's own
# SUCCESS_STATUSES constant is stale/missing it) -- this uses the REAL observed value set, same
# fix already applied in campaign_intelligence.py's REAL_SEND_SUCCESS_STATUSES.
_REAL_CAMPAIGN_PUSH_SUCCESS_STATUSES = {"pushed"}
_REAL_MESSAGE_SEND_SUCCESS_STATUSES = {"enrolled", "sent", "request_submitted"}


def detect_real_outbound_activity(db: Session, tenant_id: int, company_id: int) -> bool:
    """True only if a real outbound send actually reached a real Contact at this Company, via
    either real send mechanism. A suggestion for the human recording an outcome, never a silent
    auto-set -- the human still confirms/overrides via outcome_channel, same discipline as
    opportunity_id's own "human-supplied, never inferred" rule above."""
    contact_ids = [c.id for c in db.query(Contact.id).filter(Contact.company_id == company_id).all()]
    if not contact_ids:
        return False

    has_campaign_push = (
        db.query(CampaignPush.id)
        .filter(CampaignPush.contact_id.in_(contact_ids), CampaignPush.status.in_(_REAL_CAMPAIGN_PUSH_SUCCESS_STATUSES))
        .first()
        is not None
    )
    if has_campaign_push:
        return True

    has_message_send = (
        db.query(MessageSendAttempt.id)
        .join(MessageDraft, MessageSendAttempt.message_draft_id == MessageDraft.id)
        .filter(MessageDraft.tenant_id == tenant_id, MessageSendAttempt.contact_id.in_(contact_ids))
        .filter(MessageSendAttempt.status.in_(_REAL_MESSAGE_SEND_SUCCESS_STATUSES))
        .first()
        is not None
    )
    return has_message_send


def record_meeting_outcome(
    db: Session,
    tenant_id: int,
    booking_id: int,
    status: str | None,
    company_id: int | None,
    offering_name: str | None,
    amount_usd: float | None,
    reason: str | None,
    notes: str | None,
    recorded_by: str | None,
    opportunity_id: int | None = None,
    channel: str | None = None,
) -> CalendarBooking:
    booking = db.get(CalendarBooking, booking_id)
    if booking is None:
        raise LookupError(f"Booking {booking_id} not found")

    if status not in VALID_OUTCOME_STATUSES:
        raise ValueError(f"status must be 'won', 'lost', or null, got {status!r}")

    if channel not in OUTCOME_CHANNELS:
        raise ValueError(f"channel must be one of {sorted(c for c in OUTCOME_CHANNELS if c)}, or null, got {channel!r}")

    company = None
    if company_id is not None:
        company = db.get(Company, company_id)
        if company is None or company.batch.tenant_id != tenant_id:
            raise LookupError(f"Company {company_id} not found")

    if offering_name is not None:
        configured_names = {o["name"] for o in get_offering_config(db, tenant_id)}
        if offering_name not in configured_names:
            raise ValueError(f"offering_name {offering_name!r} is not a configured offering")

    # V2 Overrides & Evals attribution link -- optional, human-supplied, never inferred. Requires
    # a company_id to check against (the opportunity must belong to the SAME real company as the
    # outcome), which mirrors how offering_name is already validated against real configured
    # offerings above -- no silent auto-linking.
    if opportunity_id is not None:
        opportunity = db.get(Opportunity, opportunity_id)
        if opportunity is None or opportunity.tenant_id != tenant_id:
            raise LookupError(f"Opportunity {opportunity_id} not found")
        if company_id is None or opportunity.company_id != company_id:
            raise ValueError(
                f"Opportunity {opportunity_id} belongs to company {opportunity.company_id!r}, "
                f"not the outcome's company {company_id!r} -- an opportunity can only be linked "
                f"to a meeting outcome for the same company"
            )

    booking.outcome_status = status
    booking.outcome_company_id = company_id
    booking.outcome_opportunity_id = opportunity_id
    booking.outcome_offering_name = offering_name
    booking.outcome_amount_usd = amount_usd if status == "won" else None
    booking.outcome_reason = reason if status == "lost" else None
    booking.outcome_notes = notes
    booking.outcome_channel = channel

    if status is None:
        # Reset -- clears everything derived from a prior recording, same as un-setting it.
        booking.outcome_opportunity_id = None
        booking.outcome_icp_snapshot = None
        booking.outcome_recorded_at = None
        booking.outcome_recorded_by = None
        booking.outcome_channel = None
    else:
        icp_snapshot = []
        if company is not None:
            icp_names_by_id = {c["id"]: c["name"] for c in get_icp_config(db, tenant_id)}
            matches = (
                db.query(ICPMatch)
                .filter(ICPMatch.tenant_id == tenant_id, ICPMatch.company_id == company_id)
                .all()
            )
            icp_snapshot = [
                {"icp_id": m.icp_id, "icp_name": icp_names_by_id.get(m.icp_id, m.icp_id)}
                for m in matches
            ]
        booking.outcome_icp_snapshot = icp_snapshot
        booking.outcome_recorded_at = datetime.utcnow()
        booking.outcome_recorded_by = recorded_by

    db.commit()
    db.refresh(booking)
    return booking



def _annual_pace(db: Session, tenant_id: int, annual_goal, period: str, now: datetime) -> dict:
    """Year-to-date position against an annual target: what has actually closed this year, what
    should have closed by today at an even pace, and the gap between them.

    Returns explicit nulls (never zeros) when no target is configured -- a missing target is not
    the same as being $0 behind, and reporting it as 0 would read as "on track"."""
    if annual_goal is None or period == "monthly":
        return {
            "annual_target_usd": annual_goal if period != "monthly" else None,
            "ytd_actual_usd": None, "expected_by_now_usd": None, "ytd_gap_usd": None,
            "pace_status": "no_annual_target_configured",
        }

    year_start = datetime(now.year, 1, 1)
    next_year = datetime(now.year + 1, 1, 1)
    days_in_year = (next_year - year_start).days
    elapsed_days = max((now - year_start).days, 0)
    expected = annual_goal * (elapsed_days / days_in_year)

    won_this_year = (
        db.query(CalendarBooking)
        .join(Company, CalendarBooking.outcome_company_id == Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id)
        .filter(CalendarBooking.outcome_status == "won")
        .filter(CalendarBooking.outcome_recorded_at >= year_start, CalendarBooking.outcome_recorded_at < next_year)
        .all()
    )
    ytd = float(sum(b.outcome_amount_usd or 0 for b in won_this_year))
    gap = expected - ytd
    return {
        "annual_target_usd": annual_goal,
        "ytd_actual_usd": ytd,
        "expected_by_now_usd": round(expected, 2),
        "ytd_gap_usd": round(gap, 2),
        "days_elapsed": elapsed_days,
        "days_remaining": days_in_year - elapsed_days,
        # Categorical, never a fabricated confidence score -- same discipline as governance.py.
        "pace_status": "on_or_ahead_of_pace" if ytd >= expected else "behind_pace",
    }


def get_revenue_pace(db: Session, tenant_id: int, month: str | None = None) -> dict:
    now = datetime.utcnow()
    if month:
        year, mon = (int(p) for p in month.split("-"))
    else:
        year, mon = now.year, now.month
    period_start = datetime(year, mon, 1)
    period_end = datetime(year + (1 if mon == 12 else 0), 1 if mon == 12 else mon + 1, 1)

    context = get_business_context(db, tenant_id)
    goals = context.get("goals") or {}
    revenue_goal = goals.get("revenue_goal")
    annual_goal = revenue_goal if isinstance(revenue_goal, (int, float)) and not isinstance(revenue_goal, bool) else None

    # revenue_goal is the ANNUAL target (2026-08-31). It was previously compared straight against
    # ONE month's closed revenue, which silently read a $1,000,000 yearly goal as "$1,000,000 due
    # this month" -- a target the company would miss by definition every month, making the gap
    # meaningless as a steering signal.
    #
    # goals.revenue_goal_period can override this ("monthly"), but annual is the default because
    # that is how the real target is stated.
    period = (goals.get("revenue_goal_period") or "annual").strip().lower()
    target = annual_goal if period == "monthly" else (annual_goal / 12 if annual_goal is not None else None)

    # Deals closed (recorded) this month -- bucketed by outcome_recorded_at, i.e. when the human
    # actually logged the result, not when the meeting itself took place.
    #
    # CalendarBooking has no tenant_id column of its own (a pre-existing architectural
    # characteristic -- this backend is single-tenant-per-deployment). Scoped here via
    # outcome_company_id -> Company.batch_id -> Batch.tenant_id, the same real chain
    # record_meeting_outcome() already validates a company against, and the same fix applied to
    # overrides_evals.py -- required for Revenue Pace Diagnosis's tenant-isolation guarantee
    # (Part 16). For the real, single-tenant production deployment this changes nothing (every
    # real won/lost outcome already has a real company_id supplied); a won/lost outcome recorded
    # with no company at all is excluded from revenue totals, since there would be no way to know
    # which tenant's revenue it belongs to.
    closed_this_month = (
        db.query(CalendarBooking)
        .join(Company, CalendarBooking.outcome_company_id == Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id)
        .filter(
            CalendarBooking.outcome_status.isnot(None),
            CalendarBooking.outcome_recorded_at >= period_start,
            CalendarBooking.outcome_recorded_at < period_end,
        )
        .all()
    )
    won = [b for b in closed_this_month if b.outcome_status == "won"]
    lost = [b for b in closed_this_month if b.outcome_status == "lost"]
    actual = sum(b.outcome_amount_usd or 0 for b in won)

    # deal_count alongside amount_usd -- additive (Revenue Pace Diagnosis needs a sample size to
    # avoid calling a single won deal "strongest"), the base amount_usd figure is unchanged.
    by_icp: dict[str, dict] = {}
    for b in won:
        names = [e.get("icp_name") for e in (b.outcome_icp_snapshot or []) if e.get("icp_name")] or ["Unmatched"]
        for name in names:
            entry = by_icp.setdefault(name, {"amount_usd": 0, "deal_count": 0})
            entry["amount_usd"] += b.outcome_amount_usd or 0
            entry["deal_count"] += 1

    by_offering: dict[str, dict] = {}
    for b in won:
        name = b.outcome_offering_name or "Unspecified"
        entry = by_offering.setdefault(name, {"amount_usd": 0, "deal_count": 0})
        entry["amount_usd"] += b.outcome_amount_usd or 0
        entry["deal_count"] += 1

    # Meetings that took place this month (bucketed by start_time) -- a separate count from the
    # above, since a meeting can be booked one month and its outcome logged the next. NOT
    # tenant-scoped (a booking with no outcome yet has no company_id to scope by at all) -- a
    # known, pre-existing limitation, harmless in the real single-tenant production deployment.
    meetings_this_month = (
        db.query(CalendarBooking)
        .filter(CalendarBooking.start_time >= period_start, CalendarBooking.start_time < period_end)
        .all()
    )
    pending_count = sum(1 for b in meetings_this_month if b.outcome_status is None)

    return {
        "month": f"{year:04d}-{mon:02d}",
        "target_configured": target is not None,
        # Year-to-date pacing -- the number that actually answers "are we going to make it?".
        # A monthly gap alone cannot: a good month after three bad ones still misses the year.
        # elapsed_fraction is measured in DAYS, not whole months, so the expectation moves every
        # day rather than stepping once a month.
        **_annual_pace(db, tenant_id, annual_goal, period, now),
        "target_usd": target,
        "actual_usd": actual,
        "gap_usd": (target - actual) if target is not None else None,
        "pace_percent": round(actual / target * 100, 1) if target else None,
        "meetings_this_month": len(meetings_this_month),
        "pending_count": pending_count,
        "won_count": len(won),
        "lost_count": len(lost),
        "by_icp": [
            {"icp_name": k, "amount_usd": v["amount_usd"], "deal_count": v["deal_count"]}
            for k, v in sorted(by_icp.items(), key=lambda kv: -kv[1]["amount_usd"])
        ],
        "by_offering": [
            {"offering_name": k, "amount_usd": v["amount_usd"], "deal_count": v["deal_count"]}
            for k, v in sorted(by_offering.items(), key=lambda kv: -kv[1]["amount_usd"])
        ],
    }
