"""Content Intelligence, "Quick Drafts" -- account-intelligence topic source (2026-09-19,
explicit instruction). A third topic origin alongside "trend" and "competitor": instead of
external market/competitor signal, this reasons over the REAL, already-fetched company pipeline
(ProblemHypothesis/DemandHypothesis, the same evidence-backed hypotheses the outbound sensing
layer already produces) to find recurring patterns across the current ICP base worth writing
about.

GROUNDING DISCIPLINE, same as the rest of Content Intelligence: the LLM only ever sees an
AGGREGATE, ANONYMIZED block -- counts and plain-language problem/demand statements grouped by
affected_function, never a company name, domain, or any other identifying detail. The prompt
explicitly forbids naming or describing any single real company; a topic must describe a pattern
across multiple companies, not one company's specific situation. This is the same
never-fabricate-never-identify discipline as CONTENT_OPPORTUNITY_PROMPT's "only cite real
evidence", just enforced by anonymizing the input rather than checking the output.

Reuses the existing ContentTopic/ContentOpportunity tables and review lifecycle unchanged --
origin="account_intelligence" is simply a third value alongside "trend"/"competitor" (see
content_opportunity.py's own origin field comment). why_now IS the aggregate evidence summary
(no separate evidence-URL citation makes sense here, so cited_urls is stored empty and why_now
carries the real grounding instead)."""

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import Batch, Company
from app.gtm_os.content.content_business_context import get_content_business_context
from app.gtm_os.content.content_opportunity import ContentOpportunity
from app.gtm_os.content.topic import ContentTopic
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
from app.llm_client import generate_json

# Bounds both prompt size/cost and staleness -- only recent hypotheses describe what the ICP
# base is facing "right now", matching the spirit of trend_intelligence.py's own recency weighting.
LOOKBACK_DAYS = 60
MAX_STATEMENTS_PER_FUNCTION = 6
MIN_COMPANIES_FOR_A_PATTERN = 3  # a pattern needs multiple real companies, never a single one


def _aggregate_account_patterns(db: Session, tenant_id: int) -> dict:
    """Real, anonymized aggregate: {affected_function: {"company_count": N, "industries": {...},
    "problem_statements": [...], "demand_statements": [...]}}. No company name/domain/id ever
    enters this structure -- only the function, industry label, and the plain-language statement
    text, so the LLM downstream physically cannot cite a specific company even if it wanted to."""
    since = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)

    problems = (
        db.query(ProblemHypothesis, Company.industry)
        .outerjoin(Company, ProblemHypothesis.company_id == Company.id)
        .join(Batch, Company.batch_id == Batch.id, isouter=True)
        .filter(ProblemHypothesis.tenant_id == tenant_id)
        .filter(ProblemHypothesis.created_at >= since)
        .all()
    )
    demands = (
        db.query(DemandHypothesis, Company.industry)
        .outerjoin(Company, DemandHypothesis.company_id == Company.id)
        .filter(DemandHypothesis.tenant_id == tenant_id)
        .filter(DemandHypothesis.created_at >= since)
        .all()
    )

    by_function = defaultdict(lambda: {"company_ids": set(), "industries": defaultdict(int), "problem_statements": [], "demand_statements": []})

    for hyp, industry in problems:
        bucket = by_function[hyp.affected_function]
        if hyp.company_id:
            bucket["company_ids"].add(hyp.company_id)
        if industry:
            bucket["industries"][industry] += 1
        if len(bucket["problem_statements"]) < MAX_STATEMENTS_PER_FUNCTION and hyp.problem_statement:
            bucket["problem_statements"].append(hyp.problem_statement)

    for hyp, industry in demands:
        bucket = by_function[hyp.affected_function]
        if hyp.company_id:
            bucket["company_ids"].add(hyp.company_id)
        if industry:
            bucket["industries"][industry] += 1
        if len(bucket["demand_statements"]) < MAX_STATEMENTS_PER_FUNCTION and hyp.demand_statement:
            bucket["demand_statements"].append(hyp.demand_statement)

    return {
        function: {
            "company_count": len(bucket["company_ids"]),
            "industries": dict(bucket["industries"]),
            "problem_statements": bucket["problem_statements"],
            "demand_statements": bucket["demand_statements"],
        }
        for function, bucket in by_function.items()
        if len(bucket["company_ids"]) >= MIN_COMPANIES_FOR_A_PATTERN
    }


