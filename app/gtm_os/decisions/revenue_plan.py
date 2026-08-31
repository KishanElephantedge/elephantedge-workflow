"""The decision layer -- reads the evidence and says what to do about the revenue target.

Everything before this module MEASURES: revenue_pace (target/gap/pacing), offering_performance,
channel_intelligence, governance. Nothing read those numbers and decided anything, which is why
the system could be simultaneously "working" and going nowhere -- it optimised for opportunities
and sends because revenue was invisible to it.

TWO STAGES, DELIBERATELY SEPARATE:

1. build_revenue_diagnosis() -- pure, deterministic, no LLM. Computes the gap, the required
   run-rate, and the CONSTRAINTS actually visible in the data (a stage producing nothing, an
   offering with no campaign, a message variant carrying every reply). Every entry cites the real
   number it came from. This alone is usable and can never hallucinate.

2. generate_revenue_plan() -- asks an LLM to turn that diagnosis into prioritised actions, and
   is allowed to see ONLY the diagnosis. It cannot invent a metric, because it is never given raw
   data to invent from, and every action it returns is checked back against the diagnosis before
   being returned (see _verify_actions).

WHY NOT ONE LLM CALL OVER EVERYTHING: a model handed the whole database will produce confident,
plausible, unverifiable claims -- exactly what this codebase refuses to do everywhere else (see
governance.py, which is 100% structured counts and zero LLM). The facts are computed; only the
prioritisation and phrasing are generated.

HONEST LIMIT, STATED RATHER THAN HIDDEN: this cannot promise the target will be hit. It can say
what the gap is, what is structurally blocking throughput, and what the evidence supports doing
next. Where the evidence is too thin to support a recommendation, it says so instead of inventing
one -- a confident plan built on 5 replies would be a story, not a strategy.
"""

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.gtm_os.learning.channel_intelligence import get_channel_performance
from app.gtm_os.learning.offering_performance import get_offering_performance
from app.gtm_os.revenue.revenue_pace import get_revenue_pace
from app.llm_client import generate_json

logger = logging.getLogger(__name__)


def _funnel_counts(db: Session, tenant_id: int) -> dict:
    """Real current funnel volumes -- the throughput side of the equation. Revenue cannot exceed
    what the top of the funnel physically produces, so a revenue plan that ignores these is
    arithmetic about a pipeline that does not exist."""
    from app.gtm_os.learning.message_draft import MessageDraft
    from app.gtm_os.opportunity.opportunity import Opportunity
    from app.gtm_os.send.send_state import MessageSendAttempt

    return {
        "opportunities_total": db.query(Opportunity).filter(Opportunity.tenant_id == tenant_id).count(),
        "drafts_awaiting_review": db.query(MessageDraft).filter(
            MessageDraft.tenant_id == tenant_id, MessageDraft.status == "ready_for_review").count(),
        "drafts_approved": db.query(MessageDraft).filter(
            MessageDraft.tenant_id == tenant_id, MessageDraft.status == "approved").count(),
        "sends_attempted": db.query(MessageSendAttempt).filter(MessageSendAttempt.tenant_id == tenant_id).count(),
        "sends_succeeded": db.query(MessageSendAttempt).filter(
            MessageSendAttempt.tenant_id == tenant_id,
            MessageSendAttempt.status.in_(("enrolled", "succeeded", "request_submitted"))).count(),
    }


def _outcome_counts(db: Session, tenant_id: int) -> dict:
    from app.gtm_os.learning.outcome import SalesOutcome

    rows = db.query(SalesOutcome.outcome_category).filter(SalesOutcome.tenant_id == tenant_id).all()
    counts: dict[str, int] = {}
    for (category,) in rows:
        counts[category] = counts.get(category, 0) + 1
    return counts


