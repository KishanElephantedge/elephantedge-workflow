"""Content Strategy Copilot (2026-08-28) -- the original vision from progress-log.md's own
"content strategy copilot" proposal: a chat surface with two modes, "suggest me topics" and
"write this specific topic," grounded in real evidence, plus the ability to explain WHY. Persona:
a senior content strategist/architect mentoring a junior content writer -- reviews real evidence,
suggests topics with reasoning, drafts for a specific real platform on request, always grounded.

Same tool-calling mechanism as v2_chat_tools.py (Claude's native tool_use loop via
_run_chat_turn) -- this module only supplies a content-scoped system prompt/tool list/dispatcher.
Every tool here calls a real, already-built function from content_opportunity.py/
trend_intelligence.py/sensing.py -- no new business logic, purely a dispatch layer."""

from sqlalchemy.orm import Session

ACTED_BY = "AI Assistant (content chat)"


CONTENT_CHAT_SYSTEM_PROMPT = """You are {business_name}'s content strategy operator -- thinking \
like the person accountable for revenue, not a mentor answering trivia. Today is {today}. Every \
recommendation you make has one real objective behind it: which content actually moves \
{business_name} toward more leads and revenue, not just "what's trending" for its own sake.

You have real access to: real trending topics (from live Google Search sensing, standing in for \
Reddit/X/Perplexity until those API keys exist), real competitor content (from this tenant's \
configured competitors' own sites), the real Content Opportunities already generated from that \
evidence (each with a why-now and a suggested angle, citing real URLs), and -- if this tenant has \
real meeting notes/transcripts synced -- the single best source of real customer language, \
objections, and pain points, worth pulling on before proposing a "customer pain" angle from \
guesswork. If no meetings are synced for this tenant, say so honestly rather than guessing at \
what customers might be saying.

{business_name}'s real positioning: {positioning}. Every suggestion you make should connect back \
to this positioning, grounded in real evidence -- never a generic content idea. Writing for: \
{audience}.

HOW TO PRIORITIZE (this is the real judgment call you're here to make, not the human's to
re-derive every time): no fake "engagement score" exists, and you must never invent one. Instead,
reason from what's actually real in the tool data:
- recent_observation_count and recent_independent_entity_count are the closest real proxy for
  how much real attention a topic currently has -- more independent sources talking about
  something now is real signal, a single mention isn't.
- account_bridge.linked_account_count is the closest real proxy for revenue relevance -- a topic
  with real target accounts already discussing it is closer to an actual deal than a topic that's
  only trending in the abstract. Weight this heavily: engagement without a path to a real account
  is a vanity outcome, not the goal.
- origin ("trend" vs "competitor") tells you whether the pressure is market-wide or a specific
  competitor already occupying that ground -- worth naming which one you're reacting to.
When asked what to prioritize, don't just list options -- make an actual call, defend it with
these real numbers, and say what you'd trade off (e.g. "X has more raw buzz, but Y has 3 real
target accounts already circling it, so Y is the better revenue bet right now").

Your real jobs:
1. SUGGEST TOPICS -- pull real trending topics/meetings and existing content opportunities, then
RANK them by the real revenue-relevance reasoning above, not just by how new or big they look.
This is a SUGGESTION, never a gate: if nothing is trending yet and no meetings are synced, that
just means you have nothing extra to suggest -- it never means you can't write anything. Say so in
one line, then move straight to job 2.
2. WRITE CONTENT -- once the user has picked (or you've proposed and they've confirmed) an angle,
OR the user has directly told you what to write about, write it. THREE separate paths exist, and
using the wrong one is what causes dead ends -- most requests are path 3, not 1 or 2:
   - Market-trend/competitor angle -> generate_opportunity_for_topic, get it approved, THEN
     generate_platform_draft. Only for a request that specifically wants market/competitor
     evidence behind it.
   - Meeting/call-grounded angle ("from my meetings", "what my customers are saying") ->
     draft_post_from_meetings DIRECTLY. Never route this through generate_opportunity_for_topic --
     that tool checks for market-trend evidence, which a meeting-grounded angle doesn't have and
     doesn't need.
   - EVERYTHING ELSE (a personal story, an opinion, a framework, "write about X", any topic the
     user just tells you) -> draft_from_own_expertise DIRECTLY, with no market evidence and no
     meeting notes required at all. This is the DEFAULT path whenever the user names a topic
     themselves -- most real thought-leadership content is exactly this, grounded in the
     business's own real positioning, not external proof. If you find yourself about to say "no
     trending topics" or "no meeting notes" in response to a user who already told you what to
     write about, that's the sign you're about to use the wrong path -- stop and call
     draft_from_own_expertise instead.

DECISIVENESS: don't ask more than one clarifying question per request. If topics were already
suggested and the user picked one, go straight to writing it -- don't re-verify who they are or
second-guess which meetings are "real" ones; the tenant's own meeting data is the real data. Ask
again only if a tool call itself fails or returns nothing usable.

Be direct and decisive, like an operator making a real call, not a mentor listing options. Never
invent evidence, a URL, a statistic, a dollar figure, or an engagement number not returned by a
tool -- a vague-but-true point beats a specific-but-invented one. A finished draft must be ready
to publish as-is: no meta-commentary about why it works, no bracketed placeholders, no explaining
your reasoning inside the post itself."""


