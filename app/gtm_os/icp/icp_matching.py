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


# Empirical, not invented: the median revenue-per-employee across the 562 companies in THIS
# tenant's own data that carry both a real revenue figure and a real headcount (p25 $50,000,
# p75 $125,000). Used only as a last-resort proxy -- see _estimated_revenue below.
REVENUE_PER_EMPLOYEE_USD = 79_545


def _estimated_revenue(company: Company) -> tuple[int | None, str | None]:
    lower, higher = company.estimated_revenue_lower_usd, company.estimated_revenue_higher_usd
    if lower is not None and higher is not None:
        return (lower + higher) // 2, f"average of estimated_revenue_lower_usd={lower} and estimated_revenue_higher_usd={higher}"
    if lower is not None:
        return lower, f"estimated_revenue_lower_usd={lower} (higher bound not available)"
    if higher is not None:
        return higher, f"estimated_revenue_higher_usd={higher} (lower bound not available)"

    # Headcount proxy (2026-08-31). Reported revenue simply does not exist for most privately
    # held early-stage companies -- confirmed the hard way on a real account: Deepline's company
    # record carries no revenue field at all, and the Google fallback answered a name-only query
    # with a $4.035B figure belonging to a completely different company of the same name. Without
    # a proxy those companies can never match ANY ICP no matter how strong their buying signal,
    # which is what left 1,486 of 1,500 real ICP checks at "insufficient_information".
    #
    # The evidence string says plainly that this is derived from headcount and is NOT a reported
    # revenue figure, so nothing downstream (or any human reading the match) can mistake it for
    # one. The real spread is wide (p25-p75 is $50k-$125k per employee, ~2.5x), so a company
    # sitting near an ICP band boundary can land either side -- acceptable for a candidate match
    # that a human reviews, never good enough to present as fact.
    if company.employee_count is not None and company.employee_count > 0:
        derived = company.employee_count * REVENUE_PER_EMPLOYEE_USD
        return derived, (
            f"DERIVED from employee_count={company.employee_count} x ${REVENUE_PER_EMPLOYEE_USD:,}/employee "
            f"(tenant-median) = ${derived:,} -- no reported revenue on file, this is a headcount proxy, not a revenue figure"
        )
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


def get_icp_context_for_company(db: Session, tenant_id: int, company_id: int | None) -> dict:
    """V2 Phase 2 -- read-only lookup of whatever run_icp_matching_sweep has ALREADY recorded
    for this company. Never re-evaluates ICP checks itself (that would be a duplicate ICP
    calculation, explicitly ruled out) -- this only reads existing ICPMatch rows.

    "status" is intentionally a 2-way split, not 3-way: "matched" (>=1 real ICPMatch row) or
    "no_match_recorded". The latter honestly covers BOTH "evaluated, didn't match" and "not yet
    evaluated" -- ICPMatch only ever persists POSITIVE matches (see this module's own docstring,
    "only positive evidence" discipline), so there is no persisted negative-evaluation record to
    distinguish those two cases from. Inventing a 3rd "insufficient_information" status here
    would mean either re-running evaluate_icp_matches_for_company() (a duplicate calculation) or
    fabricating a distinction the underlying data doesn't actually support -- neither is done.

    No numeric score is computed or returned anywhere in this function."""
    if company_id is None:
        return {"has_icp_match": False, "matches": [], "status": "no_company_identity"}

    matches = (
        db.query(ICPMatch)
        .filter(ICPMatch.tenant_id == tenant_id, ICPMatch.company_id == company_id)
        .order_by(ICPMatch.icp_id)
        .all()
    )
    if not matches:
        return {"has_icp_match": False, "matches": [], "status": "no_match_recorded"}

    icp_names = {icp["id"]: icp["name"] for icp in get_icp_config(db, tenant_id)}
    return {
        "has_icp_match": True,
        "matches": [
            {
                "icp_id": m.icp_id,
                "icp_name": icp_names.get(m.icp_id, m.icp_id),
                "reasons": m.reasons,
                "evaluated_at": m.evaluated_at.isoformat() if m.evaluated_at else None,
            }
            for m in matches
        ],
        "status": "matched",
    }


def run_icp_matching_sweep(db: Session, tenant_id: int, limit: int = 200, dry_run: bool = False) -> dict:
    """Evaluates up to `limit` companies (tenant-scoped via Batch.tenant_id) against the tenant's
    configured ICPs. `dry_run=True` computes everything but writes nothing. One company's failure
    never aborts the sweep.

    2026-08-26, real fix -- confirmed live: this used to re-evaluate the same fixed batch of
    companies (ordered by id) from scratch on EVERY run, forever, even for a company that already
    got a real, COMPLETE verdict (a genuine match or a genuine no_match with no missing
    information) -- nothing about that verdict can change without new data, so re-checking it
    wastes the run's own `limit` budget on companies that will never move. Candidates are now
    companies that have EITHER never been checked (icp_last_evaluated_at IS NULL) OR whose last
    check was incomplete (icp_last_evaluation_had_missing_information -- it may have been enriched
    since, e.g. by a revenue backfill run just before this stage). A company with a complete prior
    verdict is skipped entirely. Ordered never-checked-first, then longest-since-checked, so the
    same `limit` now makes real forward progress through the whole company pool instead of being
    stuck on the same first N companies by id every time."""
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
        .filter((Company.icp_last_evaluated_at.is_(None)) | (Company.icp_last_evaluation_had_missing_information.is_(True)))
        .order_by(Company.icp_last_evaluated_at.is_(None).desc(), Company.icp_last_evaluated_at.asc())
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
            had_missing_information = False
            for result in results:
                counts["icp_checks_performed"] += 1
                if result["missing_information"]:
                    counts["insufficient_information"] += 1
                    had_missing_information = True
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

            if not dry_run:
                company.icp_last_evaluated_at = datetime.utcnow()
                company.icp_last_evaluation_had_missing_information = had_missing_information
                db.commit()

        except Exception:  # noqa: BLE001 -- one company's failure must never block the others
            db.rollback()  # 2026-08-26, same real fix as contact_discovery.py: never leave the shared session invalid for the next company
            counts["failed"] += 1

    return counts
