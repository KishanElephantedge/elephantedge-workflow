"""Free pre-check of a discovered company against its own public LinkedIn company page.

Why this exists (2026-09-13, batch 127): four of Elephant Edge's 17 discovered companies were
wrong fits that every existing filter passed, and each one was obvious from the company's public
page. The job-search actor only returns LinkedIn MEMBER count, a bare HQ string and an industry
label, and each of those misled us differently:

    Calabrio    278 members on LinkedIn, but the company declares 501-1,000 employees
    Attio       HQ string was just "London"; _is_us_headquarters() rightly treats a bare city as
                unknown, so it passed. The page's structured data says addressCountry "GB".
    ATALNT      labelled "Software Development", but it is a recruiting firm posting a client's
                "Tire Sales Representative" job -- not a hiring signal about ATALNT's own sales team
    Prolifics   the page is Enable Consulting, "acquired by Prolifics in 2024": a subsidiary of a
                far larger firm, sized as if it were independent

The public page (no login) is free, so this runs BEFORE any paid step -- a company rejected here
never costs a team-composition, decision-maker or revenue lookup.

Fails open: if the page can't be fetched or parsed, the company passes as "unverified". A LinkedIn
block (plausible from a datacenter IP) must degrade to the old behaviour, never to zero discovery.
"""
import json
import logging
import re
import time

import httpx

logger = logging.getLogger("company_profile_check")

_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
_TIMEOUT_SECONDS = 20
# Spacing between page fetches within one run. Was 1.5s -- real 429s hit live on 2026-09-16
# during a burst of Jobo-discovered companies checked back-to-back (Docker, Marmon both rate-
# limited mid-run). Raised to slow the request pattern down; a 429 here silently falls through
# as "unverified" (fetch_public_company_profile returns None), which looks identical to a
# genuinely unreadable page, so a too-fast pace doesn't error loudly -- it just quietly loses
# the free verification step it exists to provide.
PACING_SECONDS = 4.0

_US_COUNTRY_CODES = {"US", "USA"}

# A recruiting or staffing firm's job postings are usually for its CLIENTS, so they say nothing
# about the firm's own sales hiring. Known trade-off: a company selling recruiting SOFTWARE would
# also be caught; for our ICPs that is a small, acceptable loss next to paying to work a false
# signal like ATALNT's.
_RECRUITING_PATTERN = re.compile(
    r"\b(recruit(?:ing|ment|ers?)|staffing|talent acquisition|executive search|headhunt\w*)\b", re.I
)
_RECRUITING_INDUSTRIES = {"staffing and recruiting", "human resources services", "executive search services"}

# Phrases that mean the company page belongs to a unit of a larger organisation. Deliberately
# narrow -- "part of" alone appears in ordinary marketing copy ("part of your team").
_SUBSIDIARY_PATTERN = re.compile(
    r"\b(acquired by|a subsidiary of|wholly[- ]owned subsidiary|now part of|a division of|is a part of|a member of the .{2,40} family of companies)\b",
    re.I,
)

# Words that mean a nearby "acquired by" is about somebody's PAST company, not this one. Found live:
# Ent's page says "Founded by ..., co-founders of RiskIQ (acquired by Microsoft)" -- an independent
# startup whose founders had a prior exit, which a bare phrase match flagged as a subsidiary.
_PRIOR_COMPANY_CONTEXT = re.compile(r"\b(founders?|co-?founders?|previously|formerly|alumni|veterans? of|team behind|built and sold|exited)\b", re.I)

_last_fetch_at = 0.0


def _describes_this_company_as_subsidiary(about: str) -> bool:
    for m in _SUBSIDIARY_PATTERN.finditer(about):
        before = about[max(0, m.start() - 120):m.start()]
        if before.count("(") > before.count(")"):
            continue  # inside a parenthetical aside, e.g. "RiskIQ (acquired by Microsoft)"
        if _PRIOR_COMPANY_CONTEXT.search(before):
            continue
        return True
    return False