CONTENT_CHAT_TOOLS = [
    {
        "name": "list_trending_topics",
        "description": "Real trending topics with their trend state (emerging/accelerating/persistent/stable/declining/insufficient_evidence), evidence counts, and account-bridge coverage.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_content_opportunities",
        "description": "Real content opportunities already generated (why-now, suggested angle, cited URLs, origin trend/competitor, status, any drafts already written).",
        "input_schema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["candidate", "approved", "rejected", "changes_requested"]}}},
    },
    {
        "name": "generate_opportunity_for_topic",
        "description": "Generate a real content opportunity (why-now + angle) for one specific topic by its content_topic_id -- only succeeds if that topic actually has enough real, recent, independent evidence; reports honestly if it doesn't.",
        "input_schema": {"type": "object", "properties": {"content_topic_id": {"type": "integer"}}, "required": ["content_topic_id"]},
    },
    {
        "name": "review_content_opportunity",
        "description": "Approve, reject, or request changes on a content opportunity so it can move toward being drafted.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content_opportunity_id": {"type": "integer"},
                "action": {"type": "string", "enum": ["approve", "reject", "request_changes"]},
                "note": {"type": "string"},
            },
            "required": ["content_opportunity_id", "action"],
        },
    },
    {
        "name": "generate_platform_draft",
        "description": "Write a real, platform-specific draft (blog, linkedin, or twitter) for an already-approved content opportunity -- each platform gets its own real length/voice, never the same draft reused.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content_opportunity_id": {"type": "integer"},
                "platform": {"type": "string", "enum": ["blog", "linkedin", "twitter"]},
            },
            "required": ["content_opportunity_id", "platform"],
        },
    },
    {
        "name": "run_fresh_sensing",
        "description": "Trigger a real, on-demand check for new trending/competitor content evidence right now (real cost, real Apify/Google Search calls) -- use when explicitly asked to look for fresh topics rather than relying on the daily automatic sweep.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_meeting_brief",
        "description": "Real Granola meeting notes/transcripts -- what was actually said, objections raised, buying signals -- useful for grounding content in real customer language rather than a generic idea. person_email and booking_id are both OPTIONAL: call with NO arguments to pull a brief across the most recent meetings overall -- e.g. for 'write a post from my recent meetings' or any request not about one specific person. Never ask the user for an email or booking id first; call with no arguments and see what comes back.",
        "input_schema": {"type": "object", "properties": {"person_email": {"type": "string"}, "booking_id": {"type": "integer"}}},
    },
    {
        "name": "get_open_commitments",
        "description": "Every commitment made across recent real sales meetings (ours and theirs) -- surfaces real, current customer pain/language worth writing about.",
        "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
    },
    {
        "name": "meeting_coverage",
        "description": "How many real Granola meeting notes exist and how many are usable -- the honest denominator before claiming a customer-language angle is well-evidenced.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "draft_post_from_meetings",
        "description": (
            "Write a real, ready-to-post draft grounded directly in recent meetings -- the "
            "correct tool whenever the user wants content 'from my meetings/calls', not from "
            "market trends. Do NOT route a meeting-grounded request through "
            "generate_opportunity_for_topic/review_content_opportunity first -- that pipeline "
            "requires market-trend evidence meetings don't have and don't need, and forcing it "
            "through there just produces a dead end. Call this directly with the angle the user "
            "picked (or one you've proposed and they confirmed) and it returns the finished post."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "angle": {"type": "string", "description": "The topic/angle to write about, in plain language"},
                "platform": {"type": "string", "enum": ["blog", "linkedin", "twitter"]},
            },
            "required": ["angle"],
        },
    },
    {
        "name": "draft_from_own_expertise",
        "description": (
            "Write a real, ready-to-post draft on a topic the user is telling you directly, "
            "grounded in the business's own real positioning/audience -- NO external market "
            "evidence and NO synced meeting notes required. This is the correct tool whenever "
            "the user already knows what they want to write about (a personal story, an "
            "opinion, a framework, their own experience) -- do NOT tell them 'no trending "
            "topics' or 'no meeting notes' first. Only fall back to generate_opportunity_for_topic "
            "(market-trend-grounded) or draft_post_from_meetings (customer-language-grounded) when "
            "the user explicitly wants something grounded in external evidence or real customer "
            "quotes specifically -- otherwise, when they just tell you a topic, write it directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_request": {"type": "string", "description": "What the user asked to write about, in their own words"},
                "platform": {"type": "string", "enum": ["blog", "linkedin", "twitter"]},
            },
            "required": ["user_request"],
        },
    },
]


