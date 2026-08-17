"""Revenue Pace Diagnosis -- a read-only composition layer over Revenue Pace + three other
already-real readers. This module computes NOTHING that isn't already computed elsewhere; it
only reuses and cross-references:

    get_revenue_pace()        -- app/gtm_os/revenue/revenue_pace.py (the source of truth for
                                  target/actual/gap/by-ICP/by-offering; UNCHANGED here)
    get_jobs_to_be_done()      -- app/gtm_os/jobs/jobs_to_be_done.py ("deal_needs_you" =
                                  execution_readiness.py's requires_human_review, unmodified;
                                  "contacts_to_find" = the same real decision-maker-search state)
    get_pipeline_item()        -- app/gtm_os/execution/execution_readiness.py (per-opportunity
                                  strategy/offering context for each blocked opportunity)
    get_overrides_evals()      -- app/gtm_os/learning/overrides_evals.py (confirmed patterns only
                                  -- candidate patterns are explicitly NOT treated as knowledge)

No LLM call anywhere in this file. diagnosis_summary is built from a fixed set of template
clauses, each included ONLY when the real data behind it exists -- never a placeholder, never
invented. primary_constraint is chosen by a fixed, ordered rule list (documented on
_PRIMARY_CONSTRAINT_RULES below), never "the largest number.\""""

from datetime import datetime

from sqlalchemy.orm import Session

from app.gtm_os.execution.execution_readiness import get_pipeline_item
from app.gtm_os.jobs.jobs_to_be_done import get_jobs_to_be_done
from app.gtm_os.learning.overrides_evals import get_overrides_evals
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.revenue.revenue_pace import get_revenue_pace


