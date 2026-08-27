"""Campaign Intelligence -- 2026-08-27, explicit instruction: track the 4 real SalesRobot
campaigns (Playbook, Sales OS, Fractional VP Sales, Workshop) "like a CEO" -- not a fixed
rule-based checklist ("the agent should think"), but genuine reasoning across real live numbers,
grounded so it can never invent a fact. End goal, per explicit instruction: revenue, conversions,
meetings -- not raw send volume.

Two layers, same discipline as icp_ai_matching.py's evaluate_icp_fit_holistic() (the one other
place in this codebase an LLM judges real facts holistically):

1. get_campaign_tracking() -- pure, real, deterministic, ZERO LLM cost. Pulls LIVE per-campaign
   stats directly from SalesRobot's own API (sent/accepted/replied) -- the exact same real numbers
   V1's own Campaign tab (Outcomes.jsx, GET /salesrobot/campaigns) already shows, reusing that
   route's own account-scoping/allowlist logic rather than re-implementing it -- plus real
   deal/revenue data (CalendarBooking.outcome_*) for whichever offering each campaign maps to.

2. generate_campaign_intelligence() -- an LLM reasons over ONLY those real numbers to compare
   campaigns and prioritize toward revenue/conversions/meetings. Every campaign it cites is
   checked against what was actually given (campaign_uuids_cited, verified membership) -- a
   response citing a campaign never actually provided is discarded, never trusted blind, same
   verification icp_ai_matching.py already uses for facts_cited."""

from sqlalchemy.orm import Session

from app.db.models import Batch, CalendarBooking, Company
from app.gtm_os.opportunity.offering_config import get_offering_config
from app.llm_client import generate_json

# Real minimum sample size before accept/reply rate is treated as meaningful -- same discipline
# and same threshold as offering_performance.py's MIN_SAMPLE_FOR_COMPARISON.
MIN_SAMPLE_FOR_RATE = 5


def get_campaign_tracking(db: Session, tenant_id: int) -> dict:
    """Real, live per-campaign numbers. Never estimated: if SalesRobot's API can't be reached,
    reports that honestly rather than returning fabricated/zeroed data."""
    from app.routes.api import _get_our_campaign_uuids, _get_salesrobot_linkedin_account_uuid
    from app.salesrobot_client import SalesRobotError, list_campaigns
    from fastapi import HTTPException

    try:
        account_uuid = _get_salesrobot_linkedin_account_uuid(db)
        our_uuids = set(_get_our_campaign_uuids(db))
        result = list_campaigns(account_uuid, db, tenant_id)
    except (SalesRobotError, HTTPException) as e:
        return {"status": "unavailable", "reason": str(e), "campaigns": []}

    uuid_to_offering: dict[str, str] = {}
    for offering in get_offering_config(db, tenant_id):
        campaign_uuid = (offering.get("campaign_ids") or {}).get("salesrobot")
        if campaign_uuid:
            uuid_to_offering[campaign_uuid] = offering["name"]

    campaigns = []
    for c in result.get("data", {}).get("data", []):
        uuid = c.get("uuid")
        if uuid not in our_uuids:
            continue
        sent = c.get("connectionRequestSentCount") or 0
        accepted = c.get("connectionRequestAcceptedCount") or 0
        replied = c.get("repliedCount") or 0
        campaigns.append({
            "campaign_uuid": uuid,
            "campaign_name": c.get("name"),
            "offering_name": uuid_to_offering.get(uuid),
            "sent": sent,
            "accepted": accepted,
            "replied": replied,
            "accept_rate": round(accepted / sent, 3) if sent >= MIN_SAMPLE_FOR_RATE else None,
            "reply_rate": round(replied / sent, 3) if sent >= MIN_SAMPLE_FOR_RATE else None,
            "deals_won": 0,
            "deals_lost": 0,
            "revenue_won_usd": 0.0,
            "meetings_with_recorded_outcome": 0,
        })

    # Real deal/revenue data per offering -- same tenant-scoping chain as revenue_pace.py/
    # offering_performance.py (CalendarBooking has no tenant_id of its own).
    for entry in campaigns:
        if not entry["offering_name"]:
            continue
        bookings = (
            db.query(CalendarBooking)
            .join(Company, CalendarBooking.outcome_company_id == Company.id)
            .join(Batch, Company.batch_id == Batch.id)
            .filter(Batch.tenant_id == tenant_id, CalendarBooking.outcome_offering_name == entry["offering_name"])
            .all()
        )
        entry["meetings_with_recorded_outcome"] = len(bookings)
        entry["deals_won"] = sum(1 for b in bookings if b.outcome_status == "won")
        entry["deals_lost"] = sum(1 for b in bookings if b.outcome_status == "lost")
        entry["revenue_won_usd"] = sum(float(b.outcome_amount_usd or 0) for b in bookings if b.outcome_status == "won")

    return {"status": "ok", "campaigns": campaigns}


