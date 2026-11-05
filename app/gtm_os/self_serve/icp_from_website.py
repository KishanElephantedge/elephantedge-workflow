"""Self-serve landing page (2026-11-04, Majji's request): a visitor submits their own company's
website, we figure out who THEY sell to, then run real discovery against that -- no dashboard,
no login, immediate "here are 10 companies you could reach out to."

TWO-TIER FETCH, 2026-11-05 (real bug found testing against elephantedge.ai itself, the user's
own explicit instruction: "run this website in this feature, and see whether that is parsing the
ICP correctly"). The original single httpx GET worked on MetriFi (server-rendered, 4851 real
chars) but returned only 118 chars for elephantedge.ai -- just the <title> tag and a stray dev
placeholder comment ("Add Property > HTML tag method..."), because the real homepage is a JS-
rendered SPA. The LLM correctly reported low confidence and empty industries on that input --
not an inference bug, a fetch bug: the real content (an explicit "Industries We Champion" section
naming AI & SaaS, Cybersecurity, E-Commerce, Manufacturing, Supply Chain & Logistics -- confirmed
against company-understanding.md's independently-researched ground truth) was never fetched.
JS-rendered marketing sites (Next.js/Framer/Webflow-with-JS) are common, not a one-off edge case,
so this can't stay a hard failure routed straight to the template-upload fallback.

Tier 1 (httpx, free, instant) is tried first and used as-is for any genuinely content-bearing
page. Tier 2 (Deepline's firecrawl_batch_scrape, real but cheap -- ~$0.01/page, see
deepline_client.py's DEEPLINE_TOOL_COST_USD -- real browser rendering, ~20-50s) fires only when
tier 1 looks too thin to be real content, confirmed live to pull the actual JS-rendered page.
MIN_REAL_TEXT_CHARS is deliberately much higher than the old 100-char floor -- 118 chars (a title
plus one stray sentence) slipped through that threshold and produced a confident-looking but
empty ICP instead of triggering the fallback that would have gotten the real content.

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
import time

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
# Below this, a page is treated as "likely JS-rendered, not real content" and falls to Firecrawl
# rather than being handed to the LLM as-is. A title tag alone (elephantedge.ai: 118 chars) must
# fail this check; a real, if terse, homepage (MetriFi: 4851 chars) must pass it comfortably.
MIN_REAL_TEXT_CHARS = 400
_FIRECRAWL_POLL_SECONDS = 3
_FIRECRAWL_TIMEOUT_SECONDS = 50


class WebsiteFetchError(Exception):
    pass


def _fetch_homepage_text_httpx(url: str) -> str:
    """Tier 1: free, instant, works for any server-rendered page. Returns '' (never raises) when
    the page looks too thin to be real content -- the caller decides whether to escalate to
    Firecrawl or treat it as a hard failure; raises WebsiteFetchError only for a genuine fetch
    failure (unreachable, non-HTML, blocked), which Firecrawl is equally unlikely to recover from."""
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
    return text


def _fetch_homepage_markdown_firecrawl(url: str) -> str:
    """Tier 2: real browser rendering via Deepline's firecrawl_batch_scrape, for pages tier 1
    can't read. Genuinely async (confirmed live, 2026-11-05 -- defaultExecutionMode: 'sync' in
    its own schema does NOT mean execute_tool blocks until done; the start call returns
    status:'queued' immediately) -- polls firecrawl_get_batch_scrape_status (free) until the
    response's `raw` becomes a populated list (done) or a dict with status:'failed', or until
    _FIRECRAWL_TIMEOUT_SECONDS runs out. Raises WebsiteFetchError in every failure/timeout case."""
    from app.deepline_client import DeeplineError, DeeplineSpendBlocked, execute_tool

    try:
        start = execute_tool("firecrawl_batch_scrape", {"urls": [url]})
    except (DeeplineSpendBlocked, DeeplineError) as e:
        raise WebsiteFetchError(f"firecrawl could not start for {url}: {e}") from e

    job_id = ((start.get("toolResponse") or {}).get("raw") or {}).get("id")
    if not job_id:
        raise WebsiteFetchError(f"firecrawl did not return a job id for {url}")

    deadline = time.monotonic() + _FIRECRAWL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_FIRECRAWL_POLL_SECONDS)
        try:
            poll = execute_tool("firecrawl_get_batch_scrape_status", {"id": job_id})
        except (DeeplineSpendBlocked, DeeplineError) as e:
            raise WebsiteFetchError(f"firecrawl status check failed for {url}: {e}") from e
        raw = (poll.get("toolResponse") or {}).get("raw")
        if isinstance(raw, list):
            if not raw:
                raise WebsiteFetchError(f"firecrawl returned no pages for {url}")
            markdown = raw[0].get("markdown") or ""
            if not markdown.strip():
                raise WebsiteFetchError(f"firecrawl rendered {url} but returned no text content")
            return markdown
        if isinstance(raw, dict) and raw.get("status") == "failed":
            raise WebsiteFetchError(f"firecrawl job failed for {url}")
        # otherwise still queued/scraping -- keep polling

    raise WebsiteFetchError(f"firecrawl timed out rendering {url} after {_FIRECRAWL_TIMEOUT_SECONDS}s")


def fetch_homepage_text(url: str) -> str:
    """Fetches the given URL and returns its real text content, trying the fast free path first
    and only escalating to real browser rendering when that path looks too thin to be genuine
    content. Raises WebsiteFetchError only when NEITHER tier produces real content -- the caller
    (the /self-serve/infer-icp route) decides how to surface that to the visitor, typically by
    routing them to the template-upload fallback."""
    if not re.match(r"^https?://", url, re.I):
        url = f"https://{url}"

    text = _fetch_homepage_text_httpx(url)
    if len(text) >= MIN_REAL_TEXT_CHARS:
        return text[:_MAX_TEXT_CHARS]

    logger.info("fetch_homepage_text: tier 1 got only %d chars for %s, escalating to firecrawl",
               len(text), url)
    markdown = _fetch_homepage_markdown_firecrawl(url)
    if len(markdown) < MIN_REAL_TEXT_CHARS:
        raise WebsiteFetchError(
            f"{url} returned too little real content even after rendering "
            f"({len(markdown)} chars) -- likely blocked or genuinely empty")
    return markdown[:_MAX_TEXT_CHARS]


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
