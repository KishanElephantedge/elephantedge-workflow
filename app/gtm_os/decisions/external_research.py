"""External knowledge for the decision layer.

The system reasons only from its own database, which caps it at whatever this one company has
already tried. A real operator facing "5 replies, 1 meeting requested, 0 meetings booked" would go
and read how other people solve that -- and the system should too, rather than re-deriving GTM
practice from 62 rows of its own data.

DISCIPLINE, because this is the easiest place in the whole codebase to start fabricating:
  - Research is triggered by a REAL constraint the diagnosis already found, never by a topic
    someone thought sounded interesting. No constraint, no search, no spend.
  - Only the fetched text is summarised. The model is shown search results and asked what they
    say -- never asked what it already believes about sales.
  - Every practice returned must carry the source URL it came from, and anything unattributed is
    dropped (_verified_practices). An unsourced "best practice" is indistinguishable from the
    model's own prior, which is exactly what this exists to avoid.
  - Bounded and budget-guarded: at most MAX_QUERIES_PER_RUN searches, each checked against the
    real Apify budget first, cached per constraint per day so a daily sweep cannot re-buy the
    same answer.
"""

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.apify_budget_guard import STATUS_ALLOWED, check_apify_budget
from app.apify_client import GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD, ApifyError
from app.apify_client import _get_api_key as _get_apify_api_key
from app.apify_client import search_google_organic_results
from app.db.models import Parameter
from app.llm_client import generate_json

logger = logging.getLogger(__name__)

MAX_QUERIES_PER_RUN = 2
RESEARCH_CACHE_PARAMETER_KEY = "gtm_os_external_research_cache"
CACHE_TTL_HOURS = 24

# One question per constraint the diagnosis can actually raise. Deliberately phrased as the
# operational question a practitioner would search, not as an abstract topic.
CONSTRAINT_RESEARCH_QUESTIONS: dict[str, str] = {
    "no_meetings_booked": "how to convert positive LinkedIn replies into booked sales meetings B2B",
    "no_revenue_earned": "how early stage B2B companies close their first deals fast outbound",
    "low_outbound_volume": "how many LinkedIn outbound touches per day B2B pipeline benchmarks",
    "reply_concentration_in_one_variant": "how to scale a winning cold outreach message variant without burning it",
    "offering_not_dispatchable": "how to prioritise which service offering to sell when several fit B2B",
    "revenue_not_recorded": "sales pipeline hygiene recording deal outcomes CRM discipline",
}


def _load_cache(db: Session, tenant_id: int) -> dict:
    param = db.query(Parameter).filter(Parameter.tenant_id == tenant_id, Parameter.key == RESEARCH_CACHE_PARAMETER_KEY).first()
    return param.value if param and isinstance(param.value, dict) else {}


def _save_cache(db: Session, tenant_id: int, cache: dict) -> None:
    param = db.query(Parameter).filter(Parameter.tenant_id == tenant_id, Parameter.key == RESEARCH_CACHE_PARAMETER_KEY).first()
    if param:
        param.value = cache
    else:
        db.add(Parameter(tenant_id=tenant_id, key=RESEARCH_CACHE_PARAMETER_KEY, value=cache,
                         description="Cached external research per constraint (see gtm_os/decisions/external_research.py)"))
    db.commit()


def _fresh(entry: dict) -> bool:
    try:
        return datetime.utcnow() - datetime.fromisoformat(entry["fetched_at"]) < timedelta(hours=CACHE_TTL_HOURS)
    except Exception:  # noqa: BLE001 -- a malformed cache entry is simply stale
        return False


SUMMARY_PROMPT = """Below are real web search results about this operational problem:

PROBLEM: {question}

SEARCH RESULTS:
{results}

Extract concrete, applicable practices that these results actually state.

Rules:
- Use ONLY what the search results say. Do NOT add practices from your own knowledge.
- Every practice MUST include the exact source_url it came from, copied from the results.
- Prefer specific, testable practices ("ask for a 15-minute slot with two named times") over \
generic advice ("build rapport").
- If the results contain nothing concrete, return an empty list rather than filling it.

Return JSON exactly:
{{"practices": [{{"practice": "<specific practice>", "why_it_works": "<what the source says>", \
"source_url": "<url copied from results>"}}]}}"""


def _verified_practices(parsed: dict, results: list[dict]) -> list[dict]:
    """Keeps only practices whose source_url really appears in the fetched results -- an
    unattributed practice is the model's own prior wearing a citation, and is dropped."""
    real_urls = {str(r.get("url") or "").strip() for r in results if r.get("url")}
    kept, dropped = [], 0
    for practice in parsed.get("practices") or []:
        url = str(practice.get("source_url") or "").strip()
        if url and any(url.startswith(u) or u.startswith(url) for u in real_urls):
            kept.append(practice)
        else:
            dropped += 1
    if dropped:
        logger.warning("external_research: dropped %d practice(s) with no real source url", dropped)
    return kept


def research_constraints(db: Session, tenant_id: int, constraints: list[dict]) -> dict:
    """Researches the highest-priority constraints the diagnosis found. Never raises."""
    names = [c.get("constraint") for c in constraints if c.get("constraint") in CONSTRAINT_RESEARCH_QUESTIONS]
    if not names:
        return {"status": "skipped", "reason": "no researchable constraint in the diagnosis", "findings": []}

    cache = _load_cache(db, tenant_id)
    findings, spent_queries = [], 0

    for name in names[:MAX_QUERIES_PER_RUN]:
        cached = cache.get(name)
        if cached and _fresh(cached):
            findings.append({**cached, "from_cache": True})
            continue

        question = CONSTRAINT_RESEARCH_QUESTIONS[name]
        budget = check_apify_budget(db, tenant_id, GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD)
        if budget["status"] != STATUS_ALLOWED:
            findings.append({"constraint": name, "status": "skipped", "reason": f"budget: {budget['reason']}", "practices": []})
            continue

        try:
            api_key = _get_apify_api_key(db, tenant_id)
            results = search_google_organic_results(api_key, question, max_pages=1)
            spent_queries += 1
        except ApifyError as e:
            findings.append({"constraint": name, "status": "failed", "reason": str(e), "practices": []})
            continue

        trimmed = [{"url": r.get("url"), "title": r.get("title"), "description": r.get("description")} for r in (results or [])[:8]]
        if not trimmed:
            findings.append({"constraint": name, "status": "no_results", "practices": []})
            continue

        try:
            parsed = generate_json(SUMMARY_PROMPT.format(question=question, results=json.dumps(trimmed, indent=2)), db, tenant_id, max_tokens=1200)
        except Exception as e:  # noqa: BLE001 -- research is additive; its failure must not break the plan
            findings.append({"constraint": name, "status": "failed", "reason": f"summarisation failed: {e}", "practices": []})
            continue

        entry = {
            "constraint": name, "question": question, "status": "succeeded",
            "practices": _verified_practices(parsed, trimmed),
            "fetched_at": datetime.utcnow().isoformat(),
        }
        cache[name] = entry
        findings.append(entry)

    if spent_queries:
        _save_cache(db, tenant_id, cache)
    return {"status": "succeeded", "queries_run": spent_queries, "findings": findings}
