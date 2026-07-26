"""
JD-first Discovery -- the flow inversion confirmed by the user and team: search job postings
broadly (across all companies) by role keyword first, then verify firmographic fit on just the
companies behind those postings, instead of discovering by firmographics first and checking for
a signal afterward.

Why this order costs less for the same number of qualified leads (see the cost discussion this
was built from): "has an active Head of Sales/SDR/AE posting right now" is a much rarer, narrower
condition than "matches our revenue/headcount band" -- starting from the rarer condition means
fewer total paid lookups before arriving at a company that's both a real signal AND a real fit.

Two tools, two steps:
1. `sentrion_jobs_search` -- broad, cross-company job search by role keyword ($0.042/result,
   minimum 10 results per call). Returns Company Name + full JD text, but no domain.
2. `crustdata_companydb_search` -- filtered by `company_name` (resolves the actual company AND
   confirms firmographic fit in the same call, avoiding a separate domain-resolution tool).
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Batch, Company
from app.deepline_client import execute_tool, extract_rows
from app.phases.discovery import (
    EMPLOYEE_COUNT_MIN, EMPLOYEE_COUNT_MAX, REVENUE_MIN_USD, REVENUE_MAX_USD,
    SALES_HEADCOUNT_PERCENT_MIN, SALES_HEADCOUNT_PERCENT_MAX, EXCLUDED_COMPANY_TYPES,
    _geography_tier, _industry_classification, _parse_date, _existing_domains,
)
from app.phases.hiring_signal import _classify_role, _classify_signal, _infer_hire_type, TOFU_KEYWORD

JOB_TITLE_KEYWORDS = [
    "Head of Sales", "VP Sales", "VP of Sales", "Director of Sales", "Sales Manager", "CRO",
    "SDR", "Sales Development Representative", "BDR", "Business Development Representative",
    "Account Executive", "Head of Marketing", "Marketing Manager", "Growth Marketing",
    "Head of GTM", "GTM Engineer", "GTM Operations",
]


def search_job_postings(limit: int, search_after: list | None, db: Session) -> tuple[list[dict], list | None, int]:
    """Step 1 -- broad cross-company job search. Returns raw job postings, the pagination
    cursor, and total_jobs_found (for cost/coverage visibility, not billing accounting --
    Sentrion bills per row in `jobs`, which is capped at `limit`)."""
    payload = {
        "jobs_locations": [{"country": "US"}],
        "job_keywords": {"title": JOB_TITLE_KEYWORDS, "mode": "or"},
        "limit": max(limit, 10),  # tool's own minimum
    }
    if search_after:
        payload["search_after"] = search_after

    response = execute_tool("sentrion_jobs_search", payload)
    jobs = extract_rows(response, "jobs")
    raw = response.get("toolResponse", {}).get("raw", {})
    next_search_after = raw.get("search_after") if isinstance(raw, dict) else None
    return jobs, next_search_after, len(jobs)


def _verify_firmographic_fit(company_name: str) -> dict | None:
    """Step 2 -- one combined Crustdata query: resolves the company by name AND confirms it
    meets every one of Elephant Edge's existing hard gates, in the same call. Returns the raw
    Crustdata row if a fit is found, None otherwise."""
    payload = {
        "filters": [
            {"filter_type": "company_name", "type": "(.)", "value": company_name},
            {"filter_type": "hq_country", "type": "=", "value": "USA"},
            {"filter_type": "employee_metrics.latest_count", "type": "=>", "value": EMPLOYEE_COUNT_MIN},
            {"filter_type": "employee_metrics.latest_count", "type": "=<", "value": EMPLOYEE_COUNT_MAX},
            {"filter_type": "estimated_revenue_lower_bound_usd", "type": "=>", "value": REVENUE_MIN_USD},
            {"filter_type": "estimated_revenue_higher_bound_usd", "type": "=<", "value": REVENUE_MAX_USD},
            {"filter_type": "acquisition_status", "type": "!=", "value": "acquired"},
            {"filter_type": "company_type", "type": "not_in", "value": EXCLUDED_COMPANY_TYPES},
        ],
        "limit": 1,
    }
    response = execute_tool("crustdata_companydb_search", payload)
    rows = extract_rows(response, "companies")
    return rows[0] if rows else None


def run_jd_first_discovery(batch_id: int, db: Session, tenant_id: int, target: int = 10, jobs_per_page: int = 10, max_postings_checked: int = 100) -> dict:
    """Entrypoint. Pages through job postings (Step 1), and for each *new* company name seen,
    runs the firmographic confirm/exclude check (Step 2) -- only companies passing both steps
    are saved. Hiring-signal fields are populated directly from the JD text already in hand
    from Step 1, at no extra cost (unlike the old flow, which needed a separate per-company
    job-posting call after Discovery)."""
    seen_domains = _existing_domains(tenant_id, db)
    seen_company_names: set[str] = set()
    kept: list[Company] = []
    postings_checked = 0
    firmographic_checks = 0
    search_after = None

    while len(kept) < target and postings_checked < max_postings_checked:
        jobs, search_after, batch_count = search_job_postings(jobs_per_page, search_after, db)
        if batch_count == 0:
            break
        postings_checked += batch_count

        for job in jobs:
            if len(kept) >= target:
                break
            company_name = job.get("Company Name")
            if not company_name or company_name.lower() in seen_company_names:
                continue
            seen_company_names.add(company_name.lower())

            row = _verify_firmographic_fit(company_name)
            firmographic_checks += 1
            if not row:
                continue  # posting exists, but company doesn't meet firmographic fit -- excluded

            domain = row.get("company_website_domain") or (row.get("domains") or [None])[0]
            if not domain or domain.lower() in seen_domains:
                continue
            seen_domains.add(domain.lower())

            role_distribution_percent = row.get("role_distribution_percent") or {}
            sales_pct = role_distribution_percent.get("Sales", 0) + role_distribution_percent.get("Business Development", 0)
            marketing_pct = role_distribution_percent.get("Marketing", 0)
            if role_distribution_percent and not (SALES_HEADCOUNT_PERCENT_MIN <= sales_pct <= SALES_HEADCOUNT_PERCENT_MAX):
                continue

            industries = row.get("linkedin_industries") or []
            employee_metrics = row.get("employee_metrics") or {}
            title = job.get("Job Title") or ""
            description = job.get("Job Description") or ""
            role = _classify_role(title)

            company = Company(
                batch_id=batch_id,
                name=row.get("company_name", company_name),
                domain=domain,
                industry=", ".join(industries[:3]) if industries else None,
                employee_count=employee_metrics.get("latest_count"),
                location=row.get("hq_location"),
                estimated_revenue_lower_usd=row.get("estimated_revenue_lower_bound_usd"),
                estimated_revenue_higher_usd=row.get("estimated_revenue_higher_bound_usd"),
                last_funding_round_type=row.get("last_funding_round_type") or None,
                last_funding_date=_parse_date(row.get("last_funding_date")),
                crunchbase_total_investment_usd=row.get("crunchbase_total_investment_usd"),
                headcount_growth_12m_percent=employee_metrics.get("growth_12m_percent"),
                sales_headcount_percent=sales_pct if role_distribution_percent else None,
                marketing_headcount_percent=marketing_pct if role_distribution_percent else None,
                geography_tier=_geography_tier(row.get("hq_location")),
                industry_classification=_industry_classification(industries),
                source="jd_first:sentrion_jobs_search+crustdata_companydb_search",
                active_head_of_sales_posting=role is not None,
                active_job_title=title or None,
                buying_signal_checked_at=datetime.utcnow(),  # signal already known from Step 1 -- no separate check needed
            )
            if role:
                hire_type = _infer_hire_type(company, role)
                strength, reasoning = _classify_signal(company, role, hire_type, description)
                if TOFU_KEYWORD in description.lower():
                    reasoning += " [TOFU pipeline keyword found in JD]"
                company.hiring_signal_role = role
                company.hiring_signal_hire_type = hire_type
                company.hiring_signal_strength = strength
                company.hiring_signal_reasoning = reasoning

            db.add(company)
            db.commit()
            db.refresh(company)
            kept.append(company)

        if not search_after:
            break

    return {
        "postings_checked": postings_checked,
        "firmographic_checks_run": firmographic_checks,
        "companies_discovered": len(kept),
    }
