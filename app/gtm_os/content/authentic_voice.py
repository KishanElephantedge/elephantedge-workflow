"""Content Intelligence, "authentic voice" topic source (2026-09-21, explicit instruction).
A fourth topic origin alongside "trend"/"competitor"/"account_intelligence": instead of an
external trigger (market movement, competitor content, pipeline pattern), this looks at the
partner's OWN real recent posts -- their real themes, frameworks, and voice -- and proposes a
natural continuation on one of those same themes.

No external evidence is needed here BY DESIGN: the "why now" is simply "you already write about
this, here is another real angle on the same theme, in your voice" -- not a trending-topic claim.
Grounding discipline is the same as the rest of Content Intelligence in a different shape: the
LLM only ever sees the partner's own real post excerpts, never invents a theme or a framework
name that doesn't appear in that real material.

REAL POSTS, FETCHED AUTOMATICALLY (fixed 2026-10-12, was originally designed around a
partner_recent_posts Parameter that nothing ever populated -- hand-feeding it per partner on
demand is not a real production path, see app/gtm_os/learning/own_linkedin_content.py's own
module docstring for why that module exists). own_linkedin_profile_url is already a required
config key for the content feature (app/gtm_os/features/registry.py) and
own_linkedin_content.py already has a real, working Apify-backed sync against it
(OwnLinkedinPost table, dedup'd by post_urn). So this module syncs that same real source
on-demand, through the same Apify budget guard every other on-demand Apify call in this codebase
goes through (see run_fresh_sensing in content_chat_tools.py for the identical pattern), rather
than depending on a second, never-wired data source.

Reuses the existing ContentTopic/ContentOpportunity tables and review lifecycle unchanged --
origin="authentic_voice" is simply a fourth value alongside the ones content_opportunity.py's own
module docstring already documents."""

from sqlalchemy.orm import Session

from app.apify_budget_guard import STATUS_ALLOWED, check_apify_budget
from app.apify_client import LINKEDIN_POST_COST_PER_POST_USD
from app.gtm_os.content.content_business_context import get_content_business_context
from app.gtm_os.content.content_opportunity import ContentOpportunity
from app.gtm_os.content.topic import ContentTopic
from app.gtm_os.learning.own_linkedin_content import (
    get_own_linkedin_profile_url, get_recent_own_posts, sync_own_linkedin_posts,
)
from app.llm_client import generate_json

AUTHENTIC_VOICE_TOPIC_NAME = "Recurring Themes"
SYNC_POST_LIMIT = 10


def get_partner_recent_posts(db: Session, tenant_id: int, auto_sync: bool = True) -> list[dict]:
    """Real recent post excerpts for this partner's own LinkedIn profile --
    [{"date": "...", "text": "..."}], most recent first. Returns [] (never invented filler) if
    no own_linkedin_profile_url is configured yet, or if the profile genuinely has no posts.

    auto_sync=True (the default) triggers a real, budget-gated Apify refresh first, so a caller
    never has to remember to sync separately -- set False only for a read-only check that must
    never spend money (e.g. a status/diagnostic call)."""
    profile_url = get_own_linkedin_profile_url(db, tenant_id)
    if not profile_url:
        return []

    if auto_sync:
        estimated_cost = SYNC_POST_LIMIT * LINKEDIN_POST_COST_PER_POST_USD
        budget_result = check_apify_budget(db, tenant_id, estimated_cost, operation="authentic_voice_sync")
        if budget_result["status"] == STATUS_ALLOWED:
            sync_own_linkedin_posts(db, tenant_id, limit=SYNC_POST_LIMIT)
        # A blocked/unavailable budget degrades to "use whatever's already on file" below --
        # never raises, same fail-safe-not-fail-open discipline as every other Apify call site.

    posts = get_recent_own_posts(db, tenant_id, limit=SYNC_POST_LIMIT)
    return [{"date": p.posted_at.isoformat() if p.posted_at else "undated", "text": p.post_text or ""} for p in posts if p.post_text]


