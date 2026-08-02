"""
JD-first Discovery -- the flow inversion validated live today: search job postings broadly,
already filtered by firmographic fit, in ONE combined TheirStack query -- instead of
discovering by firmographics first (Crustdata) and then paying to check each company
individually for a signal, which was the old flow's real inefficiency (492 companies checked,
only 13 had any signal at all -- 2.6%).

Rewritten from an earlier version of this file that used sentrion_jobs_search + a separate
fuzzy company-NAME match against Crustdata to resolve firmographic fit -- that name-based
match carries the same wrong-company-attribution risk found elsewhere today (domain
collisions), and TheirStack's own combined query filters by real employee/revenue bands AND
returns the real domain directly, with no separate fuzzy lookup needed.

Precision fix validated live today: `job_title_pattern_or` (regex, word-boundary exact
matching) instead of `job_title_or` (loose natural-language full-text matching) -- the latter
matched irrelevant titles like "Software Engineering Director" and "Retail Sales Associate"
on bare tokens; the former returned 10/10 real, relevant matches in the same real test.

Each new company still gets a free crustdata_v3_company_identify call (no fuzzy matching --
exact domain lookup) purely to fill in real employee_count/revenue for storage, since
TheirStack's non-domain-scoped response doesn't include those fields, and a cheap
team-composition check (same as the old flow) before being kept.
"""
from sqlalchemy.orm import Session

from app.budget_guard import BudgetExceededError, BudgetGuard
from app.db.models import Company
from app.deepline_client import execute_tool
from app.phases.discovery import EMPLOYEE_COUNT_MIN, REVENUE_MIN_USD, _existing_domains
from app.phases.hiring_signal import (
    JOB_POSTING_MAX_AGE_DAYS,
    _classify_role, _classify_signal, _detect_product_fit_signals, _infer_hire_type,
    assess_team_composition, TOFU_KEYWORD,
)

# Word-boundary regex versions of the core sales title patterns -- job_title_pattern_or needs
# real regex, not the plain substrings used for job_title_or/local classification.
JOB_TITLE_PATTERN_OR = [
    r"\bSDR\b", r"\bBDR\b", r"\bAE\b", "Sales Development Representative",
    "Business Development Representative", "Business Development Manager", "Account Executive",
    "Head of Sales", "VP Sales", "VP of Sales", "Director of Sales", "Sales Director",
    "Sales Manager", "CRO", "Chief Revenue Officer", "Head of GTM", "GTM Engineer",
]

# Revenue mismatches found live today (Black Kite $20-50M, Mackinnon Bruce $10-20M, both
# passing TheirStack's own min/max_revenue_usd filter -- likely because it doesn't have a
# revenue figure for them and passes nulls through rather than rejecting). Since revenue
# itself can't be forced at the query level, using two indirect proxies that ARE real,
# reliable TheirStack filters: a tighter headcount ceiling (smaller headcount correlates with
# lower revenue, though imperfectly -- Black Kite is a counterexample, capital-efficient
# despite modest headcount) AND a funding cap (tracks revenue more tightly than headcount for
# capital-efficient/well-funded companies specifically). Kept separate from discovery.py's
# EMPLOYEE_COUNT_MAX (200) -- this is a jd_first-specific experiment, not a change to the
# main company-first pipeline.
JD_FIRST_EMPLOYEE_COUNT_MAX = 100
JD_FIRST_MAX_FUNDING_USD = 20_000_000
JD_FIRST_MAX_REVENUE_USD = 5_000_000  # tightened from discovery.py's shared $10M -- Inspectify
# and Skopenow both showed real revenue $10-20M despite the $10M cap, same null-passthrough
# pattern as Black Kite. This won't fix a null bypassing the filter, but does correctly
# exclude any company whose revenue IS known and between $5-10M.


def _search_jobs_page(offset: int, limit: int, exclude_domains: list[str]) -> list[dict]:
    """exclude_domains: passed as company_domain_not, a real TheirStack filter -- this
    endpoint bills per result returned (confirmed live: 2.76-5.52 credits per 25-result page,
    regardless of how many of those results turn out to be already-known companies we just
    throw away). Without this filter, a company we've already discovered in any prior run
    keeps re-surfacing on every subsequent daily run (it's still actively hiring for the same
    role) and we pay TheirStack for that result every single time before rejecting it locally.
    Excluding at the query level means TheirStack itself skips those postings, so we only ever
    pay for postings that have at least a chance of being new."""
    response = execute_tool(
        "theirstack_job_search",
        {
            "job_title_pattern_or": JOB_TITLE_PATTERN_OR,
            "posted_at_max_age_days": JOB_POSTING_MAX_AGE_DAYS,
            "min_employee_count": EMPLOYEE_COUNT_MIN,
            "max_employee_count": JD_FIRST_EMPLOYEE_COUNT_MAX,
            "min_revenue_usd": REVENUE_MIN_USD,
            "max_revenue_usd": JD_FIRST_MAX_REVENUE_USD,
            "max_funding_usd": JD_FIRST_MAX_FUNDING_USD,
            "job_country_code_or": ["US"],
            "company_domain_not": exclude_domains,
            "order_by": [{"field": "date_posted", "desc": True}],
            "offset": offset,
            "limit": limit,
        },
    )
    raw = response.get("toolResponse", {}).get("raw", {})
    return raw.get("data", []) if isinstance(raw, dict) else []


