"""Channels Intelligence -- 2026-08-27, explicit instruction: real revenue/deal attribution by
source channel (personal network, LinkedIn content, inbound, webinars, outbound), same "the
agent should think, grounded in real numbers, prioritized toward revenue" discipline as
campaign_intelligence.py.

Two layers, same split as campaign_intelligence.py / icp_ai_matching.py:

1. get_channel_performance() -- pure, real, deterministic, ZERO LLM cost. Aggregates real
   CalendarBooking.outcome_channel rows (see revenue_pace.py's OUTCOME_CHANNELS and
   detect_real_outbound_activity() for why only "outbound" can ever be auto-detected -- every
   other value is a real, honest human self-report, never guessed).

2. generate_channel_intelligence() -- an LLM reasons over ONLY those real numbers to compare
   channels and prioritize toward revenue/conversions/meetings. Every channel it cites is checked
   against what was actually given (channels_cited, verified membership) -- a response citing a
   channel never actually provided is discarded, never trusted blind."""

from sqlalchemy.orm import Session

from app.db.models import Batch, CalendarBooking, Company
from app.gtm_os.revenue.revenue_pace import OUTCOME_CHANNELS
from app.llm_client import generate_json

# Real, human-facing labels -- OUTCOME_CHANNELS itself is the validated value set (revenue_pace.py).
CHANNEL_LABELS = {
    "personal_network": "Personal network",
    "linkedin_content": "LinkedIn content",
    "inbound": "Inbound (website)",
    "webinar": "Webinars",
    "outbound": "Outbound",
    "other": "Other",
}


def get_channel_performance(db: Session, tenant_id: int) -> dict:
    """Real counts per channel: meetings with a recorded outcome, won/lost, and revenue won.
    Every real configured channel appears even at zero activity -- a real zero is still real
    information (same discipline as offering_performance.py). CalendarBooking has no tenant_id of
    its own (confirmed pre-existing characteristic, see revenue_pace.py's own comment on this) --
    scoped via outcome_company_id -> Company.batch_id -> Batch.tenant_id, the same real chain
    revenue_pace.py/offering_performance.py already use."""
    channels = [c for c in OUTCOME_CHANNELS if c]  # drop the None sentinel -- not a real channel to report on
    performance = {c: {"meetings_with_recorded_outcome": 0, "deals_won": 0, "deals_lost": 0, "revenue_won_usd": 0.0} for c in channels}

    bookings = (
        db.query(CalendarBooking)
        .join(Company, CalendarBooking.outcome_company_id == Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id, CalendarBooking.outcome_channel.isnot(None), CalendarBooking.outcome_status.isnot(None))
        .all()
    )

    unattributed_count = (
        db.query(CalendarBooking)
        .join(Company, CalendarBooking.outcome_company_id == Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id, CalendarBooking.outcome_status.isnot(None), CalendarBooking.outcome_channel.is_(None))
        .count()
    )

    for booking in bookings:
        channel = booking.outcome_channel
        if channel not in performance:
            continue  # a channel value from an older/removed config -- real, just not reportable against today's list
        performance[channel]["meetings_with_recorded_outcome"] += 1
        if booking.outcome_status == "won":
            performance[channel]["deals_won"] += 1
            performance[channel]["revenue_won_usd"] += float(booking.outcome_amount_usd or 0)
        elif booking.outcome_status == "lost":
            performance[channel]["deals_lost"] += 1

    return {"by_channel": performance, "unattributed_outcomes_count": unattributed_count}


CHANNEL_INTELLIGENCE_PROMPT = """You are acting as the GTM lead reviewing real revenue outcomes \
by source channel (how each real closed/lost deal's contact actually came to Elephant Edge). \
Prioritize REVENUE and real CONVERSIONS (deals won) -- never raw activity volume alone.

Real, human-recorded outcome data for every real channel (nothing below is estimated or invented \
-- if a number isn't listed, it isn't known):
{channels_block}

{unattributed_note}

Compare these channels and reason genuinely about which is actually producing revenue and which \
isn't, based ONLY on the real numbers above. Do NOT invent any number, deal, or channel not \
grounded in the data above. If a channel's sample size is too small (e.g. 0-1 real outcomes) to \
draw a real conclusion, say so explicitly rather than judging it anyway.

Return JSON exactly:
{{"channels_cited": ["<channel key of every channel you actually reasoned about>", ...], \
"diagnosis": "<2-4 sentences of real, grounded reasoning across the channels above, citing \
specific real numbers>", "priority_recommendation": "<1-2 sentences: what to actually do next, \
prioritized toward revenue>"}}"""


def generate_channel_intelligence(db: Session, tenant_id: int, performance: dict) -> dict:
    """The genuine-reasoning layer, additive to get_channel_performance()'s real numbers -- never
    replaces them. See module docstring for the grounding/verification discipline."""
    by_channel = performance.get("by_channel") or {}
    total_outcomes = sum(c["meetings_with_recorded_outcome"] for c in by_channel.values())
    if total_outcomes == 0:
        return {"status": "insufficient_data", "diagnosis": None, "priority_recommendation": None}

    channels_block = "\n".join(
        f"- {CHANNEL_LABELS.get(key, key)} (key={key}): "
        f"meetings_with_recorded_outcome={stats['meetings_with_recorded_outcome']}, "
        f"deals_won={stats['deals_won']}, deals_lost={stats['deals_lost']}, revenue_won_usd={stats['revenue_won_usd']}"
        for key, stats in by_channel.items()
    )
    unattributed = performance.get("unattributed_outcomes_count", 0)
    unattributed_note = (
        f"Separately, {unattributed} real recorded outcome(s) have no channel attributed at all -- do not attribute them to any channel above, they are simply unknown."
        if unattributed else ""
    )
    valid_channels = set(by_channel.keys())
    prompt = CHANNEL_INTELLIGENCE_PROMPT.format(channels_block=channels_block, unattributed_note=unattributed_note)

    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=max(600, 150 * len(by_channel)))
    except Exception as e:  # noqa: BLE001 -- an LLM outage must never crash the caller
        return {"status": "llm_unavailable", "error": str(e), "diagnosis": None, "priority_recommendation": None}

    cited = response.get("channels_cited") if isinstance(response, dict) else None
    if not cited or not isinstance(cited, list) or not all(c in valid_channels for c in cited):
        return {"status": "discarded", "reason": "cited a channel never actually given in the real data -- discarded, never trusted blind", "diagnosis": None, "priority_recommendation": None}

    return {
        "status": "ok",
        "channels_cited": cited,
        "diagnosis": response.get("diagnosis"),
        "priority_recommendation": response.get("priority_recommendation"),
    }
