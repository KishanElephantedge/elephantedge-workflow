"""
Apify Discovery -- validated live 2026-08-05/06 as a real second discovery source alongside
jd_first (TheirStack). Same jobs-first flow inversion as jd_first_discovery.py (search job
postings directly, firmographics folded into the same query), but sourced from Apify's
LinkedIn Jobs Scraper actor (fantastic-jobs/advanced-linkedin-job-search-api) instead of
TheirStack -- a real-time, hourly-updated LinkedIn-native index, found live to have a
meaningfully denser and fresher pool than TheirStack at the same hour of day (8/8 and 5/5 real
qualifying companies across two real test days, vs. TheirStack's 2/5 on the intervening real
production day).

Employee band tightened to 25-50 (not the shared 11-50 EMPLOYEE_COUNT_MIN/jd_first band) --
validated live against real LinkedIn-follower-count/founding-year/funding-language proxies
that this band skews meaningfully toward better-funded, more mature companies than 11-24,
since neither Apify nor the free Crustdata identify call expose a real revenue or funding
number to filter on directly.

Industry filter is a required, hard-won detail: Apify's own industryFilter param silently
returns zero results (not an error) if any entry contains a comma -- confirmed live that
"Technology, Information and Internet" alone broke an otherwise-identical query. Only
comma-free LinkedIn industry names are used here for that reason.

No separate Crustdata company_identify call here (unlike jd_first) -- Apify's own
org_linkedin_website/org_linkedin_headcount are already real, LinkedIn-sourced values per
company, so that extra lookup isn't needed.
"""
from sqlalchemy.orm import Session

from app.apify_client import ApifyError, estimate_cost_usd, search_linkedin_jobs
from app.apify_client import _get_api_key as _get_apify_api_key
from app.db.models import Company
from app.phases.discovery import _existing_domains
from app.phases.hiring_signal import (
    TOFU_KEYWORD, _classify_role, _classify_signal, _detect_product_fit_signals, _infer_hire_type,
    assess_team_composition,
)

APIFY_TITLE_SEARCH = [
    "SDR", "BDR", "AE", "Sales Development Representative",
    "Business Development Representative", "Business Development Manager", "Account Executive",
    "Head of Sales", "VP Sales", "VP of Sales", "Director of Sales", "Sales Director",
    "Sales Manager", "CRO", "Chief Revenue Officer", "Head of GTM", "GTM Engineer",
]

# Validated live 2026-08-06 -- see module docstring. Comma-free LinkedIn industry names only.
APIFY_INDUSTRY_FILTER = [
    "Software Development", "Computer Software", "Computer and Network Security",
    "IT Services and IT Consulting", "Data Infrastructure and Analytics",
]

APIFY_EMPLOYEE_MIN = 25
APIFY_EMPLOYEE_MAX = 50


def _normalize_domain(website: str) -> str:
    domain = (website or "").lower().strip()
    domain = domain.replace("https://", "").replace("http://", "")
    domain = domain.split("/")[0]
    return domain.replace("www.", "")


def run_apify_discovery(batch_id: int, db: Session, tenant_id: int, target: int = 5, time_range: str = "7d") -> dict:
    """Entrypoint -- single synchronous Apify actor call (no budget_guard/paging loop like
    jd_first, since the actor's own `limit` already bounds spend deterministically: N results
    costs exactly N*$0.005 + $0.01, no per-page uncertainty).

    time_range default changed from "24h" to "7d" (2026-08-10) -- confirmed live that "24h"
    produced only 1 real posting on an otherwise-normal day, well short of the 5-10/day
    target. "7d" is the actor's own recommended default. Safe for a daily-running cron: we
    already dedup against every domain ever seen (seen_domains, above), not just today's
    batch, so a wider window can only mean re-fetching (and paying to re-check, then
    discarding) postings already seen on a prior day -- never a duplicate Company row."""
    seen_domains = _existing_domains(tenant_id, db)

    try:
        api_key = _get_apify_api_key(db, tenant_id)
        jobs = search_linkedin_jobs(
            api_key,
            title_search=APIFY_TITLE_SEARCH,
            location_search=["United States"],
            organization_employees_gte=APIFY_EMPLOYEE_MIN,
            organization_employees_lte=APIFY_EMPLOYEE_MAX,
            industry_filter=APIFY_INDUSTRY_FILTER,
            time_range=time_range,
            remove_agency=True,
            limit=max(target * 20, 100),  # real hit rate ~15-30% after dedup/team-fit -- oversample
        )
    except ApifyError as e:
        return {
            "companies_discovered": 0, "postings_checked": 0, "rejection_breakdown": {},
            "budget_stopped_early": False, "api_error": str(e), "estimated_cost_usd": 0.0,
        }

    seen_this_run: set[str] = set()
    kept: list[Company] = []
    rejection_counts: dict[str, int] = {}

    for job in jobs:
        if len(kept) >= target:
            break
        domain = _normalize_domain(job.get("org_linkedin_website") or "")
        if not domain or domain in seen_domains or domain in seen_this_run:
            continue
        seen_this_run.add(domain)

        headcount = job.get("org_linkedin_headcount")
        if headcount is not None and not (APIFY_EMPLOYEE_MIN <= headcount <= APIFY_EMPLOYEE_MAX):
            # Real safety net: the actor's own filter has shown occasional stale-bucket
            # mismatches (validated live -- RADICL/QED Investors passed organizationSizeFilter
            # "11-50" despite org_linkedin_headcount 67/75) -- trust the raw number over the
            # filter having already applied it correctly.
            rejection_counts["headcount_out_of_target"] = rejection_counts.get("headcount_out_of_target", 0) + 1
            continue

        title = job.get("title") or ""
        description = job.get("description_text") or ""
        role = _classify_role(title)
        product_fit_categories = _detect_product_fit_signals(description)
        if not role and not product_fit_categories:
            rejection_counts["no_local_classification_match"] = rejection_counts.get("no_local_classification_match", 0) + 1
            continue

        company = Company(
            batch_id=batch_id,
            name=job.get("organization") or "Unknown",
            domain=domain,
            industry=job.get("org_linkedin_industry") or None,
            source="apify:fantastic-jobs_advanced-linkedin-job-search-api",
            active_job_title=title or None,
            product_fit_jd_categories=product_fit_categories or None,
            linkedin_url=job.get("organization_url") or None,
        )

        if role:
            hire_type = _infer_hire_type(company, role)
            strength, reasoning = _classify_signal(company, role, hire_type, description)
            if TOFU_KEYWORD in description.lower():
                reasoning += " [TOFU pipeline keyword found in JD]"
            reasoning += f" [posting: {job.get('url')}] [headcount: {headcount}] [industry: {job.get('org_linkedin_industry')}]"
            company.hiring_signal_role = role
            company.hiring_signal_hire_type = hire_type
            company.hiring_signal_strength = strength
            company.hiring_signal_reasoning = reasoning

        db.add(company)
        db.commit()
        db.refresh(company)

        team_fit = assess_team_composition(company, db)
        if team_fit["tier"] == "excluded":
            rejection_counts["full_team"] = rejection_counts.get("full_team", 0) + 1
            db.delete(company)
            db.commit()
            continue

        seen_domains.add(domain)
        kept.append(company)

    return {
        "companies_discovered": len(kept),
        "postings_checked": len(jobs),
        "rejection_breakdown": rejection_counts,
        "budget_stopped_early": False,
        "estimated_cost_usd": estimate_cost_usd(len(jobs)),
        "api_error": None,
    }
