"""Content Intelligence, "Content Clusters" (2026-09-19, explicit instruction) -- the second real
content mode alongside Quick Drafts (content_opportunity.py). Where Quick Drafts is one topic -> \
one piece, this is the team's own SEO pillar+cluster architecture: one Master Pillar Page per
content theme, with a linked set of sub-blogs, each targeting a specific keyword/intent/angle and
cross-linking back to the pillar and to a real configured offering.

Same review discipline as content_opportunity.py -- candidate/approved/rejected/changes_requested,
never auto-published (no published_at/published_by field on either table), draft generation only
on an approved row, one explicit action at a time. Two tables instead of one because a pillar and
its 9 clusters have genuinely different lifecycles: a pillar can be approved and its master page
written while individual clusters are still being reviewed one at a time, and a cluster's own
draft generation only makes sense once ITS OWN row is approved, not merely because its parent
pillar was.

GROUNDING (2026-09-19, corrected -- explicit instruction: a pillar theme grounded only in offering
names, with no real company/account context, is "not the right topic" -- same complaint as Quick
Drafts was built to fix): the pillar's structure proposal (H2 sections, cluster stubs) must now be
grounded in the SAME real, anonymized aggregate of ProblemHypothesis/DemandHypothesis patterns that
account_intelligence_topics.py already uses for Quick Drafts -- reused here via
_aggregate_account_patterns, never duplicated. A pillar is generated FROM one real pattern (picked
by the LLM, steered by an optional user theme_hint), not from an arbitrary free-typed theme with
only offering names as grounding. What is still checked the same way as before: every cluster CTA
links to an ACTUALLY configured offering (get_offering_config), never an invented product name."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Session

from app.db.models import Base
from app.gtm_os.content.account_intelligence_topics import MIN_COMPANIES_FOR_A_PATTERN, _aggregate_account_patterns
from app.gtm_os.content.content_business_context import get_content_business_context
from app.gtm_os.content.content_opportunity import EXPEDITION_FRAMEWORK_GUIDANCE
from app.gtm_os.opportunity.offering_config import get_offering_config
from app.llm_client import generate_json

VALID_STATUSES = {"candidate", "approved", "rejected", "changes_requested"}
CLUSTER_SIZE = 9  # the team's own architecture: one pillar page, nine linked sub-blogs


class ContentPillar(Base):
    """One Master Pillar Page -- the SEO hub page for one content theme. Never auto-published (no
    published_at/published_by field, matching ContentOpportunity's own discipline)."""
    __tablename__ = "content_pillars"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    theme = Column(String, nullable=False)  # user's theme_hint if given, else the generated title (display label)
    title = Column(String, nullable=False)
    primary_keyword = Column(String, nullable=False)
    secondary_keywords = Column(JSON, nullable=True)  # list[str]
    commercial_goal = Column(String, nullable=True)  # which real offering this is meant to drive toward
    search_intent = Column(Text, nullable=True)
    core_narrative = Column(Text, nullable=True)
    grounded_function = Column(String, nullable=True)  # the real affected_function pattern (from ProblemHypothesis/DemandHypothesis) this pillar is grounded in
    why_now = Column(Text, nullable=True)  # the real aggregate pattern (company_count + a real statement) that grounds this pillar -- same discipline as ContentOpportunity.why_now
    sections = Column(JSON, nullable=False)  # list[str] -- the real H2 outline, generated once, never silently regenerated

    status = Column(String, nullable=False, default="candidate")
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(String, nullable=True)
    review_note = Column(Text, nullable=True)

    draft_text = Column(Text, nullable=True)  # the real master-page draft, one version, same "no history" simplicity as ContentOpportunity.drafts
    draft_generated_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)


