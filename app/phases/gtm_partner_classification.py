"""GTM Partner Categorization -- answers the lead's actual question about the Targets watch-list:
"these are all different industries, it doesn't make sense to go after all of them the same way --
which industry is each one IN, and which industry do THEY sell to, so we can spot companies in
different (non-competing) industries selling to the same buyer, and introduce them to each other
as referral partners." Example the lead gave: a legal-industry company selling to credit unions.

GROUNDING (no fabrication): a profile is classified from up to three REAL evidence sources, never
a guess from a bare name/URL:
  1. `company` name on file for the profile.
  2. Google's AI Overview for "{company} company industry who do they sell to customers" -- the
     SAME already-paid, already-tested search_google_ai_overview() (app/apify_client.py) used by
     free_decision_maker.py and reverse_discovery.py, which grounds its answer in real indexed
     web content (a company's own website, LinkedIn, press, etc.) -- not a guess, and not a new
     integration. Confirmed non-deterministic in practice elsewhere in this codebase, so one retry
     on an empty result follows the same precedent as free_decision_maker.py's own usage.
  3. The real text of the profile's own captured posts (LinkedinMonitorSignal.post_text, now
     populated for every watched profile since the unfiltered-capture fix) -- direct, first-party
     evidence when available, layered on top of (2)'s broader web summary.
A profile with NONE of the three (no company name, no Google Overview result, no captured posts)
is reported as "insufficient_evidence" without ever calling the classification LLM on empty
context.

Same LLM-call shape as classify_relevance() (linkedin_monitor.py): one bounded generate_json()
call, a strict JSON schema, and an explicit "insufficient_evidence" output the model itself can
choose when the given text doesn't clearly indicate an industry or a buyer -- the model is
instructed to say so rather than invent a plausible-sounding guess."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.apify_client import ApifyError, search_google_ai_overview
from app.apify_client import _get_api_key as _get_apify_api_key
from app.claude_client import ClaudeError
from app.db.models import LinkedinMonitorProfile, LinkedinMonitorSignal
from app.llm_client import GeminiError, generate_json

# How many of a profile's own most recent captured posts to use as evidence -- enough real
# signal to infer a pattern, bounded so the prompt (and cost) doesn't grow unbounded for a
# heavily-active profile.
MAX_POSTS_AS_EVIDENCE = 5

CLASSIFICATION_PROMPT = """You are helping a B2B services company (Elephant Edge) figure out \
which companies in their LinkedIn watch-list could become REFERRAL PARTNERS -- i.e. companies \
in a DIFFERENT industry from Elephant Edge, serving a similar or adjacent buyer, worth \
introducing to each other. Example the lead gave: a legal-industry company that sells to credit \
unions.

You are given real, already-collected information about ONE watched profile/company. Do not \
invent anything not supported by this text.

Company/profile name on file: {company_name}

Web search summary about this company (Google AI Overview, may be empty if nothing was found):
\"\"\"{web_summary}\"\"\"

Real recent post text from this profile (most recent first, may be empty if nothing captured yet):
\"\"\"{post_text}\"\"\"

From ONLY the text above, determine:
1. What INDUSTRY this company/person's company operates in (e.g. "legal services", "commercial \
real estate", "cybersecurity consulting"). Be specific, not just "B2B" or "professional services" \
unless the text truly gives nothing more specific.
2. WHO they sell to / their typical customer or buyer type (e.g. "credit unions", "mid-market \
manufacturers", "SaaS companies with 50-200 employees"). This is often stated more explicitly \
than #1 -- look for who they mention serving, helping, or working with.

If the given text is too thin/generic to confidently determine EITHER field, say so honestly --\
do not guess a plausible-sounding industry just because one wasn't given.

Return JSON exactly:
{{"status": "classified" | "insufficient_evidence", "industry": "<string or null>", "sells_to": "<string or null>", "confidence": "high" | "medium" | "low" | null, "reasoning": "<one or two sentences citing what in the text supports this, or why it's insufficient>"}}"""


def _web_overview(db: Session, tenant_id: int, company_name: str) -> str:
    """Google's AI Overview for a targeted query -- the same already-paid, already-tested
    search_google_ai_overview() (app/apify_client.py) free_decision_maker.py/reverse_discovery.py
    already use, grounded in real indexed web content. One retry on an empty result, same
    precedent as free_decision_maker.py's own usage (confirmed non-deterministic in practice)."""
    if not company_name:
        return ""
    try:
        api_key = _get_apify_api_key(db, tenant_id)
        query = f"{company_name} company industry who do they sell to customers"
        content = search_google_ai_overview(api_key, query)
        if not content:
            content = search_google_ai_overview(api_key, query)
        return content or ""
    except ApifyError:
        return ""


def _gather_evidence(db: Session, tenant_id: int, profile: LinkedinMonitorProfile) -> tuple[str, str, str]:
    """Real evidence only -- the profile's own company-name field, a web search overview about
    that company, and its own most recent captured posts. Never pulls from another profile or
    fabricates text."""
    company_name = profile.company or profile.name or ""
    web_summary = _web_overview(db, tenant_id, company_name)
    posts = (
        db.query(LinkedinMonitorSignal)
        .filter(LinkedinMonitorSignal.tenant_id == tenant_id, LinkedinMonitorSignal.profile_id == profile.id)
        .filter(LinkedinMonitorSignal.post_text.isnot(None))
        .order_by(LinkedinMonitorSignal.created_at.desc())
        .limit(MAX_POSTS_AS_EVIDENCE)
        .all()
    )
    post_text = "\n\n---\n\n".join((p.post_text or "").strip()[:1500] for p in posts if p.post_text)
    return company_name, web_summary, post_text