def _real_firmographics(domain: str) -> dict | None:
    """Free, exact-domain lookup -- fills in employee_count/revenue for storage, and doubles
    as the same domain-collision check used elsewhere today (more than one distinct company
    name on this domain means we can't trust which one we actually found)."""
    response = execute_tool("crustdata_v3_company_identify", {"domains": [domain]})
    raw = response.get("toolResponse", {}).get("raw", {})
    if not raw or not isinstance(raw, list):
        return None
    matches = raw[0].get("matches", [])
    distinct_names = {m.get("company_data", {}).get("basic_info", {}).get("name") for m in matches}
    if len(distinct_names) != 1:
        return None  # collision -- can't trust which company this domain actually belongs to
    return matches[0]["company_data"]["basic_info"] if matches else None


def run_jd_first_discovery(batch_id: int, db: Session, tenant_id: int, target: int = 10, jobs_per_page: int = 25, max_postings_checked: int = 300, budget_guard: BudgetGuard | None = None, start_offset: int = 0) -> dict:
    """Entrypoint. Pages through TheirStack's combined job+firmographic query, and for each
    *new* company domain seen, does a free collision/firmographics check and the same
    team-composition check the main pipeline uses -- only companies passing everything are
    kept. budget_guard, if given, is checked after every page AND after every per-company
    check -- this endpoint bills per job result, so a hard cap has to be checked at both
    granularities, not just once per page.

    start_offset lets a budget-stopped run resume from where the previous call left off
    (returned as "next_offset" below) instead of re-paying to re-fetch the same postings --
    excluded companies are deleted from the batch (no saved state to resume from), so the
    search position itself is the only thing worth preserving across a stop."""
    seen_domains = _existing_domains(tenant_id, db)
    seen_this_run: set[str] = set()
    kept: list[Company] = []
    postings_checked = 0
    offset = start_offset
    rejection_counts: dict[str, int] = {}
    budget_stopped_early = False

    while len(kept) < target and postings_checked < max_postings_checked:
        if budget_guard is not None:
            try:
                budget_guard.check()
            except BudgetExceededError:
                budget_stopped_early = True
                break
        jobs = _search_jobs_page(offset, jobs_per_page, list(seen_domains))
        if not jobs:
            break
        offset += len(jobs)
        postings_checked += len(jobs)

        for job in jobs:
            if len(kept) >= target:
                break
            if budget_guard is not None:
                try:
                    budget_guard.check()
                except BudgetExceededError:
                    budget_stopped_early = True
                    break
            if job.get("closed_at") is not None:
                rejection_counts["stale_posting"] = rejection_counts.get("stale_posting", 0) + 1
                continue
            domain = (job.get("company_domain") or "").lower().strip()
            if not domain or domain in seen_domains or domain in seen_this_run:
                continue
            seen_this_run.add(domain)

            firmographics = _real_firmographics(domain)
            if not firmographics:
                rejection_counts["collision_or_not_found"] = rejection_counts.get("collision_or_not_found", 0) + 1
                continue

            title = job.get("job_title") or ""
            description = job.get("description") or ""
            role = _classify_role(title)
            product_fit_categories = _detect_product_fit_signals(description)
            if not role and not product_fit_categories:
                rejection_counts["no_local_classification_match"] = rejection_counts.get("no_local_classification_match", 0) + 1
                continue

            employee_range = firmographics.get("employee_count_range") or ""
            company = Company(
                batch_id=batch_id,
                name=firmographics.get("name") or job.get("company") or "Unknown",
                domain=domain,
                industry=", ".join((firmographics.get("industries") or [])[:3]) or None,
                source="jd_first:theirstack_job_search+crustdata_v3_company_identify",
                active_job_title=title or None,
                product_fit_jd_categories=product_fit_categories or None,
                # Free -- same crustdata_v3_company_identify call above already returns this,
                # just wasn't being read before. Lets manual verification click straight
                # through instead of copying the company name into LinkedIn's own search.
                linkedin_url=firmographics.get("professional_network_url") or None,
            )

            if role:
                hire_type = _infer_hire_type(company, role)
                strength, reasoning = _classify_signal(company, role, hire_type, description)
                if TOFU_KEYWORD in description.lower():
                    reasoning += " [TOFU pipeline keyword found in JD]"
                reasoning += f" [posting: {job.get('url')}] [employee_range: {employee_range}]"
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

        if budget_stopped_early:
            break

    return {
        "postings_checked": postings_checked,
        "companies_discovered": len(kept),
        "rejection_breakdown": rejection_counts,
        "budget_stopped_early": budget_stopped_early,
        "next_offset": offset,
    }
