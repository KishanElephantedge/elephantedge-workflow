"""Content Intelligence, "authentic voice" topic source (2026-09-21, explicit instruction).
A fourth topic origin alongside "trend"/"competitor"/"account_intelligence": instead of an
external trigger (market movement, competitor content, pipeline pattern), this looks at the
partner's OWN real recent posts -- their real themes, frameworks, and voice -- and proposes a
natural continuation on one of those same themes.

No external evidence is needed here BY DESIGN: the "why now" is simply "you already write about
this, here is another real angle on the same theme, in your voice" -- not a trending-topic claim.
Grounding discipline is the same as the rest of Content Intelligence in a different shape: the
LLM only ever sees the partner's own real post excerpts (partner_recent_posts, a Parameter-backed
list the partner/operator keeps current), never invents a theme or a framework name that doesn't
appear in that real material.

Reuses the existing ContentTopic/ContentOpportunity tables and review lifecycle unchanged --
origin="authentic_voice" is simply a fourth value alongside the ones content_opportunity.py's own
module docstring already documents."""

from sqlalchemy.orm import Session

from app.db.models import Parameter
from app.gtm_os.content.content_business_context import get_content_business_context
from app.gtm_os.content.content_opportunity import ContentOpportunity
from app.gtm_os.content.topic import ContentTopic
from app.llm_client import generate_json

RECENT_POSTS_PARAMETER_KEY = "partner_recent_posts"
AUTHENTIC_VOICE_TOPIC_NAME = "Recurring Themes"


def get_partner_recent_posts(db: Session, tenant_id: int) -> list[dict]:
    """Real recent post excerpts on file for this partner -- [{"date": "...", "text": "..."}].
    Never invented; an empty list means nobody has captured any yet (real gap, not silently
    filled with generic content)."""
    param = db.query(Parameter).filter(Parameter.tenant_id == tenant_id, Parameter.key == RECENT_POSTS_PARAMETER_KEY).first()
    return param.value if param and isinstance(param.value, list) else []


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
    posts = get_partner_recent_posts(db, tenant_id)
    if not posts:
        return {"status": "insufficient_evidence", "reason": "no partner_recent_posts on file for this tenant yet"}

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