class ContentCluster(Base):
    """One sub-blog linked to a Master Pillar Page. Each targets its own keyword/intent/angle and
    is reviewed/drafted independently of its pillar and of its sibling clusters."""
    __tablename__ = "content_clusters"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    content_pillar_id = Column(Integer, ForeignKey("content_pillars.id"), nullable=False)
    order_index = Column(Integer, nullable=False)  # 1..CLUSTER_SIZE, the real sequence in the doc's cluster list

    title = Column(String, nullable=False)
    keyword = Column(String, nullable=False)
    intent = Column(String, nullable=True)  # e.g. "problem awareness", "definition + clarity", "decision"
    angle = Column(Text, nullable=False)
    cta = Column(Text, nullable=True)
    offering_name = Column(String, nullable=True)  # the real configured offering this cluster's CTA links to

    status = Column(String, nullable=False, default="candidate")
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(String, nullable=True)
    review_note = Column(Text, nullable=True)

    draft_text = Column(Text, nullable=True)
    draft_generated_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)


PILLAR_STRUCTURE_PROMPT = """You are planning a real SEO content pillar for {business_name}, \
grounded in REAL patterns from its own current pipeline of target companies -- not a generic \
theme picked out of thin air.

{business_name}'s real positioning: {positioning}
Real audience: {audience}
Really configured offerings this content can point toward: {offerings_block}

Below is a REAL, AGGREGATED, ANONYMIZED summary of patterns across multiple companies currently \
in {business_name}'s pipeline, grouped by which business function is affected. No company name, \
domain, or any other identifying detail is included anywhere below -- only the pattern itself:

{patterns_block}

{theme_line}

Design a Master Pillar Page + {cluster_size} linked sub-blogs that is GROUNDED in exactly ONE of \
the real patterns above -- every section, cluster, and the core narrative must trace back to that \
one real pattern. Never invent a statistic, company detail, or claim not present in the real data \
above.

MASTER PILLAR PAGE:
- grounded_function: the function key from the patterns above this whole pillar is grounded in
- why_now: 2-3 sentences citing the real company_count and a real problem/demand statement from \
that pattern -- the real reason this pillar is worth building now
- title: a real, specific title for the pillar page (not a generic category name)
- primary_keyword / secondary_keywords: real search terms this page should target
- commercial_goal: which ONE of the real configured offerings above this pillar should drive \
toward -- must be one of the real offering names given, never invented
- search_intent: the real question a founder/buyer is asking when they'd land on this page
- core_narrative: the one-line arc this pillar tells, start to end, tying back to the real pattern
- sections: exactly 10 real H2 section titles that build the pillar page's own argument in order

CLUSTER SUB-BLOGS (exactly {cluster_size}):
Each one attacks ONE specific failure point/question within the same grounded pattern, links back \
to the pillar, and ends with a CTA toward the same real commercial_goal offering (or another real \
configured offering if a different one genuinely fits that specific sub-blog better -- never an \
invented offering name).
For each: title, keyword, intent (one short phrase: what search intent this serves), angle \
(1-2 sentences: the specific take this sub-blog takes, grounded in the real pattern), cta (the \
closing hook/question), offering_name (must be one of the real offerings given).

Return JSON exactly:
{{"grounded_function": "<the function key from the data above this pillar is grounded in>", \
"why_now": "...", \
"title": "...", "primary_keyword": "...", "secondary_keywords": ["...", ...], \
"commercial_goal": "<real offering name>", "search_intent": "...", "core_narrative": "...", \
"sections": ["...", ... exactly {cluster_size}+1 items], \
"clusters": [{{"title": "...", "keyword": "...", "intent": "...", "angle": "...", "cta": "...", \
"offering_name": "<real offering name>"}}, ... exactly {cluster_size} items]}}

Never invent an offering name, function, or statistic not given above. Never pad -- every section \
and cluster must genuinely serve the pillar's own grounded narrative."""


def _offerings_block(db: Session, tenant_id: int) -> tuple[str, set[str]]:
    offerings = get_offering_config(db, tenant_id)
    names = {o["name"] for o in offerings}
    # The exact name is often an internal short label (e.g. "Execution") while the description
    # carries the real marketing name (e.g. "Fractional VP Sales") -- confirmed live (2026-09-19)
    # the LLM will otherwise cite the marketing name from the description and get discarded, so
    # the exact required string is repeated and called out explicitly to prevent that.
    block = "\n".join(
        f"- EXACT NAME TO USE: \"{o['name']}\" -- {o.get('description') or '(no description configured)'}"
        for o in offerings
    )
    return block or "(no offerings configured)", names