def _reply_variant_concentration(db: Session, tenant_id: int) -> dict | None:
    """Which message variant actually earned the replies. The most directly actionable fact the
    system currently holds -- captured on the signal by sensing.py, never inferred."""
    from app.gtm_os.intelligence.signal import GtmSignal

    variants: dict[str, int] = {}
    for (info,) in db.query(GtmSignal.extracted_info).filter(
        GtmSignal.tenant_id == tenant_id, GtmSignal.source == "linkedin_reply"
    ).all():
        variant = (info or {}).get("replied_variant")
        if variant:
            variants[variant] = variants.get(variant, 0) + 1
    if not variants:
        return None
    total = sum(variants.values())
    top, top_count = max(variants.items(), key=lambda kv: kv[1])
    return {"by_variant": variants, "total_replies": total, "top_variant": top, "top_share": round(top_count / total, 3)}


def build_revenue_diagnosis(db: Session, tenant_id: int, now: datetime | None = None) -> dict:
    """Deterministic. No LLM. Every number here is read from real tables."""
    now = now or datetime.utcnow()
    pace = get_revenue_pace(db, tenant_id)
    funnel = _funnel_counts(db, tenant_id)
    outcomes = _outcome_counts(db, tenant_id)
    offering = get_offering_performance(db, tenant_id)
    channel = get_channel_performance(db, tenant_id)
    variants = _reply_variant_concentration(db, tenant_id)

    days_remaining = pace.get("days_remaining")
    gap = pace.get("ytd_gap_usd")
    required_per_day = round(gap / days_remaining, 2) if (gap and days_remaining) else None

    constraints: list[dict] = []

    # The single most important one: with no closed-won amount anywhere, revenue is unmeasurable,
    # so NO plan below it can be evaluated. Stated first for that reason, not for drama.
    if not pace.get("ytd_actual_usd"):
        constraints.append({
            "constraint": "no_revenue_recorded",
            "evidence": f"ytd_actual_usd={pace.get('ytd_actual_usd')}, won_count={pace.get('won_count')}",
            "why_it_matters": "Revenue only enters the system when a meeting is marked won/lost with an amount. Until one is, every downstream number here is throughput, not revenue, and progress toward the target cannot be measured at all.",
        })

    if not outcomes.get("meeting_requested") and not pace.get("meetings_this_month"):
        constraints.append({
            "constraint": "no_meetings_booked",
            "evidence": f"meetings_this_month={pace.get('meetings_this_month')}, meeting_requested_outcomes={outcomes.get('meeting_requested', 0)}",
            "why_it_matters": "Revenue requires meetings. The funnel currently converts outreach into replies but not into booked meetings.",
        })

    if funnel["sends_succeeded"] < 10:
        constraints.append({
            "constraint": "low_outbound_volume",
            "evidence": f"sends_succeeded={funnel['sends_succeeded']} against a configured target of 10/day",
            "why_it_matters": "Throughput caps revenue arithmetically: at this volume the top of the funnel cannot produce enough meetings to close the gap regardless of conversion quality.",
        })

    for name, stats in (offering.get("by_offering") or {}).items():
        if stats.get("failed_pushes") and not stats.get("pushed"):
            constraints.append({
                "constraint": "offering_not_dispatchable",
                "evidence": f"offering {name!r}: pushed={stats.get('pushed')}, failed_pushes={stats.get('failed_pushes')}",
                "why_it_matters": f"Opportunities matched to {name} cannot be actioned -- no campaign is configured for it, so that demand is lost rather than worked.",
            })

    if variants and variants["top_share"] >= 0.8 and variants["total_replies"] >= 3:
        constraints.append({
            "constraint": "reply_concentration_in_one_variant",
            "evidence": f"{variants['by_variant']} -- {variants['top_share']:.0%} of {variants['total_replies']} replies came from {variants['top_variant']!r}",
            "why_it_matters": "One message variant is carrying the response rate. The others are consuming send volume without producing replies.",
        })

    return {
        "generated_at": now.isoformat(),
        "target": {
            "annual_target_usd": pace.get("annual_target_usd"),
            "ytd_actual_usd": pace.get("ytd_actual_usd"),
            "expected_by_now_usd": pace.get("expected_by_now_usd"),
            "gap_usd": gap,
            "days_remaining": days_remaining,
            "required_usd_per_day": required_per_day,
            "pace_status": pace.get("pace_status"),
        },
        "funnel": funnel,
        "outcomes": outcomes,
        "reply_variants": variants,
        "offering_performance": offering.get("by_offering"),
        "channel_performance": channel.get("by_channel"),
        "constraints": constraints,
        # Named explicitly so a reader can weigh the plan: 5 replies and 0 closed deals is not
        # enough to conclude what WINS, only enough to see what is blocked.
        "evidence_strength": {
            "closed_deals": (pace.get("won_count") or 0) + (pace.get("lost_count") or 0),
            "replies": sum(v for k, v in outcomes.items() if k in ("reply", "positive_reply", "meeting_requested")),
            "sufficient_for_win_loss_learning": ((pace.get("won_count") or 0) + (pace.get("lost_count") or 0)) >= 3,
        },
    }


