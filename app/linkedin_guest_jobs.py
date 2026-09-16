"""Free LinkedIn job discovery via the public "jobs-guest" endpoints -- no login, no API key,
no account at risk (this is the same public search LinkedIn serves to a logged-out browser at
linkedin.com/jobs/search, just called directly). Tested from a home connection 2026-09-13
(TODO.md Section 5): 110 postings over 11 pages without throttling, job detail returns the full
description, and the company page returns headcount.

UNKNOWN THAT DECIDES WHETHER THIS IS USABLE IN PRODUCTION: Render's datacenter IP range may be
blocked by LinkedIn even though a home IP isn't -- untested until this module runs from Render
itself. `linkedin_guest_diagnostic()` below exists to answer exactly that question cheaply (one
real request, zero Apify cost) before anything is wired to depend on this as a real discovery
source. Do not build the Apify-replacement discovery path on top of this until that diagnostic
comes back clean on a real Render deploy.

No server-side headcount/industry filter exists on this endpoint (confirmed live) -- unlike the
paid fantastic-jobs actor, which takes organization_employees_gte/lte and industry directly.
So the real pipeline order this replaces is: guest search (free) -> company page fetch (free,
gives headcount) -> apply the SAME filters apify_discovery.py already applies locally -> job
detail fetch (free) only for survivors. This module intentionally does ONLY the fetch/parse
layer -- filtering stays in apify_discovery.py so there is exactly one place that decides what
counts as in-ICP, same as the existing profile_rejection_reason() discipline."""

import re

import httpx

# Real, public, unauthenticated endpoint LinkedIn's own logged-out job search page calls.
GUEST_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
GUEST_JOB_DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

# A real browser User-Agent -- LinkedIn's guest endpoints reject requests with no UA at all
# (confirmed live), but do NOT require session cookies or any auth header.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

_JOB_ID_RE = re.compile(r"/jobs/view/[^/]*-(\d+)")


class LinkedInGuestError(Exception):
    pass


def linkedin_guest_diagnostic(timeout: int = 20) -> dict:
    """The one real question this whole module exists to answer: from THIS deployment's actual
    outbound IP, does LinkedIn's guest search endpoint respond normally, get rate-limited, or
    get blocked outright? One real request, no keyword/location tuning -- a generic query is
    enough to tell blocked from working. Never raises; every outcome (including a network
    error) is a real, reportable answer, not a crash."""
    try:
        response = httpx.get(
            GUEST_SEARCH_URL,
            params={"keywords": "software engineer", "location": "United States", "start": 0},
            headers=_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as e:
        return {"reachable": False, "status_code": None, "error": str(e), "postings_found": 0}

    postings_found = len(_JOB_ID_RE.findall(response.text)) if response.status_code == 200 else 0
    return {
        "reachable": True,
        "status_code": response.status_code,
        "postings_found": postings_found,
        "likely_blocked": response.status_code in (403, 429) or (response.status_code == 200 and postings_found == 0),
        "response_length": len(response.text),
    }


def search_guest_jobs(keywords: str, location: str = "United States", pages: int = 1, timeout: int = 20) -> list[dict]:
    """Returns a list of {job_id, title, company_name, company_linkedin_url, location, listed_at}
    dicts parsed from the guest search HTML fragment (this endpoint returns an HTML list, not
    JSON -- confirmed live, same as every public JobSpy-style scraper for this same endpoint).
    Raises LinkedInGuestError only on a real fetch failure; a page with zero results returns []."""
    from bs4 import BeautifulSoup  # local import -- only needed on this path, same convention
    # as other optional-dependency imports in this codebase (e.g. feedparser in rss_client.py).

    results: list[dict] = []
    for page in range(pages):
        try:
            response = httpx.get(
                GUEST_SEARCH_URL,
                params={"keywords": keywords, "location": location, "start": page * 10},
                headers=_HEADERS,
                timeout=timeout,
                follow_redirects=True,
            )
        except httpx.HTTPError as e:
            raise LinkedInGuestError(f"guest search request failed: {e}") from e
        if response.status_code != 200:
            raise LinkedInGuestError(f"guest search failed ({response.status_code}): {response.text[:300]}")

        soup = BeautifulSoup(response.text, "html.parser")
        cards = soup.select("li")
        if not cards:
            break
        for card in cards:
            link = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
            if not link:
                continue
            href = link.get("href") or ""
            m = _JOB_ID_RE.search(href)
            job_id = m.group(1) if m else None
            title_el = card.select_one("h3.base-search-card__title")
            company_el = card.select_one("h4.base-search-card__subtitle a, h4.base-search-card__subtitle")
            location_el = card.select_one("span.job-search-card__location")
            listed_el = card.select_one("time")
            results.append({
                "job_id": job_id,
                "title": title_el.get_text(strip=True) if title_el else None,
                "company_name": company_el.get_text(strip=True) if company_el else None,
                "company_linkedin_url": company_el.get("href") if company_el and company_el.name == "a" else None,
                "location": location_el.get_text(strip=True) if location_el else None,
                "listed_at": listed_el.get("datetime") if listed_el else None,
            })
        if len(cards) < 10:
            break  # last page
    return results
