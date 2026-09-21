"""Content Intelligence, partner-pipeline topic source (2026-09-21, explicit instruction).
A lighter, partner-tenant version of account_intelligence_topics.py's own idea: instead of
reasoning over ProblemHypothesis/DemandHypothesis records (which require the full V2 sensing
pipeline to have run against a tenant's companies -- not true yet for most partner tenants),
this reasons directly over the real, already-fetched Company rows in a partner's own ICP-matched
pipeline (hiring_signal_role, industry, employee_count) -- the same real data the partner's
Accounts tab already shows them, just aggregated.

Same grounding discipline as account_intelligence_topics.py: the LLM only ever sees an
AGGREGATE, ANONYMIZED count -- never a company name or domain. A pattern needs multiple real
companies, never one -- MIN_COMPANIES_FOR_A_PATTERN is lower than account_intelligence_topics.py's
(3) because a partner's matched pool is typically far smaller (tens, not hundreds) at this stage;
revisit upward once partner pools grow.

Reuses the existing ContentTopic/ContentOpportunity tables and review lifecycle unchanged --
origin="pipeline_signal" is a fifth value alongside trend/competitor/account_intelligence/
authentic_voice (see content_opportunity.py's own origin field comment)."""

from collections import defaultdict

from sqlalchemy.orm import Session

from app.db.models import Batch, Company
from app.gtm_os.content.content_business_context import get_content_business_context
from app.gtm_os.content.content_opportunity import ContentOpportunity
from app.gtm_os.content.topic import ContentTopic
from app.llm_client import generate_json

MIN_COMPANIES_FOR_A_PATTERN = 2
PIPELINE_SIGNAL_TOPIC_NAME = "Target Account Signals"


def _aggregate_pipeline_patterns(db: Session, tenant_id: int) -> dict:
    """Real, anonymized aggregate over this tenant's own matched Company pool: {hiring_signal_role:
    {"company_count": N, "industries": {...}}}. No company name/domain/id ever enters this
    structure. Only non-null hiring_signal_role values are counted -- "no signal detected" is not
    a pattern to write about."""
    rows = (
        db.query(Company.hiring_signal_role, Company.industry)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id)
        .filter(Company.hiring_signal_role.isnot(None))
        .all()
    )

    by_role = defaultdict(lambda: {"company_count": 0, "industries": defaultdict(int)})
    for role, industry in rows:
        bucket = by_role[role]
        bucket["company_count"] += 1
        if industry:
            bucket["industries"][industry] += 1

    total_matched = db.query(Company).join(Batch, Company.batch_id == Batch.id).filter(Batch.tenant_id == tenant_id).count()

    return {
        "total_matched_companies": total_matched,
        "by_role": {
            role: {"company_count": bucket["company_count"], "industries": dict(bucket["industries"])}
            for role, bucket in by_role.items()
            if bucket["company_count"] >= MIN_COMPANIES_FOR_A_PATTERN
        },
    }


def _get_or_create_pipeline_signal_topic(db: Session, tenant_id: int) -> ContentTopic:
    topic = (
        db.query(ContentTopic)
        .filter(ContentTopic.tenant_id == tenant_id, ContentTopic.canonical_name == PIPELINE_SIGNAL_TOPIC_NAME)
        .first()
    )
    if topic is None:
        topic = ContentTopic(tenant_id=tenant_id, canonical_name=PIPELINE_SIGNAL_TOPIC_NAME, aliases=[], origin="configured")
        db.add(topic)
        db.commit()
        db.refresh(topic)
    return topic


PIPELINE_SIGNAL_PROMPT = """You are helping {business_name} find a real content topic grounded in \
a pattern across its OWN current pipeline of target accounts -- not external trend, not \
competitor content, but what {business_name}'s real matched accounts are actually doing right now.

{business_name}'s real positioning: {positioning}
Audience: {audience}

Below is a REAL, AGGREGATED, ANONYMIZED summary of {total_matched} companies currently matched \
to this ICP, showing how many are hiring for which role right now. No company name, domain, or \
other identifying detail is included -- only the real counts:

{patterns_block}

CRITICAL RULES:
- The topic must describe the PATTERN across these real companies (cite the real count out of \
{total_matched}), never invent a single company's situation.
- Never invent a statistic, industry detail, or claim not present in the aggregate data above.
- The angle must be something {business_name} could genuinely write about because its own real \
pipeline shows this is happening right now -- not generic industry commentary.

Return JSON exactly:
{{"headline": "<a real, specific, compelling title -- never a bare stat or category name>", \
"why_now": "<2-3 sentences citing the real count/pattern above>", \
"suggested_angle": "<1-2 sentences: the specific angle to take, consistent with the real \
positioning above>"}}"""


def generate_pipeline_signal_opportunity(db: Session, tenant_id: int) -> dict:
    """Layer 1 (real gating) + Layer 2 (grounded synthesis), same shape as
    content_opportunity.generate_content_opportunity."""
    patterns = _aggregate_pipeline_patterns(db, tenant_id)
    if not patterns["by_role"]:
        return {
            "status": "insufficient_evidence",
            "reason": f"no hiring-signal role reaches {MIN_COMPANIES_FOR_A_PATTERN}+ companies across "
                      f"the {patterns['total_matched_companies']} currently matched",
        }

    business_context = get_content_business_context(db, tenant_id)
    if not business_context.get("business_name"):
        return {"status": "no_business_context", "reason": "this tenant hasn't set business_name/positioning/audience yet"}

    topic = _get_or_create_pipeline_signal_topic(db, tenant_id)

    existing = (
        db.query(ContentOpportunity)
        .filter(ContentOpportunity.tenant_id == tenant_id, ContentOpportunity.content_topic_id == topic.id)
        .filter(ContentOpportunity.status != "rejected")
        .order_by(ContentOpportunity.created_at.desc())
        .first()
    )
    if existing is not None:
        return {"status": "already_exists", "content_opportunity_id": existing.id}

    patterns_block = "\n".join(
        f"- {role}: {data['company_count']} of {patterns['total_matched_companies']} companies "
        f"(industries: {', '.join(f'{k} ({v})' for k, v in data['industries'].items()) or 'unspecified'})"
        for role, data in patterns["by_role"].items()
    )
    prompt = PIPELINE_SIGNAL_PROMPT.format(
        business_name=business_context["business_name"], positioning=business_context["positioning"],
        audience=business_context.get("audience") or "not specified",
        total_matched=patterns["total_matched_companies"], patterns_block=patterns_block,
    )

    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=700)
    except Exception as e:  # noqa: BLE001 -- LLM outage must degrade, never crash the caller
        return {"status": "llm_unavailable", "reason": str(e)}

    if not response.get("headline") or not response.get("why_now") or not response.get("suggested_angle"):
        return {"status": "discarded", "reason": "missing headline, why_now, or suggested_angle"}

    opportunity = ContentOpportunity(
        tenant_id=tenant_id, content_topic_id=topic.id, origin="pipeline_signal", trend_state="stable",
        headline=response["headline"], why_now=response["why_now"], suggested_angle=response["suggested_angle"],
        cited_urls=[], status="candidate",
    )
    db.add(opportunity)
    db.commit()
    db.refresh(opportunity)
    return {"status": "ok", "content_opportunity_id": opportunity.id, "origin": "pipeline_signal"}
