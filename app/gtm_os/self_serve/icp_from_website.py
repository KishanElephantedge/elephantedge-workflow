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
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session

from app.llm_client import generate_json

logger = logging.getLogger(__name__)

_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
_TIMEOUT_SECONDS = 15
# REAL FIX, 2026-11-05: 8000 was too low -- confirmed live on zaylix.si, whose real homepage is
# 11344 chars (fetched fine via the fast httpx path, no Firecrawl needed), truncating off exactly
# the footer section ("Operating Regions Across The Globe: India, United States, Canada, Africa")
# that would have corrected the geography guess. Raised with real headroom for a long page
# rather than tuned to this one site's exact length.
_MAX_TEXT_CHARS = 16000
# Below this, a page is treated as "likely JS-rendered, not real content" and falls to Firecrawl
# rather than being handed to the LLM as-is. A title tag alone (elephantedge.ai: 118 chars) must
# fail this check; a real, if terse, homepage (MetriFi: 4851 chars) must pass it comfortably.
MIN_REAL_TEXT_CHARS = 400
_FIRECRAWL_POLL_SECONDS = 3
_FIRECRAWL_TIMEOUT_SECONDS = 50
_LINKEDIN_COMPANY_RE = re.compile(r"https?://(?:[\w-]+\.)?linkedin\.com/company/[^\s\"'<>)\]]+", re.I)


class WebsiteFetchError(Exception):
    pass


def _find_linkedin_company_url(raw: str) -> str | None:
    """Pulled from the RAW fetched content (HTML or markdown) before it's stripped to plain
    text/truncated -- a plain-text extraction loses href attributes entirely (a link rendered as
    the word "LinkedIn" has no URL left in visible text), and the real URL is usually only a few
    hundred bytes into the page (header/footer social links), nowhere near where a long page
    might get truncated. Used to pull a real, free, declared employee count off the company's own
    LinkedIn page instead of leaving it null -- see infer_icp_from_website()."""
    m = _LINKEDIN_COMPANY_RE.search(raw)
    return m.group(0).rstrip(".,;") if m else None