CAMPAIGN_INTELLIGENCE_PROMPT = """You are acting as the GTM lead reviewing real outbound campaign \
performance across a company's live SalesRobot campaigns. Prioritize REVENUE, real CONVERSIONS \
(replies that turn into meetings/deals), and MEETINGS BOOKED -- never raw send volume alone.

Real, live data for every real campaign currently running (nothing below is estimated or invented \
-- if a number isn't listed, it isn't known):
{campaigns_block}

Compare these campaigns and reason genuinely about what's actually working toward revenue, what \
needs attention, and why -- based ONLY on the real numbers above. Do NOT invent any number, deal, \
campaign, or reason not grounded in the data above. If a campaign's sample size is too small to \
draw a real conclusion, say so explicitly rather than judging it anyway.

Return JSON exactly:
{{"campaign_uuids_cited": ["<uuid of every campaign you actually reasoned about>", ...], \
"diagnosis": "<2-4 sentences of real, grounded reasoning across the campaigns above, citing \
specific real numbers>", "priority_recommendation": "<1-2 sentences: what to actually do next, \
prioritized toward revenue/conversions/meetings>"}}"""


def generate_campaign_intelligence(db: Session, tenant_id: int, tracking: dict) -> dict:
    """The genuine-reasoning layer, additive to get_campaign_tracking()'s real numbers -- never
    replaces them. See module docstring for the grounding/verification discipline."""
    campaigns = tracking.get("campaigns") or []
    if tracking.get("status") != "ok" or not campaigns:
        return {"status": "insufficient_data", "diagnosis": None, "priority_recommendation": None}

    campaigns_block = "\n".join(
        f"- {c['offering_name'] or c['campaign_name'] or c['campaign_uuid']} (uuid={c['campaign_uuid']}): "
        f"sent={c['sent']}, accepted={c['accepted']}, replied={c['replied']}, "
        f"accept_rate={c['accept_rate']}, reply_rate={c['reply_rate']}, "
        f"deals_won={c['deals_won']}, deals_lost={c['deals_lost']}, revenue_won_usd={c['revenue_won_usd']}, "
        f"meetings_with_recorded_outcome={c['meetings_with_recorded_outcome']}"
        for c in campaigns
    )
    valid_uuids = {c["campaign_uuid"] for c in campaigns}
    prompt = CAMPAIGN_INTELLIGENCE_PROMPT.format(campaigns_block=campaigns_block)

    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=400)
    except Exception as e:  # noqa: BLE001 -- an LLM outage must never crash the caller
        return {"status": "llm_unavailable", "error": str(e), "diagnosis": None, "priority_recommendation": None}

    cited = response.get("campaign_uuids_cited") if isinstance(response, dict) else None
    if not cited or not isinstance(cited, list) or not all(u in valid_uuids for u in cited):
        return {"status": "discarded", "reason": "cited a campaign never actually given in the real data -- discarded, never trusted blind", "diagnosis": None, "priority_recommendation": None}

    return {
        "status": "ok",
        "campaign_uuids_cited": cited,
        "diagnosis": response.get("diagnosis"),
        "priority_recommendation": response.get("priority_recommendation"),
    }
