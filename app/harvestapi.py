"""HarvestAPI (LinkedIn) through Deepline -- the cheap LinkedIn data layer for the plays.

Billed per requested PAGE, not per result (deepline tools describe, measured 2026-09-27):
    harvestapi_search_jobs         $0.001 / page of 25 postings
    harvestapi_get_job             $0.001 / posting (full description)
    harvestapi_get_company         $0.003 / company (exact headcount, industry, HQ, website)
    harvestapi_search_leads        $0.07  / page of 25 profiles (Sales Navigator-style filters)
    harvestapi_search_posts        $0.003 / page of 50 posts
    harvestapi_get_post_comments   $0.003 / page of up to 100 commenters
The Apify actors these replace bill per item ($0.002-0.005 each), 20-100x more for the same data.

Every call goes through deepline_client.execute_tool, so inside a spend_scope it is reserved
against the tenant's combined daily cap and the run cap before it is made. These helpers only
fetch and parse -- deciding who is a fit stays with the plays' Qualifier.
"""
from __future__ import annotations

from app.deepline_client import execute_tool


def _raw(response: dict) -> dict:
    raw = (response or {}).get("toolResponse", {}).get("raw") or {}
    if isinstance(raw, dict) and isinstance(raw.get("output"), dict):
        raw = raw["output"]
    return raw if isinstance(raw, dict) else {}


def _elements(response: dict) -> tuple[list[dict], dict]:
    raw = _raw(response)
    return [e for e in (raw.get("elements") or []) if isinstance(e, dict)], raw.get("pagination") or {}


def universal_name(linkedin_company_url: str | None) -> str | None:
    if not linkedin_company_url or "/company/" not in linkedin_company_url:
        return None
    return linkedin_company_url.rstrip("/").rsplit("/company/", 1)[-1].split("?")[0] or None


def search_jobs(title: str, location: str = "United States", posted: str = "week", page: int = 1) -> list[dict]:
    """-> [{job_id, title, url, posted_at, company_name, company_linkedin_url, company_universal_name, location}]"""
    elements, _ = _elements(execute_tool("harvestapi_search_jobs", {
        "search": title, "location": location, "postedLimit": posted, "sortBy": "date", "page": page}))
    jobs = []
    for e in elements:
        company = e.get("company") if isinstance(e.get("company"), dict) else {}
        jobs.append({
            "job_id": str(e.get("id") or ""), "title": e.get("title"), "url": e.get("url"),
            "posted_at": e.get("postedDate"), "company_name": company.get("name"),
            "company_linkedin_url": company.get("linkedinUrl"),
            "company_universal_name": company.get("universalName") or universal_name(company.get("linkedinUrl")),
            "location": (e.get("location") or {}).get("linkedinText") if isinstance(e.get("location"), dict) else e.get("location"),
        })
    return jobs


def get_job(job_id: str) -> dict:
    """Full posting, including its description text."""
    raw = _raw(execute_tool("harvestapi_get_job", {"jobId": job_id}))
    return raw.get("element") if isinstance(raw.get("element"), dict) else raw


def get_company(universal: str) -> dict | None:
    """-> {name, employee_count, industry, hq_country, hq_text, website, description, linkedin_url} or None."""
    raw = _raw(execute_tool("harvestapi_get_company", {"universalName": universal}))
    e = raw.get("element")
    if not isinstance(e, dict):
        return None
    industries = e.get("industries") or []
    locations = [l for l in (e.get("locations") or []) if isinstance(l, dict)]
    hq = next((l for l in locations if l.get("headquarter")), locations[0] if locations else {})
    return {
        "name": e.get("name"),
        "employee_count": e.get("employeeCount") if isinstance(e.get("employeeCount"), int) else None,
        "industry": industries[0].get("name") if industries and isinstance(industries[0], dict) else None,
        "hq_country": hq.get("country"),
        "hq_text": ", ".join(str(hq.get(k)) for k in ("city", "geographicArea", "country") if hq.get(k)),
        "website": e.get("website"),
        "description": (e.get("description") or "")[:1500],
        "linkedin_url": e.get("linkedinUrl"),
        "founded": (e.get("foundedOn") or {}).get("year") if isinstance(e.get("foundedOn"), dict) else None,
    }


def search_leads(page: int = 1, **filters) -> list[dict]:
    """Sales Navigator-style people search. filters use HarvestAPI's names (currentCompanies,
    currentJobTitles, companyHeadcount, locations, ...), comma-joined strings.
    -> [{first_name, last_name, linkedin_url, title, company_name, company_id, company_linkedin_url, location, headline}]"""
    payload = {k: v for k, v in filters.items() if v}
    payload["page"] = page
    elements, _ = _elements(execute_tool("harvestapi_search_leads", payload))
    people = []
    for e in elements:
        pos = next((p for p in (e.get("currentPositions") or []) if isinstance(p, dict) and p.get("current", True)), {})
        people.append({
            "first_name": e.get("firstName"), "last_name": e.get("lastName"), "linkedin_url": e.get("linkedinUrl"),
            "title": pos.get("title"), "company_name": pos.get("companyName"), "company_id": pos.get("companyId"),
            "company_linkedin_url": pos.get("companyLinkedinUrl"), "headline": (e.get("summary") or "")[:300],
            "location": (e.get("location") or {}).get("linkedinText") if isinstance(e.get("location"), dict) else None,
        })
    return people


def search_posts(query: str, posted: str = "week", page: int = 1) -> list[dict]:
    elements, _ = _elements(execute_tool("harvestapi_search_posts", {"search": query, "postedLimit": posted, "page": page}))
    return elements


def get_post_comments(post_url: str, page: int = 1) -> list[dict]:
    elements, _ = _elements(execute_tool("harvestapi_get_post_comments", {"post": post_url, "page": page}))
    return elements