def generate_content_pillar(db: Session, tenant_id: int, theme_hint: str | None = None) -> dict:
    """Layer 1 (2026-09-19, corrected): real account patterns are now a hard gate, same as Quick
    Drafts -- no patterns, no pillar, regardless of theme_hint. Layer 2: real offering names are
    still checked the same way as before. Layer 3: LLM proposes the real structure, grounded in
    exactly one real pattern; any cited function/offering name never in the real data is
    discarded, never trusted blind. theme_hint is now optional -- a directional nudge only, never
    the sole grounding (that complaint is exactly what this rewrite fixes)."""
    patterns = _aggregate_account_patterns(db, tenant_id)
    if not patterns:
        return {"status": "insufficient_data", "reason": f"no affected_function has >= {MIN_COMPANIES_FOR_A_PATTERN} real companies with a hypothesis in the last 60 days -- a content pillar needs a real pattern across accounts, not just a typed theme"}

    business_context = get_content_business_context(db, tenant_id)
    if not business_context.get("business_name"):
        return {"status": "no_business_context", "reason": "this tenant hasn't set business_name/positioning/audience yet (PUT /gtm-os/partner/content-context)"}

    offerings_block, valid_offerings = _offerings_block(db, tenant_id)
    if not valid_offerings:
        return {"status": "no_offerings_configured", "reason": "no real offerings configured for this tenant -- a pillar's commercial_goal and every cluster CTA need a real offering to point to"}

    patterns_block = "\n\n".join(
        f"FUNCTION: {function} ({data['company_count']} real companies, industries: {data['industries'] or 'not recorded'})\n"
        + ("Problem patterns:\n" + "\n".join(f"  - {s}" for s in data["problem_statements"]) if data["problem_statements"] else "")
        + ("\nDemand patterns:\n" + "\n".join(f"  - {s}" for s in data["demand_statements"]) if data["demand_statements"] else "")
        for function, data in patterns.items()
    )
    theme_line = (
        f'The user wants this pillar to relate to: "{theme_hint}" -- pick whichever real pattern above best fits that direction.'
        if theme_hint else
        "No specific theme was requested -- pick whichever real pattern above is strongest (highest company_count with genuine, specific statements)."
    )

    prompt = PILLAR_STRUCTURE_PROMPT.format(
        business_name=business_context["business_name"], positioning=business_context["positioning"],
        audience=business_context.get("audience") or "not specified", offerings_block=offerings_block,
        patterns_block=patterns_block, theme_line=theme_line, cluster_size=CLUSTER_SIZE,
    )

    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=3000)
    except Exception as e:  # noqa: BLE001
        return {"status": "llm_unavailable", "error": str(e)}

    if not isinstance(response, dict) or not response.get("sections") or not response.get("clusters"):
        return {"status": "discarded", "reason": "malformed response -- missing sections or clusters"}
    if response.get("grounded_function") not in patterns:
        return {"status": "discarded", "reason": f"grounded_function {response.get('grounded_function')!r} references a pattern never actually given"}
    if response.get("commercial_goal") not in valid_offerings:
        return {"status": "discarded", "reason": f"commercial_goal {response.get('commercial_goal')!r} is not a real configured offering"}

    clusters_in = response["clusters"]
    if len(clusters_in) != CLUSTER_SIZE:
        return {"status": "discarded", "reason": f"expected {CLUSTER_SIZE} clusters, got {len(clusters_in)}"}
    for c in clusters_in:
        if c.get("offering_name") not in valid_offerings:
            return {"status": "discarded", "reason": f"cluster {c.get('title')!r} cites offering {c.get('offering_name')!r}, not a real configured offering"}

    pillar = ContentPillar(
        tenant_id=tenant_id, theme=theme_hint or response["title"], title=response["title"],
        primary_keyword=response["primary_keyword"], secondary_keywords=response.get("secondary_keywords") or [],
        commercial_goal=response["commercial_goal"], search_intent=response.get("search_intent"),
        core_narrative=response.get("core_narrative"), sections=response["sections"],
        grounded_function=response["grounded_function"], why_now=response.get("why_now"),
    )
    db.add(pillar)
    db.commit()
    db.refresh(pillar)

    cluster_ids = []
    for i, c in enumerate(clusters_in, start=1):
        cluster = ContentCluster(
            tenant_id=tenant_id, content_pillar_id=pillar.id, order_index=i,
            title=c["title"], keyword=c["keyword"], intent=c.get("intent"),
            angle=c["angle"], cta=c.get("cta"), offering_name=c["offering_name"],
        )
        db.add(cluster)
        db.commit()
        db.refresh(cluster)
        cluster_ids.append(cluster.id)

    return {"status": "ok", "content_pillar_id": pillar.id, "cluster_ids": cluster_ids, "grounded_function": pillar.grounded_function}