def classify_profile(db: Session, tenant_id: int, profile: LinkedinMonitorProfile) -> dict:
    """Pure classification of one profile from its own real, already-collected evidence.
    Persists nothing -- see classify_and_save_profile() for the writer. Returns
    status="insufficient_evidence" (never calling the LLM at all) when there is no real text to
    reason from in the first place -- a bare LinkedIn URL with no company name, no web search
    result, and zero captured posts tells us nothing, and asking an LLM to classify nothing would
    just invite a guess."""
    company_name, web_summary, post_text = _gather_evidence(db, tenant_id, profile)
    if not company_name and not web_summary and not post_text:
        return {
            "status": "insufficient_evidence",
            "industry": None, "sells_to": None, "confidence": None,
            "reasoning": "no company name on file, no web search result, and no posts captured yet for this profile",
            "evidence_excerpt": None,
        }

    try:
        result = generate_json(
            CLASSIFICATION_PROMPT.format(
                company_name=company_name or "(unknown)",
                web_summary=web_summary or "(no web search result)",
                post_text=post_text or "(no posts captured yet)",
            ),
            db, tenant_id, max_tokens=400,
        )
    except (GeminiError, ClaudeError) as e:
        return {
            "status": "insufficient_evidence",
            "industry": None, "sells_to": None, "confidence": None,
            "reasoning": f"classifier unavailable: {e}",
            "evidence_excerpt": None,
        }

    status = result.get("status") if result.get("status") in ("classified", "insufficient_evidence") else "insufficient_evidence"
    return {
        "status": status,
        "industry": result.get("industry") if status == "classified" else None,
        "sells_to": result.get("sells_to") if status == "classified" else None,
        "confidence": result.get("confidence") if status == "classified" else None,
        "reasoning": result.get("reasoning"),
        "evidence_excerpt": (
            company_name
            + (" -- web: " + web_summary[:400] if web_summary else "")
            + (" -- posts: " + post_text[:300] if post_text else "")
        ) or None,
    }


def classify_and_save_profile(db: Session, tenant_id: int, profile: LinkedinMonitorProfile) -> LinkedinMonitorProfile:
    """The only writer of a profile's classification fields."""
    result = classify_profile(db, tenant_id, profile)
    profile.classification_status = result["status"]
    profile.industry = result["industry"]
    profile.sells_to = result["sells_to"]
    profile.classification_confidence = result["confidence"]
    profile.classification_reasoning = result["reasoning"]
    profile.classification_evidence_excerpt = result["evidence_excerpt"]
    profile.classified_at = datetime.utcnow()
    db.commit()
    return profile


def run_gtm_partner_classification_sweep(db: Session, tenant_id: int, limit: int = 200, only_unclassified: bool = True) -> dict:
    """Bounded, tenant-scoped. only_unclassified=True (default) skips profiles already classified
    -- re-classifying everything on every call would re-spend LLM calls for no new evidence; call
    with only_unclassified=False to force a refresh after more posts have accumulated. One
    profile's failure never aborts the sweep."""
    query = db.query(LinkedinMonitorProfile).filter(LinkedinMonitorProfile.tenant_id == tenant_id, LinkedinMonitorProfile.active.is_(True))
    if only_unclassified:
        query = query.filter(LinkedinMonitorProfile.classified_at.is_(None))
    profiles = query.order_by(LinkedinMonitorProfile.id).limit(limit).all()

    counts = {"evaluated": 0, "classified": 0, "insufficient_evidence": 0, "failed": 0}
    for profile in profiles:
        try:
            counts["evaluated"] += 1
            result = classify_and_save_profile(db, tenant_id, profile)
            counts[result.classification_status] += 1
        except Exception:  # noqa: BLE001 -- one profile's failure must never block the others
            counts["failed"] += 1

    return counts


def get_partner_matches(db: Session, tenant_id: int) -> dict:
    """Groups classified profiles by real, stored `sells_to` text (exact match, case-insensitive
    -- deliberately no fuzzy/semantic grouping, since two differently-worded buyer descriptions
    being "the same" is itself a judgment call this function doesn't make). A group with 2+
    DIFFERENT industries represented is a real candidate referral-partner cluster: everyone in it
    already sells to the same stated buyer type, from a different industry. Groups with only one
    industry represented are omitted -- that's competition, not a partner match."""
    profiles = (
        db.query(LinkedinMonitorProfile)
        .filter(LinkedinMonitorProfile.tenant_id == tenant_id, LinkedinMonitorProfile.classification_status == "classified")
        .filter(LinkedinMonitorProfile.sells_to.isnot(None))
        .all()
    )

    by_sells_to: dict[str, list[LinkedinMonitorProfile]] = {}
    for p in profiles:
        key = p.sells_to.strip().lower()
        by_sells_to.setdefault(key, []).append(p)

    clusters = []
    for sells_to_key, members in by_sells_to.items():
        industries = {m.industry for m in members if m.industry}
        if len(industries) < 2:
            continue
        clusters.append({
            "sells_to": members[0].sells_to,
            "member_count": len(members),
            "industries_represented": sorted(industries),
            "members": [
                {"id": m.id, "name": m.name, "company": m.company, "industry": m.industry, "linkedin_url": m.linkedin_url, "confidence": m.classification_confidence}
                for m in members
            ],
        })
    clusters.sort(key=lambda c: -c["member_count"])

    classified_count = len(profiles)
    total_active = db.query(LinkedinMonitorProfile).filter(LinkedinMonitorProfile.tenant_id == tenant_id, LinkedinMonitorProfile.active.is_(True)).count()

    return {
        "classified_count": classified_count,
        "total_active_profiles": total_active,
        "clusters": clusters,
    }
