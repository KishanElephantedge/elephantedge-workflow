"""Company enrichment for V2-discovered companies -- the step V2 never had.

V2 finds companies from fresh signals (job posts, LinkedIn posts) and creates them, but nothing
ever filled in the firmographics ICP matching depends on. Confirmed live (2026-08-31): only 17 of
776 companies carried all three fields evaluate_icp_matches_for_company() requires, so 1,486 of
1,500 real ICP checks returned "insufficient_information" and ZERO matches were ever recorded.
V1's scoring phases compute these, but V2 never runs them.

TOOL CHOICE IS COST-DRIVEN, and every price here was read from `deepline tools describe`, not
assumed:
  - harvestapi_get_company     0.03 credits -- exact LinkedIn employeeCount + industry
  - assess_team_composition    0.04 credits -- real sales/marketing headcount (already existed)
  - crustdata_v3_company_enrich 0.8 credits -- 27x dearer and returns NO revenue; deliberately
                               NOT used (confirmed by spending it once, so nobody has to again)
  - apollo_enrich_company      free, but bills the tenant's own Apollo credits -- excluded by
                               explicit instruction
Revenue stays with the existing Apify Google path (~$0.0085), now domain-disambiguated.

Total ~0.07 credits (~$0.007) per company. Deliberately NOT a backfill: this only ever touches
companies that are missing the fields, newest first, under a hard per-run cap, because the
existing 776 are V1's and re-enriching them was explicitly declined.
"""

import logging

from sqlalchemy.orm import Session

from app.db.models import Batch, Company
from app.deepline_client import DeeplineError, execute_tool
from app.phases.hiring_signal import assess_team_composition

logger = logging.getLogger(__name__)

# Bounded per run so a large backlog can never turn into a surprise bill -- at 0.07 credits each
# this caps a single tick at ~0.7 credits (~$0.07), matching the 10-companies/day target.
DEFAULT_ENRICHMENT_LIMIT = 10

# Batch sources V2 itself creates -- "v2_discovery" (jobs-first discovery, see
# orchestration/discovery.py) and "v2-signal" (a company created from a fresh signal's own
# identity resolution). V1's "deepline"/"jobo" batches are never enriched by this sweep.
V2_BATCH_SOURCES = ("v2_discovery", "v2-signal")


def _linkedin_universal_name(company: Company) -> str | None:
    """LinkedIn company URLs are .../company/<universalName>[/]. Returns None rather than
    guessing when the stored URL isn't a company URL at all."""
    url = (company.linkedin_url or "").strip().rstrip("/")
    if "/company/" not in url:
        return None
    return url.rsplit("/company/", 1)[-1].split("?")[0] or None


def enrich_company_headcount(db: Session, tenant_id: int, company: Company) -> dict:
    """Exact employee count + industry from LinkedIn via harvestapi (0.03 credits).

    Exact, not a bucket: crustdata only ever returns a range ("51-200"), and storing a bucket's
    low bound understated a real 82-person company as 51 -- which then feeds
    sales-team-size estimation and ICP revenue banding. Never raises."""
    universal_name = _linkedin_universal_name(company)
    if not universal_name:
        return {"status": "skipped", "reason": "no LinkedIn company URL on file to look up"}
    try:
        response = execute_tool("harvestapi_get_company", {"universalName": universal_name})
    except DeeplineError as e:
        return {"status": "unavailable", "reason": f"harvestapi_provider_error: {e}"}

    raw = (response.get("toolResponse") or {}).get("raw") or {}
    element = raw.get("element") if isinstance(raw, dict) else None
    if not isinstance(element, dict):
        return {"status": "not_found", "reason": "harvestapi returned no company element"}

    updated = []
    count = element.get("employeeCount")
    if isinstance(count, int) and count > 0 and count != company.employee_count:
        company.employee_count = count
        updated.append(f"employee_count={count}")
    industries = element.get("industries") or []
    if not company.industry and industries and isinstance(industries[0], dict):
        name = industries[0].get("name")
        if name:
            company.industry = name
            updated.append(f"industry={name!r}")
    if updated:
        db.add(company)
        db.commit()
    return {"status": "succeeded", "updated": updated}


def enrich_company(db: Session, tenant_id: int, company: Company) -> dict:
    """One company, cheapest-first. Each step is independent: a failure in one never blocks the
    others, same per-stage isolation the sweep itself uses."""
    result = {"company_id": company.id, "name": company.name}

    if company.employee_count is None or company.industry is None:
        result["headcount"] = enrich_company_headcount(db, tenant_id, company)

    if company.sales_headcount_percent is None and company.domain:
        try:
            result["team_composition"] = assess_team_composition(company, db)
            db.commit()
        except Exception as e:  # noqa: BLE001 -- a provider failure must never abort the sweep
            db.rollback()
            result["team_composition"] = {"status": "failed", "error": str(e)}
    return result


def run_company_enrichment_sweep(db: Session, tenant_id: int, limit: int = DEFAULT_ENRICHMENT_LIMIT) -> dict:
    """Enriches only companies that are actually missing something, newest first.

    Newest-first is deliberate: a company V2 discovered today is the one an Opportunity is about
    to be built on, whereas the oldest rows are V1's backlog that was explicitly excluded from
    re-enrichment."""
    candidates = (
        db.query(Company)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id)
        # V2-created batches ONLY. V1's existing companies (sources "deepline"/"jobo") are
        # deliberately excluded: re-enriching that backlog was explicitly declined, and without
        # this filter a run that found fewer than `limit` new companies would quietly start
        # spending on it anyway.
        .filter(Batch.source.in_(V2_BATCH_SOURCES))
        .filter((Company.employee_count.is_(None)) | (Company.sales_headcount_percent.is_(None)))
        .order_by(Company.created_at.desc())
        .limit(limit)
        .all()
    )
    result = {"status": "succeeded", "evaluated": len(candidates), "enriched": 0, "failed": 0, "details": []}
    for company in candidates:
        try:
            detail = enrich_company(db, tenant_id, company)
            result["details"].append(detail)
            result["enriched"] += 1
        except Exception as e:  # noqa: BLE001 -- one company must never abort the sweep
            db.rollback()
            result["failed"] += 1
            logger.error("company_enrichment: company_id=%s failed -- %s", company.id, e)
    return result