def _get_owned_pillar(db: Session, tenant_id: int, content_pillar_id: int) -> ContentPillar:
    row = db.get(ContentPillar, content_pillar_id)
    if row is None or row.tenant_id != tenant_id:
        raise LookupError(f"ContentPillar {content_pillar_id} not found")
    return row


def _get_owned_cluster(db: Session, tenant_id: int, content_cluster_id: int) -> ContentCluster:
    row = db.get(ContentCluster, content_cluster_id)
    if row is None or row.tenant_id != tenant_id:
        raise LookupError(f"ContentCluster {content_cluster_id} not found")
    return row


def review_content_pillar(db: Session, tenant_id: int, content_pillar_id: int, action: str, reviewed_by: str, note: str | None = None) -> ContentPillar:
    """Mirrors content_opportunity.py's approve/reject/request_changes exactly, folded into one
    function since pillar review has no per-field differences worth three separate functions."""
    if action not in ("approve", "reject", "request_changes"):
        raise ValueError(f"action must be approve/reject/request_changes, got {action!r}")
    pillar = _get_owned_pillar(db, tenant_id, content_pillar_id)
    if pillar.status != "candidate":
        raise ValueError(f"cannot review a pillar with status {pillar.status!r} -- must be 'candidate'")
    pillar.status = {"approve": "approved", "reject": "rejected", "request_changes": "changes_requested"}[action]
    pillar.reviewed_at = datetime.utcnow()
    pillar.reviewed_by = reviewed_by
    pillar.review_note = note
    db.commit()
    return pillar


def review_content_cluster(db: Session, tenant_id: int, content_cluster_id: int, action: str, reviewed_by: str, note: str | None = None) -> ContentCluster:
    if action not in ("approve", "reject", "request_changes"):
        raise ValueError(f"action must be approve/reject/request_changes, got {action!r}")
    cluster = _get_owned_cluster(db, tenant_id, content_cluster_id)
    if cluster.status != "candidate":
        raise ValueError(f"cannot review a cluster with status {cluster.status!r} -- must be 'candidate'")
    cluster.status = {"approve": "approved", "reject": "rejected", "request_changes": "changes_requested"}[action]
    cluster.reviewed_at = datetime.utcnow()
    cluster.reviewed_by = reviewed_by
    cluster.review_note = note
    db.commit()
    return cluster


PILLAR_DRAFT_PROMPT = """Write the real, publishable Master Pillar Page for {business_name}.

Title: {title}
Primary keyword: {primary_keyword}
Search intent this page answers: {search_intent}
Core narrative: {core_narrative}
Real H2 section outline to follow, in order:
{sections_block}

{business_name}'s real positioning: {positioning}

{expedition_guidance}

Write the FULL page (every section above, each with real substance -- 150-300 words per section, \
1500-2500 words total), in {business_name}'s real voice, grounded in the positioning above. Use \
the real section titles as your H2 headers. Never invent a statistic, named source, or specific \
real-world event as fact -- ground claims in the business's own real point of view instead.

Return JSON exactly:
{{"draft_text": "<the full pillar page, with real H2 headers matching the section outline>"}}"""