def _text_of_dd(html: str, test_id: str) -> str | None:
    m = re.search(rf'data-test-id="{test_id}".*?<dd[^>]*>\s*(.*?)\s*</dd>', html, re.S)
    if not m:
        return None
    return re.sub(r"<[^>]+>|\s+", " ", m.group(1)).strip() or None


def _parse_size_band(text: str | None) -> tuple[int, int | None] | None:
    """'501-1,000 employees' -> (501, 1000); '10,001+ employees' -> (10001, None)."""
    if not text:
        return None
    m = re.search(r"([\d,]+)\s*(?:-\s*([\d,]+)|\+)", text)
    if not m:
        return None
    low = int(m.group(1).replace(",", ""))
    high = int(m.group(2).replace(",", "")) if m.group(2) else None
    return low, high


def fetch_public_company_profile(linkedin_url: str | None) -> dict | None:
    """Returns {country, size_band, industry, about} from the public page, or None when the page
    could not be read. Any individual field may be None -- only a readable page is required."""
    global _last_fetch_at
    if not linkedin_url or "linkedin.com/company/" not in linkedin_url:
        return None

    wait = PACING_SECONDS - (time.monotonic() - _last_fetch_at)
    if wait > 0:
        time.sleep(wait)
    try:
        response = httpx.get(linkedin_url, headers=_UA, follow_redirects=True, timeout=_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        logger.warning("company profile fetch failed for %s: %s", linkedin_url, e)
        return None
    finally:
        _last_fetch_at = time.monotonic()
    if response.status_code != 200:
        logger.warning("company profile fetch for %s returned %s", linkedin_url, response.status_code)
        return None
    html = response.text

    org: dict = {}
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in nodes if isinstance(nodes, list) else []:
            if isinstance(node, dict) and node.get("@type") == "Organization":
                org = node
                break
        if org:
            break

    size_text = _text_of_dd(html, "about-us__size")
    meta = re.search(r'<meta name="description" content="([^"]*)"', html)
    about = " ".join(filter(None, [org.get("description"), org.get("slogan"), meta.group(1) if meta else None]))

    if not org and not size_text and not about:
        return None  # a login wall or an unexpected page shape -- nothing trustworthy to judge

    address = org.get("address") if isinstance(org.get("address"), dict) else {}
    return {
        "country": (address.get("addressCountry") or "").strip().upper() or None,
        "size_band": _parse_size_band(size_text),
        "industry": _text_of_dd(html, "about-us__industry"),
        "about": about or None,
    }


def profile_rejection_reason(profile: dict | None, employee_min: int | None, employee_max: int | None) -> str | None:
    """The rejection-breakdown key for a company that should not be kept, or None to keep it.
    A None profile (unreadable page) always keeps the company -- see module docstring."""
    if not profile:
        return None

    if profile["country"] and profile["country"] not in _US_COUNTRY_CODES:
        return "non_us_headquarters"

    band = profile["size_band"]
    # TOO BIG only, never too small. The two directions have opposite reliability:
    #   - LinkedIn member count UNDERSTATES larger, older companies (Calabrio: 278 members, declares
    #     501-1,000), so a declared band above the search is real evidence of a wrong fit.
    #   - A declared band BELOW the search is usually just stale: Stuut declares 11-50 while 83 of
    #     its people are on LinkedIn -- fast-growing startups rarely update it. The member count the
    #     search already checked is the better evidence there, so it is left to stand.
    if band is not None and employee_max is not None and band[0] > employee_max:
        return "declared_size_out_of_target"

    industry = (profile["industry"] or "").lower()
    about = profile["about"] or ""
    if industry in _RECRUITING_INDUSTRIES or _RECRUITING_PATTERN.search(about):
        return "recruiting_or_staffing_firm"
    if _describes_this_company_as_subsidiary(about):
        return "subsidiary_of_larger_company"
    return None
