"""Self-serve landing page (2026-11-04, Majji's request): a visitor submits their own company's
website, we figure out who THEY sell to, then run real discovery against that -- no dashboard,
no login, immediate "here are 10 companies you could reach out to."

WHY A SYNCHRONOUS SINGLE-PAGE FETCH, NOT A MULTI-PAGE CRAWL. Researched how comparable tools do
this (SignalizeAI, Paminga's aiDiscoverIcps, a public n8n Firecrawl+Gemini workflow) -- the
common pattern is a few pages (home + product/about), not a deep crawl. But Majji's own stated
requirement is "within a minute," and Deepline's firecrawl_crawl is an ASYNC job (start, then
poll for completion) -- too slow for a one-shot landing-page experience. Confirmed on a real test
case first (2026-11-04): MetriFi's own homepage alone states "For credit unions and banks" in
plain text -- the single page a visitor already has open is usually enough signal. Falls back to
Deepline's firecrawl tools later if single-page proves insufficient on more test cases; not
built speculatively for a problem not yet confirmed live.

GROUNDING DISCIPLINE, same as app/phases/partner_icp.py's PARSE_PROMPT: every field is either
genuinely stated/implied by the fetched text, or null -- never a guessed industry or revenue
figure the page never mentioned. Unlike partner_icp.py (which extracts an ICP someone already
wrote down about themselves), this is a strictly harder, more inferential task: the page describes
the VISITOR'S OWN business, and we have to infer who THEIR customers likely are from that
positioning -- so this prompt is explicit that "who we sell to" is being inferred from "what we
do," not read off the page verbatim.
"""
from __future__ import annotations

import logging
import re

import httpx
from sqlalchemy.orm import Session

from app.llm_client import generate_json

logger = logging.getLogger(__name__)

_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
_TIMEOUT_SECONDS = 15
_MAX_TEXT_CHARS = 8000  # plenty for a homepage; keeps the LLM prompt small and cheap


class WebsiteFetchError(Exception):
    pass


def fetch_homepage_text(url: str) -> str:
    """Fetches the given URL and strips it down to visible text. Raises WebsiteFetchError on
    any real failure (unreachable, non-HTML, blocked) -- the caller decides how to surface that
    to the visitor; this never silently returns empty text pretending it succeeded."""
    if not re.match(r"^https?://", url, re.I):
        url = f"https://{url}"

    try:
        resp = httpx.get(url, headers=_UA, timeout=_TIMEOUT_SECONDS, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise WebsiteFetchError(f"could not fetch {url}: {e}") from e

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type.lower():
        raise WebsiteFetchError(f"{url} did not return HTML (content-type: {content_type!r})")

    html = resp.text
    # Strip script/style blocks before stripping tags, or their contents (JS/CSS source) end up
    # as noise in the extracted text.
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;|&#39;|&quot;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) < 100:
        raise WebsiteFetchError(f"{url} returned almost no visible text ({len(text)} chars) -- likely JS-rendered")

    return text[:_MAX_TEXT_CHARS]


INFER_PROMPT = """You are looking at the homepage text of a real company's website. Your job has
two parts:

1. Summarize in plain language what THIS company (the one whose website this is) actually does.
2. Infer who THIS company's own ideal customers likely are -- not who visits their site, but who
   THEY would sell to. Base this on their stated positioning, named customer examples, case
   studies, or industry language on the page -- never invent a customer type the page gives no
   real basis for.

WEBSITE TEXT:
{page_text}

Rules:
- "industries" are the industries of the CUSTOMERS this company would sell to, inferred from
  their positioning (e.g. a company that "designs websites for credit unions and banks" has
  customers in "credit unions" / "banking" -- short lowercase keywords, not full sentences).
- Only state a revenue or headcount band if the page gives a real basis for it (e.g. "we work
  with 50-500 employee companies," or case studies naming company sizes). Otherwise null --
  guessing a band from nothing is worse than leaving it blank.
- geographies: only if the page states or clearly implies one (e.g. "in the US", a named city,
  a currency). Otherwise null.
- confidence: "high" if the page explicitly states who they sell to (like MetriFi saying "for
  credit unions and banks"), "medium" if inferred from case studies/examples, "low" if inferred
  only from generic positioning language with no concrete evidence.

Return JSON exactly:
{{"company_summary": "<1-2 plain sentences on what this company does>",
  "their_customer_industries": ["..."],
  "revenue_min_usd": <int|null>, "revenue_max_usd": <int|null>,
  "employee_min": <int|null>, "employee_max": <int|null>,
  "geographies": ["..."],
  "confidence": "high"|"medium"|"low",
  "evidence": "<the specific phrase(s) from the page this was based on>"}}"""


def infer_icp_from_website(db: Session, tenant_id: int, url: str) -> dict:
    """The full pipeline: fetch -> infer. Returns the parsed ICP dict plus the raw page text's
    length (for logging/debugging) and the source url actually fetched (after any redirect)."""
    page_text = fetch_homepage_text(url)
    prompt = INFER_PROMPT.format(page_text=page_text)
    result = generate_json(prompt, db, tenant_id, max_tokens=800)
    result["source_url"] = url
    result["page_text_chars"] = len(page_text)
    return result


def to_search_icp(inferred: dict) -> dict:
    """Maps this module's own inference-shaped dict (their_customer_industries, revenue_min_usd,
    ...) onto the exact key names app/gtm_os/sourcing/atoms.py's decompose_icp() reads (industries,
    revenue_min_usd, ...) -- the same dict shape a real partner's stored ICP already has, so it
    can be handed straight to run_icp_filters() with no separate code path. Also used for an ICP
    that came from the uploaded-template path (app/gtm_os/self_serve/template.py), which writes
    these same field names directly rather than through inference."""
    return {
        "industries": inferred.get("their_customer_industries") or inferred.get("industries") or [],
        "revenue_min_usd": inferred.get("revenue_min_usd"),
        "revenue_max_usd": inferred.get("revenue_max_usd"),
        "employee_min": inferred.get("employee_min"),
        "employee_max": inferred.get("employee_max"),
        "geographies": inferred.get("geographies") or [],
        "notes": inferred.get("company_summary"),
    }