def generate_pillar_draft(db: Session, tenant_id: int, content_pillar_id: int) -> dict:
    """Only callable on an approved pillar -- writes the full master page in one pass, following
    its own already-approved section outline exactly (never re-planned here)."""
    pillar = _get_owned_pillar(db, tenant_id, content_pillar_id)
    if pillar.status != "approved":
        return {"status": "not_approved", "reason": f"pillar status is {pillar.status!r}, must be 'approved'"}

    business_context = get_content_business_context(db, tenant_id)
    if not business_context.get("business_name"):
        return {"status": "no_business_context", "reason": "this tenant hasn't set business_name/positioning/audience yet"}

    sections_block = "\n".join(f"{i}. {s}" for i, s in enumerate(pillar.sections, start=1))
    prompt = PILLAR_DRAFT_PROMPT.format(
        business_name=business_context["business_name"], title=pillar.title,
        primary_keyword=pillar.primary_keyword, search_intent=pillar.search_intent or "not specified",
        core_narrative=pillar.core_narrative or "not specified", sections_block=sections_block,
        positioning=business_context["positioning"], expedition_guidance=EXPEDITION_FRAMEWORK_GUIDANCE,
    )

    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=4000)
    except Exception as e:  # noqa: BLE001
        return {"status": "llm_unavailable", "error": str(e)}

    draft_text = response.get("draft_text") if isinstance(response, dict) else None
    if not draft_text:
        return {"status": "discarded", "reason": "no draft_text returned"}

    pillar.draft_text = draft_text
    pillar.draft_generated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok", "draft_text": draft_text}


CLUSTER_DRAFT_PROMPT = """Write a real, publishable blog post (500-700 words) for {business_name} \
-- one sub-blog in a content cluster, linking back to its parent pillar page.

Title: {title}
Keyword: {keyword}
Search intent: {intent}
Angle: {angle}

Parent pillar this links back to: "{pillar_title}" ({pillar_keyword})

{business_name}'s real positioning: {positioning}

{expedition_guidance}

End the piece with this closing hook/CTA, adapted naturally into the piece's own voice (never \
pasted verbatim as a template line): {cta}

Also naturally mention, near the end, that {business_name}'s real offering "{offering_name}" is \
the practical next step for a reader convinced by this piece -- woven into the narrative, never a \
bolted-on sales pitch.

This will be copied and pasted directly into the real publishing tool, never rendered from \
markdown -- so write PLAIN TEXT ONLY: no #, ##, **, __, or - used as formatting, and no em dash \
or en dash anywhere (rewrite the sentence instead).

Return JSON exactly:
{{"draft_text": "<the full sub-blog>"}}"""


def generate_cluster_draft(db: Session, tenant_id: int, content_cluster_id: int) -> dict:
    """Only callable on an approved cluster -- independent of whether the parent pillar's own
    master-page draft has been generated yet (a human may want individual sub-blogs live before
    the pillar page itself is finished)."""
    cluster = _get_owned_cluster(db, tenant_id, content_cluster_id)
    if cluster.status != "approved":
        return {"status": "not_approved", "reason": f"cluster status is {cluster.status!r}, must be 'approved'"}

    pillar = db.get(ContentPillar, cluster.content_pillar_id)
    business_context = get_content_business_context(db, tenant_id)
    if not business_context.get("business_name"):
        return {"status": "no_business_context", "reason": "this tenant hasn't set business_name/positioning/audience yet"}

    prompt = CLUSTER_DRAFT_PROMPT.format(
        business_name=business_context["business_name"], title=cluster.title, keyword=cluster.keyword,
        intent=cluster.intent or "not specified", angle=cluster.angle,
        pillar_title=pillar.title if pillar else "(no parent pillar found)",
        pillar_keyword=pillar.primary_keyword if pillar else "",
        positioning=business_context["positioning"], expedition_guidance=EXPEDITION_FRAMEWORK_GUIDANCE,
        cta=cluster.cta or "a natural closing question", offering_name=cluster.offering_name,
    )

    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=1500)
    except Exception as e:  # noqa: BLE001
        return {"status": "llm_unavailable", "error": str(e)}

    draft_text = response.get("draft_text") if isinstance(response, dict) else None
    if not draft_text:
        return {"status": "discarded", "reason": "no draft_text returned"}

    cluster.draft_text = draft_text
    cluster.draft_generated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok", "draft_text": draft_text}
