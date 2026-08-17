"""ICP matching / trigger detection -- Batch 8. Deterministic only, no LLM (per scope boundary).

REUSES EXISTING CAPABILITIES, DOES NOT DUPLICATE THEM: hiring-signal detection already exists
and is already computed onto Company (app/phases/hiring_signal.py -> Company.hiring_signal_role/
hiring_signal_hire_type/hiring_signal_strength/hiring_signal_reasoning), as part of the existing
Score/ICP discovery pipeline (Phase 9). This module reads those columns READ-ONLY -- it never
re-derives hiring signal from raw job postings itself, and it never modifies Company, Score, or
any Phase 9 file. That existing pipeline's own `Score.icp_fit` is a DIFFERENT, older, unrelated
concept (a scoring component in the batch-discovery pipeline) -- untouched by this module.

NO FABRICATION (test 5/6): every ICP check that cannot be evaluated because the underlying
Company field is null is recorded as `missing_information`, never assumed true or false. A
company is only ever recorded as matching an ICP when every one of that ICP's configured checks
was BOTH evaluable AND satisfied.

TENANT SCOPING: Company has no direct tenant_id column (existing schema) -- tenant scoping flows
through Company.batch_id -> Batch.tenant_id, exactly like every other Company query in this
codebase already does."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Session

from app.db.models import Base, Batch, Company
from app.gtm_os.icp.icp_config import get_icp_config


class ICPMatch(Base):
    """One row per (company, ICP) pair that CURRENTLY matches -- non-matches are never persisted
    (same "only positive evidence" discipline as ContentTopicEvidence). Unique on
    (tenant_id, company_id, icp_id): re-evaluating an already-matched company updates this row
    in place (a current-state fact, not a historical decision -- unlike GtmStrategy's own
    deliberate append-only versioning) rather than creating a duplicate."""
    __tablename__ = "icp_matches"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    icp_id = Column(String, nullable=False)  # references icp_config's own "id" field -- not a DB FK, since ICP defs live in Parameter, not a table

    reasons = Column(JSON, nullable=False)  # list[str] -- every satisfied check, explainable
    trigger_evidence = Column(JSON, nullable=False)  # the real Company field values this match was computed from

    evaluated_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)


def _estimated_revenue(company: Company) -> tuple[int | None, str | None]:
    lower, higher = company.estimated_revenue_lower_usd, company.estimated_revenue_higher_usd
    if lower is not None and higher is not None:
        return (lower + higher) // 2, f"average of estimated_revenue_lower_usd={lower} and estimated_revenue_higher_usd={higher}"
    if lower is not None:
        return lower, f"estimated_revenue_lower_usd={lower} (higher bound not available)"
    if higher is not None:
        return higher, f"estimated_revenue_higher_usd={higher} (lower bound not available)"
    return None, None


def _estimated_sales_team_size(company: Company) -> tuple[float | None, str | None]:
    if company.employee_count is not None and company.sales_headcount_percent is not None:
        estimate = company.employee_count * company.sales_headcount_percent / 100
        return estimate, f"employee_count={company.employee_count} * sales_headcount_percent={company.sales_headcount_percent}%"
    return None, None


def evaluate_icp_matches_for_company(company: Company, icp_config: list[dict]) -> list[dict]:
    """Pure, read-only evaluation of one Company against every configured ICP. Returns one
    structured result per ICP (matched or not) -- never a single score, never silently skips an
    ICP. Reads ONLY existing Company columns -- no new computation beyond the two documented
    derivations above (revenue midpoint, sales-team-size estimate), both computed from real,
    already-populated fields."""
    revenue, revenue_evidence = _estimated_revenue(company)
    sales_team_size, sales_team_evidence = _estimated_sales_team_size(company)

    results = []
    for icp in icp_config:
        reasons: list[str] = []
        missing_information: list[str] = []
        trigger_evidence: dict = {}
        checks_satisfied = True

        # Revenue range check
        if icp.get("revenue_min_usd") is not None or icp.get("revenue_max_usd") is not None:
            if revenue is None:
                missing_information.append("no estimated revenue available (Company.estimated_revenue_lower_usd/higher_usd both null)")
                checks_satisfied = False
            else:
                trigger_evidence["estimated_revenue_usd"] = revenue
                trigger_evidence["estimated_revenue_basis"] = revenue_evidence
                min_ok = icp.get("revenue_min_usd") is None or revenue >= icp["revenue_min_usd"]
                max_ok = icp.get("revenue_max_usd") is None or revenue <= icp["revenue_max_usd"]
                if min_ok and max_ok:
                    reasons.append(f"estimated revenue ${revenue:,} within configured range")
                else:
                    checks_satisfied = False

        # Sales team size check (only ICP 1 configures this today)
        if icp.get("sales_team_size_max") is not None:
            if sales_team_size is None:
                missing_information.append("cannot estimate sales team size (Company.employee_count and/or sales_headcount_percent null)")
                checks_satisfied = False
            else:
                trigger_evidence["estimated_sales_team_size"] = round(sales_team_size, 1)
                trigger_evidence["estimated_sales_team_size_basis"] = sales_team_evidence
                if sales_team_size <= icp["sales_team_size_max"]:
                    reasons.append(f"estimated sales team size ({round(sales_team_size, 1)}) within configured max ({icp['sales_team_size_max']})")
                else:
                    checks_satisfied = False

        # Hiring-signal trigger check
        trigger_mode = icp["trigger_mode"]
        trigger_roles = set(icp["trigger_hiring_roles"])
        if company.hiring_signal_role is None:
            missing_information.append("no hiring_signal_role available for this company (hiring signal not yet evaluated by the existing Score pipeline)")
            checks_satisfied = False
        else:
            trigger_evidence["hiring_signal_role"] = company.hiring_signal_role
            trigger_evidence["hiring_signal_strength"] = company.hiring_signal_strength
            trigger_evidence["hiring_signal_reasoning"] = company.hiring_signal_reasoning
            if trigger_mode == "requires_presence":
                if company.hiring_signal_role in trigger_roles:
                    reasons.append(f"{icp['trigger_description']} (hiring_signal_role={company.hiring_signal_role!r})")
                else:
                    checks_satisfied = False
            else:  # requires_absence
                if company.hiring_signal_role not in trigger_roles:
                    reasons.append(f"{icp['trigger_description']} (hiring_signal_role={company.hiring_signal_role!r}, not in excluded set)")
                else:
                    checks_satisfied = False

        matched = checks_satisfied and len(reasons) > 0 and len(missing_information) == 0
        results.append({
            "icp_id": icp["id"],
            "icp_name": icp["name"],
            "matched": matched,
            "reasons": reasons,
            "missing_information": missing_information,
            "trigger_evidence": trigger_evidence,
        })

    return results


def _existing_match(db: Session, tenant_id: int, company_id: int, icp_id: str) -> ICPMatch | None:
    return (
        db.query(ICPMatch)
        .filter(ICPMatch.tenant_id == tenant_id, ICPMatch.company_id == company_id, ICPMatch.icp_id == icp_id)
        .first()
    )


def record_icp_match(db: Session, tenant_id: int, company_id: int, result: dict) -> ICPMatch:
    """Upserts the current-state ICPMatch row for this (company, ICP) pair -- idempotent by
    construction (test 9): re-running with unchanged evidence updates the same row to the same
    values, never creates a duplicate."""
    existing = _existing_match(db, tenant_id, company_id, result["icp_id"])
    if existing is not None:
        existing.reasons = result["reasons"]
        existing.trigger_evidence = result["trigger_evidence"]
        existing.evaluated_at = datetime.utcnow()
        db.commit()
        return existing

    match = ICPMatch(
        tenant_id=tenant_id,
        company_id=company_id,
        icp_id=result["icp_id"],
        reasons=result["reasons"],
        trigger_evidence=result["trigger_evidence"],
    )
    db.add(match)
    db.commit()
    return match


def run_icp_matching_sweep(db: Session, tenant_id: int, limit: int = 200, dry_run: bool = False) -> dict:
    """Evaluates up to `limit` companies (tenant-scoped via Batch.tenant_id) against the tenant's
    configured ICPs. `dry_run=True` computes everything but writes nothing. One company's failure
    never aborts the sweep."""
    counts = {
        "companies_evaluated": 0,
        "icp_checks_performed": 0,
        "matches_recorded": 0,
        "matches_updated": 0,
        "no_match": 0,
        "insufficient_information": 0,
        "failed": 0,
        "dry_run": dry_run,
    }

    icp_config = get_icp_config(db, tenant_id)

    company_ids = [
        row[0]
        for row in db.query(Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id)
        .order_by(Company.id)
        .limit(limit)
        .all()
    ]

    for company_id in company_ids:
        try:
            company = db.get(Company, company_id)
            if company is None:
                continue
            counts["companies_evaluated"] += 1

            results = evaluate_icp_matches_for_company(company, icp_config)
            for result in results:
                counts["icp_checks_performed"] += 1
                if result["missing_information"]:
                    counts["insufficient_information"] += 1
                    continue
                if not result["matched"]:
                    counts["no_match"] += 1
                    continue

                existed_before = _existing_match(db, tenant_id, company_id, result["icp_id"]) is not None
                if not dry_run:
                    record_icp_match(db, tenant_id, company_id, result)
                if existed_before:
                    counts["matches_updated"] += 1
                else:
                    counts["matches_recorded"] += 1

        except Exception:  # noqa: BLE001 -- one company's failure must never block the others
            counts["failed"] += 1

    return counts
