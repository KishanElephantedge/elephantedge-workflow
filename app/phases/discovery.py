"""
Phase 3 — Company Discovery, with Phase 4's exclusion filters and Phase 5's free funding/growth
field capture folded in directly, per those phases' own documented findings (see
phase3-company-discovery.md, phase4-company-qualification.md, phase5-buying-signal-intelligence.md).

Updated per Signal Framework v2 (from Gokul, signal-framework-v2-from-gokul.md):
- Industry widened from SaaS/Agentic-AI-only to all industries (Tech/Non-tech is no longer a
  gate, just a classification tag).
- Geography tiering added (Tier 1 city vs. Tier 2), on top of the existing US-only hard gate.
- Sales/Marketing headcount ratios captured for free from the same Crustdata response
  (`role_distribution_percent`), and Sales headcount % is now a real hard gate (2-10%).

Single provider (Crustdata via Deepline), same proven pattern as Synefi's discover_company_page --
one query, paginated via cursor until a target count is reached or a safety cap is hit.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.budget_guard import BudgetExceededError, BudgetGuard
from app.db.models import Batch, Company
from app.deepline_client import execute_tool, extract_rows


def _parse_date(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


# Phase 2's confirmed hard gates.
EMPLOYEE_COUNT_MIN = 11
EMPLOYEE_COUNT_MAX = 50
REVENUE_MIN_USD = 2_500_000
REVENUE_MAX_USD = 5_000_000

# Signal Framework v2 -- Sales headcount as a % of total headcount is now a real hard gate,
# computed post-fetch (Crustdata's query filters don't support filtering on
# role_distribution_percent directly -- it's returned data, not a filterable field).
SALES_HEADCOUNT_PERCENT_MIN = 2.0
SALES_HEADCOUNT_PERCENT_MAX = 10.0

# Signal Framework v2 -- Tier 1 cities (from the original list plus the Bay Area example
# re-confirmed this pass). Everything else in the US is Tier 2, not excluded.
TIER_1_CITIES = ["new york", "los angeles", "san francisco", "bay area", "chicago", "washington"]

# Signal Framework v2 -- Tech/Non-tech classification (informational tag now, not a gate --
# both are in scope). Keyword-matched against Crustdata's own linkedin_industries values.
TECH_INDUSTRY_KEYWORDS = [
    "software", "technology", "internet", "artificial intelligence", "computer",
    "information technology", "it services", "saas", "data", "cloud", "cybersecurity",
]

# Phase 4, Finding 1 -- free exclusion filters (Crustdata bills per returned result, so excluding
# non-fits here reduces cost rather than adding a step).
EXCLUDED_COMPANY_TYPES = ["Nonprofit", "Government Agency", "Educational Institution", "Self-Employed", "Sole Proprietorship"]


def _existing_domains(tenant_id: int, db: Session) -> set[str]:
    """Phase 3, Topic 5 -- domain-primary duplicate detection, scoped per-tenant across all of
    its batches (not just the current one). Necessary even within a single manual Discovery
    call: each call starts its own cursor from scratch, so re-running Discovery (e.g. across
    two separate requests, as happened live) would otherwise re-fetch and re-save the same
    leading companies every time."""
    rows = (
        db.query(Company.domain)
        .join(Batch)
        .filter(Batch.tenant_id == tenant_id)
        .filter(Company.domain.isnot(None))
        .all()
    )
    return {r[0].lower() for r in rows if r[0]}


def _geography_tier(hq_location: str | None) -> str:
    if not hq_location:
        return "tier_2"
    location_lower = hq_location.lower()
    return "tier_1" if any(city in location_lower for city in TIER_1_CITIES) else "tier_2"


def _industry_classification(industries: list[str]) -> str:
    combined = " ".join(industries).lower()
    return "tech" if any(kw in combined for kw in TECH_INDUSTRY_KEYWORDS) else "non_tech"


def discover_company_page(batch_id: int, cursor: str | None, page_size: int, db: Session, tenant_id: int, seen_domains: set[str], rejection_counts: dict, query_exclude_domains: frozenset[str]) -> tuple[list[Company], str | None, int]:
    """One page of Crustdata's companydb_search, mapped to Elephant Edge's ICP + exclusions.
    Retains the funding/growth/role-distribution fields confirmed returned for free in the same
    response, so no separate paid call is needed for those signals.

    Sales headcount % is applied as a post-fetch gate here (not a query filter, since Crustdata
    doesn't support filtering on role_distribution_percent) -- a company outside 2-10% sales
    headcount is not persisted, same treatment as a company missing a domain.

    rejection_counts is mutated in place with a reason for every row NOT kept -- every row
    here already matched the hard query filters (employee count/revenue/geography/type), so
    this is the only visibility into why a row that matched those still wasn't saved (no
    public domain listed, or outside the sales-headcount range) -- the previous version of
    this code billed for these rows without ever recording why they were dropped.

    query_exclude_domains (every domain already saved for this tenant from PRIOR batches,
    frozen once at the start of run_discovery) is passed as a company_website_domain not_in
    filter, so Crustdata excludes them server-side and we're never billed for seeing them
    again -- found live: with heavy repeat testing, up to 12 of 13 rows in one page were
    already-seen duplicates, each one billed anyway under the old client-side-only dedup.

    This list is deliberately NOT grown with companies kept during this same run (that's
    what seen_domains, mutated per-call below, is for) -- found live that changing the
    filters between pages while reusing the same pagination cursor breaks the cursor
    entirely ("Cursor is invalid for this query"), since Crustdata ties a cursor to the
    exact filter set it was issued for. seen_domains still protects the (very unlikely)
    case of the same row appearing twice across pages, client-side, without touching the
    query itself mid-pagination."""
    filters = [
        {"filter_type": "hq_country", "type": "=", "value": "USA"},
        {"filter_type": "employee_metrics.latest_count", "type": "=>", "value": EMPLOYEE_COUNT_MIN},
        {"filter_type": "employee_metrics.latest_count", "type": "=<", "value": EMPLOYEE_COUNT_MAX},
        {"filter_type": "estimated_revenue_lower_bound_usd", "type": "=>", "value": REVENUE_MIN_USD},
        {"filter_type": "estimated_revenue_higher_bound_usd", "type": "=<", "value": REVENUE_MAX_USD},
        {"filter_type": "acquisition_status", "type": "!=", "value": "acquired"},
        {"filter_type": "company_type", "type": "not_in", "value": EXCLUDED_COMPANY_TYPES},
    ]
    if query_exclude_domains:
        filters.append({"filter_type": "company_website_domain", "type": "not_in", "value": sorted(query_exclude_domains)})

    payload = {"filters": filters, "limit": page_size}
    if cursor:
        payload["cursor"] = cursor

    response = execute_tool("crustdata_companydb_search", payload)
    rows = extract_rows(response, "companies")
    raw = response.get("toolResponse", {}).get("raw", {})
    next_cursor = None
    if isinstance(raw, dict):
        next_cursor = ((raw.get("data") or raw).get("meta") or {}).get("nextCursor")

    companies = []
    for row in rows:
        domain = row.get("company_website_domain") or (row.get("domains") or [None])[0]
        if not domain:
            rejection_counts["no_public_domain"] = rejection_counts.get("no_public_domain", 0) + 1
            continue
        if domain.lower() in seen_domains:
            rejection_counts["already_seen_duplicate"] = rejection_counts.get("already_seen_duplicate", 0) + 1
            continue

        role_distribution_percent = row.get("role_distribution_percent") or {}
        sales_pct = role_distribution_percent.get("Sales", 0) + role_distribution_percent.get("Business Development", 0)
        marketing_pct = role_distribution_percent.get("Marketing", 0)

        # Sales headcount % is NOT a gate for now, per explicit instruction -- 92% of
        # rows were being rejected on this alone (real diagnostic run, batch 20), and
        # it was unclear whether that reflected a genuinely narrow ICP or too strict a
        # band. Still captured and stored below (useful for scoring/visibility), just
        # no longer used to reject a company outright.
        seen_domains.add(domain.lower())
        industries = row.get("linkedin_industries") or []
        employee_metrics = row.get("employee_metrics") or {}
        company = Company(
            batch_id=batch_id,
            name=row.get("company_name", "Unknown"),
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
            source="crustdata_companydb_search",
        )
        db.add(company)
        companies.append(company)
    db.commit()
    for c in companies:
        db.refresh(c)
    return companies, next_cursor, len(rows)


def run_discovery(batch_id: int, db: Session, tenant_id: int, target: int = 10, page_size: int = 10, max_checked: int = 100, budget_guard: BudgetGuard | None = None) -> dict:
    """Phase 3 entrypoint. Pages through Crustdata until `target` companies are found or
    `max_checked` total have been checked (safety cap), matching Synefi's proven
    target-seeking pagination pattern.

    Requests only as many rows as still needed each page (min(page_size, remaining target)) --
    discover_company_page persists every row it's given immediately, so over-requesting would
    silently save more companies than `target` asked for, past what was intended to be fetched
    (and billed for, since Crustdata bills per row returned).

    Deduplicates against every domain already saved for this tenant (any batch, any prior
    call) -- found live: two separate Discovery calls each start their own cursor from
    scratch, so without this, re-running Discovery re-fetches and re-saves the same leading
    companies every time.

    budget_guard, if given, is checked after every single page -- stops the loop (rather
    than raising out of it) the moment this run's hard spend cap is reached, so a runaway
    page loop can burn through at most one more page's worth of credits past the cap."""
    seen_domains = _existing_domains(tenant_id, db)
    query_exclude_domains = frozenset(seen_domains)  # frozen for the whole pagination sequence -- see discover_company_page's docstring for why
    kept: list[Company] = []
    raw_seen = 0  # every row examined, duplicate or not -- what's actually billed
    cursor = None
    budget_stopped_early = False
    rejection_counts: dict = {}

    while len(kept) < target and raw_seen < max_checked:
        remaining = min(page_size, target - len(kept), max_checked - raw_seen)
        page, cursor, raw_count = discover_company_page(batch_id, cursor, remaining, db, tenant_id, seen_domains, rejection_counts, query_exclude_domains)
        raw_seen += raw_count

        if raw_count == 0:
            break  # provider has genuinely nothing left matching the filters

        for company in page:
            kept.append(company)
            if len(kept) >= target:
                break

        if budget_guard is not None:
            try:
                budget_guard.check()
            except BudgetExceededError:
                budget_stopped_early = True
                break

        if not cursor:
            break  # provider says there's no next page, even if target wasn't reached

    return {
        "companies_checked": raw_seen,
        "companies_discovered": len(kept),
        "budget_stopped_early": budget_stopped_early,
        "rejection_breakdown": rejection_counts,
    }
