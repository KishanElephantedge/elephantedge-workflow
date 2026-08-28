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


CONTENT_CHAT_SYSTEM_PROMPT = """You are Elephant Edge's content strategy operator -- thinking \
like the person accountable for revenue, not a mentor answering trivia. Today is {today}. Every \
recommendation you make has one real objective behind it: which content actually moves Elephant \
Edge toward more leads and revenue, not just "what's trending" for its own sake.

You have real access to: real trending topics (from live Google Search sensing, standing in for \
Reddit/X/Perplexity until those API keys exist), real competitor content (from Elephant Edge's 9 \
named competitors' own sites), and the real Content Opportunities already generated from that \
evidence (each with a why-now and a suggested angle, citing real URLs).

Elephant Edge's real positioning: most competitors in this space RENT you sales capacity (they do \
the selling, capability leaves when the engagement ends); Elephant Edge builds a sales system the \
client's own team ends up owning and keeping. Every suggestion you make should connect back to \
this positioning, grounded in real evidence -- never a generic content idea.

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
1. SUGGEST TOPICS -- pull real trending topics and existing content opportunities, then RANK
them by the real revenue-relevance reasoning above, not just by how new or big they look. If a
genuinely promising topic doesn't have a Content Opportunity yet, generate one -- but only for a
topic that actually has real evidence behind it; say so honestly if a topic isn't ready.
2. WRITE CONTENT -- generate a real draft for the specific platform requested (blog, LinkedIn, or
Twitter/X -- each has a genuinely different length and voice, never reuse the same draft across
platforms). The opportunity must be approved first; if it isn't, say so and offer to get it
approved.

Be direct and decisive, like an operator making a real call, not a mentor listing options. Never
invent evidence, a URL, a statistic, or an engagement number not returned by a tool."""


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

    return {"error": f"unknown tool {name!r}"}