def _serialize_opportunity(o, topic_name: str | None) -> dict:
    return {
        "id": o.id,
        "content_topic_id": o.content_topic_id,
        "topic_name": topic_name,
        "origin": o.origin,
        "trend_state": o.trend_state,
        "why_now": o.why_now,
        "suggested_angle": o.suggested_angle,
        "cited_urls": o.cited_urls,
        "status": o.status,
        "drafts": o.drafts or {},
    }


def execute_content_chat_tool(name: str, tool_input: dict, db: Session, tenant_id: int) -> dict:
    """Dispatches one Claude tool_use call to a real content-strategy function. Every branch is
    wrapped by the caller in a try/except -- a failed tool call becomes an error string fed back
    to Claude, not a crashed chat turn (same discipline as v2_chat_tools.py)."""

    if name == "list_trending_topics":
        from app.gtm_os.content.trend_intelligence import get_market_intelligence_overview
        return get_market_intelligence_overview(db, tenant_id)

    if name == "list_content_opportunities":
        from app.gtm_os.content.content_opportunity import ContentOpportunity
        from app.gtm_os.content.topic import ContentTopic

        query = db.query(ContentOpportunity).filter(ContentOpportunity.tenant_id == tenant_id)
        if tool_input.get("status"):
            query = query.filter(ContentOpportunity.status == tool_input["status"])
        opportunities = query.order_by(ContentOpportunity.created_at.desc()).all()
        topic_ids = {o.content_topic_id for o in opportunities}
        topics_by_id = {t.id: t.canonical_name for t in db.query(ContentTopic).filter(ContentTopic.id.in_(topic_ids)).all()} if topic_ids else {}
        return {"opportunities": [_serialize_opportunity(o, topics_by_id.get(o.content_topic_id)) for o in opportunities]}

    if name == "generate_opportunity_for_topic":
        from app.gtm_os.content.content_opportunity import generate_content_opportunity
        return generate_content_opportunity(db, tenant_id, tool_input["content_topic_id"])

    if name == "review_content_opportunity":
        from app.gtm_os.content.content_opportunity import (
            approve_content_opportunity, reject_content_opportunity, request_content_opportunity_changes,
        )
        action = tool_input["action"]
        opp_id = tool_input["content_opportunity_id"]
        note = tool_input.get("note")
        if action == "approve":
            opportunity = approve_content_opportunity(db, tenant_id, opp_id, reviewed_by=ACTED_BY)
        elif action == "reject":
            opportunity = reject_content_opportunity(db, tenant_id, opp_id, reviewed_by=ACTED_BY, note=note)
        else:
            opportunity = request_content_opportunity_changes(db, tenant_id, opp_id, reviewed_by=ACTED_BY, note=note or "changes requested via content chat")
        return {"id": opportunity.id, "status": opportunity.status}

    if name == "generate_platform_draft":
        from app.gtm_os.content.content_opportunity import generate_content_draft
        return generate_content_draft(db, tenant_id, tool_input["content_opportunity_id"], platform=tool_input["platform"])

    if name == "run_fresh_sensing":
        from app.apify_client import GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD
        from app.apify_budget_guard import STATUS_ALLOWED, check_apify_budget
        from app.gtm_os.content.content_opportunity import run_content_opportunity_generation_sweep
        from app.gtm_os.content.promotion import run_candidate_promotion_sweep
        from app.gtm_os.content.topic_linking import run_content_topic_linking_sweep
        from app.gtm_os.content.trend_intelligence import run_trend_intelligence_sweep
        from app.gtm_os.intelligence.sensing import sense_competitor_content, sense_web_search_trends

        trend_limit, competitor_limit = 20, 30
        estimated_cost = (trend_limit + competitor_limit) * GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD
        budget_result = check_apify_budget(db, tenant_id, estimated_cost)
        if budget_result["status"] != STATUS_ALLOWED:
            return {"status": "budget_blocked", "reason": budget_result["reason"]}

        trend_signals = sense_web_search_trends(db, tenant_id, limit=trend_limit)
        competitor_signals = sense_competitor_content(db, tenant_id, limit=competitor_limit)
        linking = run_content_topic_linking_sweep(db, tenant_id)
        run_candidate_promotion_sweep(db, tenant_id)
        run_trend_intelligence_sweep(db, tenant_id)
        opportunities = run_content_opportunity_generation_sweep(db, tenant_id)
        return {
            "status": "ok",
            "new_trend_signals": len(trend_signals),
            "new_competitor_signals": len(competitor_signals),
            "topic_linking": linking,
            "opportunity_generation": opportunities,
        }

    if name == "get_meeting_brief":
        from app.gtm_os.meetings.meeting_intelligence import get_meeting_brief
        return get_meeting_brief(db, tenant_id, person_email=tool_input.get("person_email"),
                                  booking_id=tool_input.get("booking_id"))

    if name == "get_open_commitments":
        from app.gtm_os.meetings.meeting_intelligence import get_open_commitments
        return get_open_commitments(db, tenant_id, limit=tool_input.get("limit", 20))

    if name == "meeting_coverage":
        from app.gtm_os.meetings.meeting_intelligence import meeting_coverage
        return meeting_coverage(db, tenant_id)

    if name == "draft_post_from_meetings":
        from app.gtm_os.meetings.meeting_intelligence import draft_post_from_meetings
        return draft_post_from_meetings(db, tenant_id, angle=tool_input["angle"],
                                         platform=tool_input.get("platform", "linkedin"))

    if name == "draft_from_own_expertise":
        from app.gtm_os.content.content_opportunity import generate_direct_draft
        return generate_direct_draft(db, tenant_id, user_request=tool_input["user_request"],
                                      platform=tool_input.get("platform", "linkedin"))

    return {"error": f"unknown tool {name!r}"}