def _pace_status(pace: dict) -> dict:
    """Deterministic label from Revenue Pace's own already-computed numbers -- no new
    calculation, no forecast. Also computes days-elapsed-in-month, which IS defensible (pure
    calendar arithmetic, not a prediction) and gives a real reference point for "on pace" beyond
    just the raw dollar gap."""
    if not pace["target_configured"]:
        return {"status": "no_target", "days_elapsed_percent": None, "ahead_of_time_pace": None}

    now = datetime.utcnow()
    year, month = (int(p) for p in pace["month"].split("-"))
    period_start = datetime(year, month, 1)
    period_end = datetime(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
    total_days = (period_end - period_start).days
    elapsed_days = min((min(now, period_end) - period_start).days, total_days)
    days_elapsed_percent = round(elapsed_days / total_days * 100, 1) if total_days else None

    if pace["actual_usd"] >= pace["target_usd"]:
        status = "at_or_ahead_of_target"
    elif days_elapsed_percent is not None and pace["pace_percent"] is not None and pace["pace_percent"] >= days_elapsed_percent:
        status = "behind_target_but_ahead_of_time_pace"
    else:
        status = "behind_target_and_behind_time_pace"

    ahead_of_time_pace = (
        pace["pace_percent"] is not None and days_elapsed_percent is not None and pace["pace_percent"] >= days_elapsed_percent
    )
    return {"status": status, "days_elapsed_percent": days_elapsed_percent, "ahead_of_time_pace": ahead_of_time_pace}


def _strongest(rows: list[dict]) -> dict:
    if not rows:
        return {"items": [], "note": "No recorded revenue outcomes yet this month."}
    top = rows[0]
    note = None
    if top["deal_count"] == 1:
        note = f"Based on a single recorded deal -- not yet enough data to call this a reliable leader."
    return {"items": rows, "note": note}


def _blocked_opportunities(db: Session, tenant_id: int, jtbd: dict) -> list[dict]:
    """Reuses Jobs to Be Done's own "deal_needs_you" category (itself execution_readiness.py's
    requires_human_review, unmodified) -- never a second definition of "blocked". Enriches each
    with strategy/offering context via get_pipeline_item(), the same reader Opportunity Detail
    already uses."""
    items = [i for i in jtbd["items"] if i["category"] == "deal_needs_you"]
    enriched = []
    for item in items:
        pipeline_item = get_pipeline_item(db, tenant_id, item["opportunity_id"]) if item["opportunity_id"] else None
        enriched.append({
            "company_id": item["company_id"],
            "company_name": item["company_name"],
            "opportunity_id": item["opportunity_id"],
            "execution_status": pipeline_item["execution"]["status"] if pipeline_item else None,
            "blocker": item["description"],
            "next_action": pipeline_item["execution"]["action"] if pipeline_item else None,
            "reason": item["reason"],
            "strategy_type": pipeline_item["strategy"]["strategy_type"] if pipeline_item and pipeline_item["strategy"] else None,
            "matched_offering_name": pipeline_item["strategy"]["matched_offering_name"] if pipeline_item and pipeline_item["strategy"] else None,
            "action_route": item["action_route"],
        })
    return enriched


# Ordered, documented rule list -- the FIRST rule whose condition is true wins. This is
# deliberately a fixed stage order (goal -> pipeline -> blocked -> upstream data gaps -> outcome
# pattern), not "whichever count is largest" (Part 8's explicit instruction), because a small
# count at an earlier, more foundational stage is usually the real constraint even when a later
# stage has a bigger raw number.
def _primary_constraint(pace: dict, opportunities_total: int, blocked: list[dict], contacts_to_find_count: int) -> dict:
    if not pace["target_configured"]:
        return {"constraint": "no_revenue_goal_configured", "reason": "No numeric revenue target is configured -- pace cannot be diagnosed without one.", "evidence_count": None}
    if opportunities_total == 0:
        return {"constraint": "insufficient_opportunities", "reason": "Zero Opportunities exist for this tenant -- there is nothing in the pipeline to diagnose yet.", "evidence_count": 0}
    if len(blocked) > 0:
        return {"constraint": "opportunities_blocked_on_human_review", "reason": f"{len(blocked)} opportunity(ies) currently require a human action before they can progress.", "evidence_count": len(blocked)}
    if contacts_to_find_count > 0:
        return {"constraint": "missing_decision_makers", "reason": f"{contacts_to_find_count} account(s) have a completed decision-maker search with no usable contact -- this blocks new opportunities from being worked further upstream.", "evidence_count": contacts_to_find_count}
    if pace["won_count"] == 0 and pace["meetings_this_month"] > 0:
        return {"constraint": "meetings_not_yet_producing_recorded_revenue", "reason": f"{pace['meetings_this_month']} meeting(s) took place this month but none have a recorded won outcome yet.", "evidence_count": pace["meetings_this_month"]}
    if pace["gap_usd"] is not None and pace["gap_usd"] > 0:
        return {"constraint": "revenue_gap_no_specific_blocker_identified", "reason": f"A ${pace['gap_usd']:,.0f} gap remains to the target, but no single upstream blocker (opportunities, decision-makers, or meeting-to-outcome conversion) is currently the dominant issue.", "evidence_count": None}
    return {"constraint": None, "reason": "No gap or blocking condition is identified from current evidence -- pace is at or ahead of target with no outstanding blockers.", "evidence_count": None}


def _diagnosis_summary(pace: dict, blocked: list[dict], strongest_offerings: dict, confirmed_pattern_notes: list[str]) -> str:
    """Deterministic template -- NOT LLM-generated. Every clause below is skipped entirely if its
    supporting real data doesn't exist; nothing is ever backfilled with a placeholder."""
    clauses = []
    if pace["target_configured"]:
        if pace["gap_usd"] is not None and pace["gap_usd"] > 0:
            clauses.append(f"You are ${pace['gap_usd']:,.0f} behind the configured ${pace['target_usd']:,.0f} revenue target this month.")
        else:
            clauses.append(f"You are at or ahead of the configured ${pace['target_usd']:,.0f} revenue target this month.")
    else:
        clauses.append("No revenue target is configured yet.")

    if blocked:
        clauses.append(f"{len(blocked)} opportunity(ies) currently require human action.")

    if strongest_offerings["items"]:
        top = strongest_offerings["items"][0]
        clauses.append(f"{top['offering_name']} has generated the most recorded revenue this month (${top['amount_usd']:,.0f}).")

    clauses.extend(confirmed_pattern_notes)

    return " ".join(clauses)


def get_revenue_pace_diagnosis(db: Session, tenant_id: int, month: str | None = None) -> dict:
    """Pure read-only composition. Calls no write function anywhere in its dependency chain."""
    pace = get_revenue_pace(db, tenant_id, month=month)
    jtbd = get_jobs_to_be_done(db, tenant_id)
    overrides_evals = get_overrides_evals(db, tenant_id, month=month)

    opportunities_total = db.query(Opportunity).filter(Opportunity.tenant_id == tenant_id).count()
    contacts_to_find_count = jtbd["category_status"]["contacts_to_find"]["count"]

    blocked = _blocked_opportunities(db, tenant_id, jtbd)
    pace_status = _pace_status(pace)

    strongest_icps = _strongest(pace["by_icp"])
    strongest_offerings = _strongest(pace["by_offering"])

    primary_constraint = _primary_constraint(pace, opportunities_total, blocked, contacts_to_find_count)

    # Part 11 -- confirmed patterns may be SURFACED as context, never treated as a conclusion or
    # used to auto-derive a recommendation. Candidate (unconfirmed) patterns are excluded here on
    # purpose.
    confirmed_patterns = overrides_evals["confirmed_patterns"]
    confirmed_pattern_notes = [f"Confirmed pattern: {p['pattern_description']}" for p in confirmed_patterns]

    diagnosis_summary = _diagnosis_summary(pace, blocked, strongest_offerings, confirmed_pattern_notes)

    # Ordered by real-world proximity to revenue (same staged reasoning as _primary_constraint,
    # not an arbitrary score) -- only included when its underlying evidence count is > 0.
    priorities = []
    if not pace["target_configured"]:
        priorities.append({"title": "Set a revenue target", "reason": "No numeric revenue goal is configured", "source": "business_context", "related_entity": None, "action_route": "/v2/settings?tab=strategy"})
    if opportunities_total == 0:
        priorities.append({"title": "Build pipeline", "reason": "No opportunities exist yet for this tenant", "source": "opportunity", "related_entity": None, "action_route": "/v2/jobs"})
    if blocked:
        priorities.append({"title": "Review blocked opportunities", "reason": f"{len(blocked)} opportunities currently require human action", "source": "execution_readiness", "related_entity": None, "action_route": "/v2/jobs"})
    if contacts_to_find_count > 0:
        priorities.append({"title": "Find missing decision-makers", "reason": f"{contacts_to_find_count} accounts have a completed search with no contact found", "source": "jobs_to_be_done", "related_entity": None, "action_route": "/v2/jobs"})
    if overrides_evals["candidate_patterns"]:
        priorities.append({"title": "Review candidate patterns", "reason": f"{len(overrides_evals['candidate_patterns'])} recurring pattern(s) awaiting confirmation", "source": "overrides_evals", "related_entity": None, "action_route": "/v2/overrides-evals"})

    return {
        "month": pace["month"],
        "revenue_goal": pace["target_usd"],
        "actual_revenue": pace["actual_usd"],
        "gap": pace["gap_usd"],
        "pace_status": pace_status,
        "strongest_icps": strongest_icps,
        "strongest_offerings": strongest_offerings,
        "blocked_opportunities": blocked,
        "pipeline_gaps": {
            "opportunities_total": opportunities_total,
            "contacts_to_find_count": contacts_to_find_count,
        },
        "primary_constraint": primary_constraint,
        "diagnosis_summary": diagnosis_summary,
        "evidence": {
            "target_configured": pace["target_configured"],
            "won_count": pace["won_count"],
            "lost_count": pace["lost_count"],
            "meetings_this_month": pace["meetings_this_month"],
            "pending_count": pace["pending_count"],
            "opportunities_total": opportunities_total,
            "blocked_opportunities_count": len(blocked),
            "contacts_to_find_count": contacts_to_find_count,
            "candidate_patterns_count": len(overrides_evals["candidate_patterns"]),
            "confirmed_patterns_count": len(confirmed_patterns),
        },
        "priorities": priorities,
        "confirmed_patterns_referenced": confirmed_patterns,
    }