ACCOUNT_INTELLIGENCE_TOPIC_PROMPT = """You are helping {business_name} find real content topics \
grounded in patterns across its OWN current pipeline of companies -- not external trends, not \
competitor content, but what {business_name}'s real target accounts are actually facing right now.

{business_name}'s real positioning: {positioning}
Audience: {audience}

Below is a REAL, AGGREGATED, ANONYMIZED summary of patterns across multiple companies currently \
in the pipeline, grouped by which business function is affected. No company name, domain, or any \
other identifying detail is included anywhere below -- only the pattern itself:

{patterns_block}

CRITICAL RULES:
- Every topic must describe a PATTERN across multiple real companies above, never a single \
company's specific situation -- you have no company names to reference anyway, so this should be \
natural, but be explicit that a topic is about the pattern, not "a company we work with".
- Ground every topic in the real statements/counts above. Never invent a statistic, company \
detail, or claim not present in the aggregate data.
- Propose topics {business_name} could genuinely write about because its own real pipeline shows \
this is what its ICP is actually dealing with -- not generic industry commentary.

Return JSON exactly:
{{"topics": [
  {{"topic_name": "<short, specific topic name>",
    "affected_function": "<the function key from the data above this topic is grounded in>",
    "why_now": "<2-3 sentences: the real aggregate pattern (cite the real company_count and a \
real statement) that makes this worth writing about now>",
    "suggested_angle": "<1-2 sentences: the specific angle to take, consistent with the real \
positioning above>"}}
], ...]}}

Propose at most 5 topics, only for patterns that genuinely support a real, specific topic -- \
fewer good topics beats padding to 5."""


def generate_account_intelligence_topics(db: Session, tenant_id: int) -> dict:
    """Real gating (Layer 1): only functions with >= MIN_COMPANIES_FOR_A_PATTERN real companies
    ever reach the LLM -- a pattern of one company is not a pattern. Grounded synthesis (Layer 2):
    the LLM only ever sees the anonymized aggregate, never a company identity. Returns
    {"status": "insufficient_data" | "no_business_context" | "llm_unavailable" | "ok", ...}."""
    patterns = _aggregate_account_patterns(db, tenant_id)
    if not patterns:
        return {"status": "insufficient_data", "reason": f"no affected_function has >= {MIN_COMPANIES_FOR_A_PATTERN} real companies with a hypothesis in the last {LOOKBACK_DAYS} days"}

    business_context = get_content_business_context(db, tenant_id)
    if not business_context.get("business_name"):
        return {"status": "no_business_context", "reason": "this tenant hasn't set business_name/positioning/audience yet (PUT /gtm-os/partner/content-context)"}

    patterns_block = "\n\n".join(
        f"FUNCTION: {function} ({data['company_count']} real companies, industries: {data['industries'] or 'not recorded'})\n"
        + ("Problem patterns:\n" + "\n".join(f"  - {s}" for s in data["problem_statements"]) if data["problem_statements"] else "")
        + ("\nDemand patterns:\n" + "\n".join(f"  - {s}" for s in data["demand_statements"]) if data["demand_statements"] else "")
        for function, data in patterns.items()
    )
    prompt = ACCOUNT_INTELLIGENCE_TOPIC_PROMPT.format(
        business_name=business_context["business_name"], positioning=business_context["positioning"],
        audience=business_context.get("audience") or "not specified", patterns_block=patterns_block,
    )

    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=1500)
    except Exception as e:  # noqa: BLE001 -- an LLM outage must never crash the caller
        return {"status": "llm_unavailable", "error": str(e)}

    topics = response.get("topics") if isinstance(response, dict) else None
    if not topics or not isinstance(topics, list):
        return {"status": "discarded", "reason": "no topics returned"}

    created = []
    for item in topics:
        function = item.get("affected_function")
        if function not in patterns:
            continue  # references a function never actually given -- discarded, never trusted blind
        if not item.get("topic_name") or not item.get("why_now") or not item.get("suggested_angle"):
            continue

        existing_topic = (
            db.query(ContentTopic)
            .filter(ContentTopic.tenant_id == tenant_id, ContentTopic.canonical_name == item["topic_name"])
            .first()
        )
        topic = existing_topic or ContentTopic(tenant_id=tenant_id, canonical_name=item["topic_name"], origin="discovered")
        if existing_topic is None:
            db.add(topic)
            db.commit()
            db.refresh(topic)

        existing_opportunity = (
            db.query(ContentOpportunity)
            .filter(ContentOpportunity.tenant_id == tenant_id, ContentOpportunity.content_topic_id == topic.id)
            .filter(ContentOpportunity.status != "rejected")
            .first()
        )
        if existing_opportunity is not None:
            continue

        opportunity = ContentOpportunity(
            tenant_id=tenant_id, content_topic_id=topic.id, origin="account_intelligence",
            trend_state="account_pattern",  # not a trend-intelligence state -- this origin has no trend curve
            why_now=item["why_now"], suggested_angle=item["suggested_angle"], cited_urls=[],
        )
        db.add(opportunity)
        db.commit()
        db.refresh(opportunity)
        created.append(opportunity.id)

    return {"status": "ok", "content_opportunity_ids": created, "patterns_considered": list(patterns.keys())}