PLAN_PROMPT = """You are the operating brain of a B2B GTM system with ONE goal: close the revenue \
gap below within the days remaining.

You are given a DIAGNOSIS computed from the company's real database. Every number in it is real.

DIAGNOSIS:
{diagnosis}

Produce a prioritised action plan. Rules you must follow:
- Use ONLY facts present in the diagnosis. Never introduce a metric, rate, or number that is not \
there. If you need a number that is absent, say the number is unavailable instead of estimating it.
- Order actions by how much they move revenue, hardest constraint first.
- Every action must name the constraint or evidence it addresses, quoting the diagnosis.
- Be specific and operational ("raise X to N", "stop doing Y", "record Z"), never generic advice \
like "improve messaging".
- If the evidence is too thin to justify an action, say so rather than inventing a recommendation. \
evidence_strength tells you how much weight the data can bear.
- Distinguish what the SYSTEM can do automatically from what needs a HUMAN.

Return JSON exactly:
{{"assessment": "<2-3 sentences: where we stand against the target and why>",
  "actions": [{{"action": "<specific action>", "owner": "system" | "human",
                "addresses": "<constraint name or evidence quoted from the diagnosis>",
                "expected_effect": "<what changes if done>",
                "confidence": "high" | "medium" | "low"}}],
  "not_recommended_yet": [{{"idea": "<action deliberately NOT recommended>", "why": "<what evidence is missing>"}}]}}"""


def _verify_actions(plan: dict, diagnosis: dict) -> dict:
    """Drops any action whose `addresses` cites nothing recognisable from the diagnosis.

    Same fabrication-guard pattern the content and campaign layers already use: the model is
    allowed to prioritise and phrase, never to introduce evidence. An action grounded in nothing
    is the exact failure mode that makes a plan look authoritative and be wrong."""
    known = {c["constraint"] for c in diagnosis.get("constraints", [])}
    known_text = json.dumps(diagnosis).lower()
    verified, dropped = [], []
    for action in plan.get("actions") or []:
        addresses = str(action.get("addresses") or "").lower()
        if any(k.lower() in addresses for k in known) or (addresses and addresses[:40] in known_text):
            verified.append(action)
        else:
            dropped.append(action)
    if dropped:
        logger.warning("revenue_plan: dropped %d ungrounded action(s): %s", len(dropped), [d.get("action") for d in dropped])
    plan["actions"] = verified
    plan["dropped_ungrounded_actions"] = len(dropped)
    return plan


def generate_revenue_plan(db: Session, tenant_id: int) -> dict:
    """Diagnosis + a verified, prioritised plan. Never raises -- a model failure returns the
    deterministic diagnosis on its own, which is still useful, rather than nothing."""
    diagnosis = build_revenue_diagnosis(db, tenant_id)
    try:
        plan = generate_json(PLAN_PROMPT.format(diagnosis=json.dumps(diagnosis, indent=2, default=str)), db, tenant_id, max_tokens=2000)
    except Exception as e:  # noqa: BLE001 -- the diagnosis alone is a real, usable result
        logger.exception("revenue_plan: plan generation failed")
        return {"diagnosis": diagnosis, "plan": None, "error": str(e)}
    return {"diagnosis": diagnosis, "plan": _verify_actions(plan, diagnosis)}
