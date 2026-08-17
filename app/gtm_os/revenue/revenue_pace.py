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

from app.db.models import Batch, CalendarBooking, Company
from app.gtm_os.context.business_context import get_business_context
from app.gtm_os.icp.icp_config import get_icp_config
from app.gtm_os.icp.icp_matching import ICPMatch
from app.gtm_os.opportunity.offering_config import get_offering_config
from app.gtm_os.opportunity.opportunity import Opportunity

VALID_OUTCOME_STATUSES = {"won", "lost", None}


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
) -> CalendarBooking:
    booking = db.get(CalendarBooking, booking_id)
    if booking is None:
        raise LookupError(f"Booking {booking_id} not found")

    if status not in VALID_OUTCOME_STATUSES:
        raise ValueError(f"status must be 'won', 'lost', or null, got {status!r}")

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

    if status is None:
        # Reset -- clears everything derived from a prior recording, same as un-setting it.
        booking.outcome_opportunity_id = None
        booking.outcome_icp_snapshot = None
        booking.outcome_recorded_at = None
        booking.outcome_recorded_by = None
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


def get_revenue_pace(db: Session, tenant_id: int, month: str | None = None) -> dict:
    now = datetime.utcnow()
    if month:
        year, mon = (int(p) for p in month.split("-"))
    else:
        year, mon = now.year, now.month
    period_start = datetime(year, mon, 1)
    period_end = datetime(year + (1 if mon == 12 else 0), 1 if mon == 12 else mon + 1, 1)

    context = get_business_context(db, tenant_id)
    revenue_goal = (context.get("goals") or {}).get("revenue_goal")
    target = revenue_goal if isinstance(revenue_goal, (int, float)) and not isinstance(revenue_goal, bool) else None

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