def _get_or_create_authentic_voice_topic(db: Session, tenant_id: int) -> ContentTopic:
    """One reusable ContentTopic per tenant for this origin -- this source doesn't propose a new
    topic per theme the way trend/competitor do; it always reasons over the same real post
    history, so one durable topic row is the right shape (same "one reusable row" precedent as
    _get_or_create_signal_batch in company_resolution.py)."""
    topic = (
        db.query(ContentTopic)
        .filter(ContentTopic.tenant_id == tenant_id, ContentTopic.canonical_name == AUTHENTIC_VOICE_TOPIC_NAME)
        .first()
    )
    if topic is None:
        topic = ContentTopic(tenant_id=tenant_id, canonical_name=AUTHENTIC_VOICE_TOPIC_NAME, aliases=[], origin="configured")
        db.add(topic)
        db.commit()
        db.refresh(topic)
    return topic


AUTHENTIC_VOICE_PROMPT = """You are helping {business_name} plan their next piece of content, in \
their own real voice.

{business_name}'s real positioning: {positioning}
Audience: {audience}

Below are their own real, recent LinkedIn posts (nothing else -- no external trend, no \
competitor content):

{posts_block}

Your job: identify ONE real theme they already write about across these posts (a recurring \
subject, framework, or angle -- not something new), and propose a natural continuation on that \
SAME theme -- a new, specific angle they haven't already covered in the posts above, but that \
clearly belongs in the same series/voice.

CRITICAL RULES:
- The theme must be something they demonstrably already write about above -- quote or closely \
paraphrase what shows that pattern in why_now.
- Never invent a framework, statistic, or claim that isn't in their real posts above.
- The suggested continuation must sound like a natural next post in their own voice and style, \
not a generic industry take.

Return JSON exactly:
{{"headline": "<a real, specific, compelling title for the next post -- never a bare topic name>", \
"why_now": "<2-3 sentences: which real recurring theme this continues, citing what they actually \
said above>", \
"suggested_angle": "<1-2 sentences: the specific new angle on that same theme for this next post>"}}"""


def generate_authentic_voice_opportunity(db: Session, tenant_id: int) -> dict:
    """Layer 1 (real gating) + Layer 2 (grounded synthesis), same shape as
    content_opportunity.generate_content_opportunity. Returns
    {"status": "insufficient_evidence" | "already_exists" | "llm_unavailable" | "ok", ...}."""
    if not get_own_linkedin_profile_url(db, tenant_id):
        return {"status": "not_configured", "reason": "no own_linkedin_profile_url set for this tenant yet (Settings) -- needed to find their real posts"}

    posts = get_partner_recent_posts(db, tenant_id)
    if not posts:
        return {"status": "insufficient_evidence", "reason": "synced this partner's real LinkedIn profile but found no posts yet -- nothing to continue a theme from"}

    business_context = get_content_business_context(db, tenant_id)
    if not business_context.get("business_name"):
        return {"status": "no_business_context", "reason": "this tenant hasn't set business_name/positioning/audience yet"}

    topic = _get_or_create_authentic_voice_topic(db, tenant_id)

    existing = (
        db.query(ContentOpportunity)
        .filter(ContentOpportunity.tenant_id == tenant_id, ContentOpportunity.content_topic_id == topic.id)
        .filter(ContentOpportunity.status != "rejected")
        .order_by(ContentOpportunity.created_at.desc())
        .first()
    )
    if existing is not None:
        return {"status": "already_exists", "content_opportunity_id": existing.id}

    posts_block = "\n\n".join(f"[{p.get('date', 'undated')}] {p.get('text', '')}" for p in posts)
    prompt = AUTHENTIC_VOICE_PROMPT.format(
        business_name=business_context["business_name"], positioning=business_context["positioning"],
        audience=business_context.get("audience") or "not specified", posts_block=posts_block,
    )

    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=700)
    except Exception as e:  # noqa: BLE001 -- LLM outage must degrade, never crash the caller
        return {"status": "llm_unavailable", "reason": str(e)}

    if not response.get("headline") or not response.get("why_now") or not response.get("suggested_angle"):
        return {"status": "discarded", "reason": "missing headline, why_now, or suggested_angle"}

    opportunity = ContentOpportunity(
        tenant_id=tenant_id, content_topic_id=topic.id, origin="authentic_voice", trend_state="stable",
        headline=response["headline"], why_now=response["why_now"], suggested_angle=response["suggested_angle"],
        cited_urls=[], status="candidate",
    )
    db.add(opportunity)
    db.commit()
    db.refresh(opportunity)
    return {"status": "ok", "content_opportunity_id": opportunity.id, "origin": "authentic_voice"}