def page_url_base(url: str) -> str:
    """'zaylix.si' or 'https://zaylix.si/pricing' -> 'https://zaylix.si' -- the scheme+host only,
    for building a sibling page URL like {base}/contact. Same https-default normalization
    fetch_homepage_text() applies to a bare domain."""
    if not re.match(r"^https?://", url, re.I):
        url = f"https://{url}"
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _fetch_homepage_text_httpx(url: str) -> tuple[str, str | None]:
    """Tier 1: free, instant, works for any server-rendered page. Returns ('', None) (never
    raises) when the page looks too thin to be real content -- the caller decides whether to
    escalate to Firecrawl or treat it as a hard failure; raises WebsiteFetchError only for a
    genuine fetch failure (unreachable, non-HTML, blocked), which Firecrawl is equally unlikely
    to recover from."""
    try:
        resp = httpx.get(url, headers=_UA, timeout=_TIMEOUT_SECONDS, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise WebsiteFetchError(f"could not fetch {url}: {e}") from e

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type.lower():
        raise WebsiteFetchError(f"{url} did not return HTML (content-type: {content_type!r})")

    html = resp.text
    linkedin_url = _find_linkedin_company_url(html)
    # Strip script/style blocks before stripping tags, or their contents (JS/CSS source) end up
    # as noise in the extracted text.
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;|&#39;|&quot;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text, linkedin_url


def _fetch_homepage_markdown_firecrawl(url: str) -> tuple[str, str | None]:
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
            return markdown, _find_linkedin_company_url(markdown)
        if isinstance(raw, dict) and raw.get("status") == "failed":
            raise WebsiteFetchError(f"firecrawl job failed for {url}")
        # otherwise still queued/scraping -- keep polling

    raise WebsiteFetchError(f"firecrawl timed out rendering {url} after {_FIRECRAWL_TIMEOUT_SECONDS}s")


def fetch_homepage_text(url: str) -> tuple[str, str | None]:
    """Fetches the given URL and returns (its real text content, its own LinkedIn company URL if
    found), trying the fast free path first and only escalating to real browser rendering when
    that path looks too thin to be genuine content. Raises WebsiteFetchError only when NEITHER
    tier produces real content -- the caller (the /self-serve/infer-icp route) decides how to
    surface that to the visitor, typically by routing them to the template-upload fallback."""
    if not re.match(r"^https?://", url, re.I):
        url = f"https://{url}"

    text, linkedin_url = _fetch_homepage_text_httpx(url)
    if len(text) >= MIN_REAL_TEXT_CHARS:
        return text[:_MAX_TEXT_CHARS], linkedin_url

    logger.info("fetch_homepage_text: tier 1 got only %d chars for %s, escalating to firecrawl",
               len(text), url)
    markdown, fc_linkedin_url = _fetch_homepage_markdown_firecrawl(url)
    if len(markdown) < MIN_REAL_TEXT_CHARS:
        raise WebsiteFetchError(
            f"{url} returned too little real content even after rendering "
            f"({len(markdown)} chars) -- likely blocked or genuinely empty")
    return markdown[:_MAX_TEXT_CHARS], (fc_linkedin_url or linkedin_url)


INFER_PROMPT = """You are looking at the homepage text of a real company's website. Your job has
two parts:

1. Summarize in plain language what THIS company (the one whose website this is) actually does.
2. Infer who THIS company's own ideal customers likely are -- not who visits their site, but who
   THEY would sell to. Base this on their stated positioning, named customer examples, case
   studies, or industry language on the page -- never invent a customer type the page gives no
   real basis for.

WEBSITE TEXT:
{page_text}
{linkedin_context}
Rules:
- "industries" are the industries of the CUSTOMERS this company would sell to, inferred from
  their positioning (e.g. a company that "designs websites for credit unions and banks" has
  customers in "credit unions" / "banking" -- short lowercase keywords, not full sentences).
- Only state a revenue or headcount band if the page gives a real basis for it (e.g. "we work
  with 50-500 employee companies," or case studies naming company sizes). Otherwise null --
  guessing a band from nothing is worse than leaving it blank.
- geographies: list EVERY location the page explicitly states (a named office/region/country, a
  stated "operating regions" or "we serve" list, a named city). If the page lists several
  offices or regions, include ALL of them, not just one. Only fall back to a weaker signal (a
  phone country code, a currency symbol) when NO explicit location is stated anywhere on the
  page. Otherwise null.
- confidence: "high" if the page explicitly states who they sell to (like MetriFi saying "for
  credit unions and banks"), "medium" if inferred from case studies/examples, "low" if inferred
  only from generic positioning language with no concrete evidence.
- "evidence" is for internal verification only, never shown to the company itself -- quote the
  real phrase(s) it's based on, but don't explain your own reasoning process (e.g. never write
  things like "the presence of a phone code implies..." -- just quote what the page said).

Return JSON exactly:
{{"company_summary": "<1-2 plain sentences on what this company does>",
  "their_customer_industries": ["..."],
  "revenue_min_usd": <int|null>, "revenue_max_usd": <int|null>,
  "employee_min": <int|null>, "employee_max": <int|null>,
  "geographies": ["..."],
  "confidence": "high"|"medium"|"low",
  "evidence": "<the specific phrase(s) from the page this was based on>"}}"""

_LINKEDIN_CONTEXT_TEMPLATE = """
This company's own LinkedIn page (real, independently verified -- not from the website text
above) states: country={country}, declared employee size band={size_band}, industry={industry}.
If this conflicts with the website text, prefer the LinkedIn data for the company's OWN country/
size (not the customer industries you're inferring -- that still comes from the website's own
positioning).
"""

EMPLOYEE_SIZE_SEARCH_PROMPT = """Search the web for the real, current employee headcount of the
company at this website: {url}

Look on their LinkedIn company page, Crunchbase, news coverage, or any other reliable current
source. A very early-stage startup genuinely may have no public employee count anywhere -- if
you can't find one, say so honestly rather than guessing a number. State the number or range
plainly, and name which source(s) it came from."""

_EMPLOYEE_SIZE_PARSE_PROMPT = """A research assistant was asked to find a company's real employee
headcount via web search. Here is their raw answer:

{search_answer}

Parse this into a single usable range. If multiple sources disagree, use the range that covers
the most plausible cluster of values (ignore a single wild outlier). If the answer says it
couldn't find any public headcount, found is false.

Return JSON exactly:
{{"found": true|false, "employee_min": <int|null>, "employee_max": <int|null>,
  "source_note": "<one short phrase on where this came from>"}}"""


def search_employee_size_via_claude(db: Session, tenant_id: int, url: str) -> dict | None:
    """Two calls: (1) Claude's native web_search tool does the actual research (real queries,
    real sources -- confirmed live against Ramp: a believable multi-source clustered range), then
    (2) a cheap, search-free follow-up parses that prose answer into a clean {employee_min,
    employee_max} -- more reliable than asking the search call itself to also hit a strict JSON
    shape, which tends to narrate its research instead. Returns None (never raises) on any
    failure; this is a bonus lookup, not a required step."""
    from app.claude_client import ClaudeError, call_claude_messages
    from app.llm_client import generate_json

    try:
        response = call_claude_messages(
            messages=[{"role": "user", "content": EMPLOYEE_SIZE_SEARCH_PROMPT.format(url=url)}],
            db=db, tenant_id=tenant_id,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            max_tokens=1000,
        )
        search_answer = "\n".join(
            block.get("text", "") for block in response.get("content", []) if block.get("type") == "text")
        if not search_answer.strip():
            return None
        return generate_json(_EMPLOYEE_SIZE_PARSE_PROMPT.format(search_answer=search_answer), db, tenant_id,
                             max_tokens=300)
    except ClaudeError as e:
        logger.info("self-serve employee-size Claude web search skipped for %s: %s", url, e)
        return None


def infer_icp_from_website(db: Session, tenant_id: int, url: str) -> dict:
    """The full pipeline: fetch -> (free LinkedIn lookup, if the page links to one) -> infer.
    Returns the parsed ICP dict plus the raw page text's length (for logging/debugging) and the
    source url actually fetched (after any redirect).

    REAL FIX, 2026-11-05 (found live testing zaylix.si): the LLM correctly refuses to guess an
    employee count the website never states -- but a free, already-built mechanism
    (app/phases/company_profile_check.py's fetch_public_company_profile, used elsewhere in this
    codebase to read a company's own public LinkedIn page) can often get the REAL declared
    headcount band for free, no paid API, when the website links to its own LinkedIn company
    page (most do, in the header/footer social links). Also grounds the company's own country
    more reliably than guessing from a phone number's country code -- the same test case showed
    the LLM inferring India purely from a '+91' prefix instead of using the page's own stated
    headquarters."""
    page_text, linkedin_url = fetch_homepage_text(url)

    # Free, bounded bonus: confirmed live (zaylix.si) its own LinkedIn link AND its full list of
    # operating regions ("India, United States, Canada, Africa") live only on /contact, not the
    # homepage -- raising _MAX_TEXT_CHARS alone didn't help, because the content genuinely isn't
    # on the page being fetched at all. One extra free httpx GET (fails silently: wrong guess at
    # the URL, no /contact page, a redirect loop -- any of it, since this is a bonus, not a
    # required step), and its real text is appended to what the LLM sees, not just mined for a
    # LinkedIn link.
    try:
        base = page_url_base(url)
        contact_text, contact_linkedin = _fetch_homepage_text_httpx(f"{base}/contact")
        if len(contact_text) >= MIN_REAL_TEXT_CHARS:
            page_text = f"{page_text}\n\n--- /contact page ---\n{contact_text[:4000]}"
        linkedin_url = linkedin_url or contact_linkedin
    except Exception as e:  # noqa: BLE001
        logger.info("self-serve /contact lookup skipped for %s: %s", url, e)

    linkedin_context = ""
    linkedin_profile = None
    if linkedin_url:
        try:
            from app.phases.company_profile_check import fetch_public_company_profile

            linkedin_profile = fetch_public_company_profile(linkedin_url)
        except Exception as e:  # noqa: BLE001 -- this is a free bonus lookup, never a dependency
            logger.warning("self-serve LinkedIn profile lookup failed for %s: %s", linkedin_url, e)
        if linkedin_profile:
            linkedin_context = _LINKEDIN_CONTEXT_TEMPLATE.format(
                country=linkedin_profile.get("country") or "unknown",
                size_band=linkedin_profile.get("size_band") or "unknown",
                industry=linkedin_profile.get("industry") or "unknown")

    prompt = INFER_PROMPT.format(page_text=page_text, linkedin_context=linkedin_context)
    result = generate_json(prompt, db, tenant_id, max_tokens=800)

    # Real LinkedIn data overrides only when the LLM found nothing from the website text itself
    # -- a page that explicitly states its own size band is still better evidence than LinkedIn's
    # member count, which is well-documented elsewhere in this codebase to understate fast-growing
    # companies (see company_profile_check.py's own profile_rejection_reason comment).
    if linkedin_profile and linkedin_profile.get("size_band") and result.get("employee_min") is None:
        result["employee_min"], result["employee_max"] = linkedin_profile["size_band"]

    # LAST RESORT: a real web search. Gemini's native "Grounding with Google Search" was tried
    # first (2026-11-05) but confirmed, with two different API keys, to be gated behind a Google
    # Cloud billing setting neither key has -- same 429 with or without search, isolating it as
    # a plan/billing block, not a quota or retry problem. Elephant Edge has real Anthropic
    # credit, so this uses Claude's own native web_search tool instead -- confirmed live (Ramp:
    # real multi-source search, a believable clustered range) before being wired in here. Only
    # runs when every earlier signal (website text, LinkedIn scrape) came back with nothing -- it
    # costs real money (web_search is ~$0.01/search plus normal token cost, and this prompt lets
    # the model run a few), and a genuinely early-stage company with no public headcount anywhere
    # should still come back null rather than loop forever trying to find one.
    if result.get("employee_min") is None:
        try:
            search_result = search_employee_size_via_claude(db, tenant_id, url)
            if search_result and search_result.get("found") and search_result.get("employee_min") is not None:
                result["employee_min"] = search_result["employee_min"]
                result["employee_max"] = search_result.get("employee_max")
                result["employee_size_source"] = search_result.get("source_note")
        except Exception as e:  # noqa: BLE001 -- a bonus lookup must never break the main result
            logger.warning("self-serve employee-size web search failed for %s: %s", url, e)

    result["source_url"] = url
    result["page_text_chars"] = len(page_text)
    result["linkedin_url"] = linkedin_url
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
