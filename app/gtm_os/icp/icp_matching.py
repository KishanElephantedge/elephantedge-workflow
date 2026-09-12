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

from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Session

from app.db.models import Base, Batch, Company, Contact
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

# The real SPREAD behind that median, recomputed 2026-09-10 across 601 real companies in this
# tenant's data (p25 $54,348 / p50 $83,333 / p75 $129,310). Kept separate from the median above,
# which stays the right single-point answer when ESTIMATING one company's revenue from its
# headcount.
#
# These two exist because a SEARCH RANGE and a POINT ESTIMATE are different problems, and
# conflating them was a real, measured bug (see headcount_band_for_icp in discovery_profiles.py).
# Converting an ICP's revenue band into an employee-count band with the median alone assumes every
# company in that band earns exactly the median per head. Real ones don't: a $5M company is a
# genuine ICP-1 match at 40 employees (efficient) or at 100 (services-heavy), and searching only
# 37-50 structurally cannot see the second one. A search range must therefore use the efficient end
# for its lower bound and the inefficient end for its upper bound, or it silently excludes most of
# the real addressable market. Nothing downstream is loosened by this: icp_matching still checks
# every discovered company against the ICP's own real revenue/sales-team rules.
REVENUE_PER_EMPLOYEE_P25_USD = 54_348
REVENUE_PER_EMPLOYEE_P75_USD = 129_310


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


def _estimated_revenue_range(company: Company) -> tuple[int | None, int | None, bool, str | None]:
    """(low, high, is_derived, evidence) -- the PLAUSIBLE revenue range, for band checks.

    Split out from _estimated_revenue (2026-09-10) because a point estimate and a band test are
    different questions. When there is no reported revenue, the headcount proxy carries a ~2.4x
    spread (p25 $54,348 to p75 $129,310 per employee), which _estimated_revenue's own comment has
    always acknowledged -- and the caller then compared that single median-derived number against
    an ICP's revenue band as a hard pass/fail anyway.

    Measured 2026-09-10: Ocient (241 employees, really hiring a VP of Sales, employee count inside
    icp_3's band) was rejected because 241 x the median = $19,170,345, four percent under icp_3's
    $20M floor. Its real plausible range at that headcount is $13.1M-$31.2M, which straddles the
    floor. 23 of 28 companies that day failed on proxy-derived checks like this one.

    A reported figure keeps its own real bounds. A derived one returns the honest range, and the
    caller tests for OVERLAP with the ICP band -- "this company could plausibly be in this band",
    which is what a proxy can actually support. It is deliberately not presented as a confirmed
    revenue match; see the evidence string and the reason text the caller writes."""
    lower, higher = company.estimated_revenue_lower_usd, company.estimated_revenue_higher_usd
    if lower is not None and higher is not None:
        return lower, higher, False, f"reported range ${lower:,}-${higher:,}"
    if lower is not None or higher is not None:
        value = lower if lower is not None else higher
        which = "estimated_revenue_lower_usd" if lower is not None else "estimated_revenue_higher_usd"
        return value, value, False, f"{which}=${value:,} (only bound available)"

    if company.employee_count is not None and company.employee_count > 0:
        low = company.employee_count * REVENUE_PER_EMPLOYEE_P25_USD
        high = company.employee_count * REVENUE_PER_EMPLOYEE_P75_USD
        return low, high, True, (
            f"PLAUSIBLE RANGE derived from employee_count={company.employee_count} x "
            f"${REVENUE_PER_EMPLOYEE_P25_USD:,}-${REVENUE_PER_EMPLOYEE_P75_USD:,}/employee (tenant p25-p75) "
            f"= ${low:,}-${high:,} -- no reported revenue on file, this is a headcount proxy, not a revenue figure"
        )
    return None, None, False, None


# Empirical, same discipline as REVENUE_PER_EMPLOYEE_USD above: the median sales_headcount_percent
# across the 570 companies in THIS database that have a real measured value (p25 11.93%,
# p75 28.46%). Used ONLY when the measured percentage is absent.
#
# WHY A FALLBACK AT ALL. sales_headcount_percent comes from assess_team_composition, a PAID call,
# so a company discovered from a job posting never has it. icp_matching then recorded
# "cannot estimate sales team size" and refused to match -- not "does not match", but "cannot
# tell". Measured 2026-09-04: all 10 of that day's real opportunities were blocked on exactly
# this, and had to be matched by hand.
#
# "Cannot tell" is the worst of the three answers: it is indistinguishable from a genuine
# non-match downstream, and it hides WHY an ICP never fires. With the proxy the matcher returns a
# real verdict from data already on file, and the evidence string says plainly that it is derived.
SALES_HEADCOUNT_PERCENT_MEDIAN = 19.85

# Same median-vs-spread distinction as REVENUE_PER_EMPLOYEE_P25/P75_USD above, recomputed
# 2026-09-10 across 579 real companies (p25 11.87% / p50 19.70% / p75 28.13%). The p25 figure is
# the one a SEARCH range needs: a "<=10 sales reps" ICP rule is still satisfied by a 84-person
# company that runs a lean 11.87% sales function, so a discovery band that stops at the median's
# ~50 employees excludes real matches the ICP itself would accept.
SALES_HEADCOUNT_PERCENT_P25 = 11.87
SALES_HEADCOUNT_PERCENT_P75 = 28.13


def _estimated_sales_team_size(company: Company) -> tuple[float | None, str | None]:
    if company.employee_count is None:
        return None, None
    if company.sales_headcount_percent is not None:
        estimate = company.employee_count * company.sales_headcount_percent / 100
        return estimate, f"employee_count={company.employee_count} * sales_headcount_percent={company.sales_headcount_percent}%"
    estimate = company.employee_count * SALES_HEADCOUNT_PERCENT_MEDIAN / 100
    return estimate, (
        f"DERIVED from employee_count={company.employee_count} x {SALES_HEADCOUNT_PERCENT_MEDIAN}% "
        f"(tenant-median sales headcount) = {estimate:.1f} -- no measured sales_headcount_percent on "
        f"file, this is a proxy, not a counted sales team"
    )


def _estimated_sales_team_size_range(company: Company) -> tuple[float | None, float | None, bool, str | None]:
    """(low, high, is_derived, evidence) -- same median-vs-spread split as
    _estimated_revenue_range, for the sales_team_size_max check.

    Measured 2026-09-10: Parabola (77 employees, hiring an AE, revenue inside icp_1's band) failed
    icp_1 because 77 x the 19.85% median = 15.3 estimated reps against a max of 10. At the p25 rate
    the same headcount is 9.1 reps, inside the cap. The real spread is 11.87%-28.13%, so a single
    median cannot decide this."""
    if company.employee_count is None:
        return None, None, False, None
    if company.sales_headcount_percent is not None:
        exact = company.employee_count * company.sales_headcount_percent / 100
        return exact, exact, False, f"employee_count={company.employee_count} * measured sales_headcount_percent={company.sales_headcount_percent}%"
    low = company.employee_count * SALES_HEADCOUNT_PERCENT_P25 / 100
    high = company.employee_count * SALES_HEADCOUNT_PERCENT_P75 / 100
    return low, high, True, (
        f"PLAUSIBLE RANGE derived from employee_count={company.employee_count} x "
        f"{SALES_HEADCOUNT_PERCENT_P25}%-{SALES_HEADCOUNT_PERCENT_P75}% (tenant p25-p75) = {low:.1f}-{high:.1f} "
        f"-- no measured sales_headcount_percent on file, this is a proxy, not a counted sales team"
    )


# Industries where revenue routinely includes pass-through billings (contractor/placement
# revenue), so revenue-per-employee runs far above the tenant's own p25-p75 of $54k-$129k and the
# headcount proxy understates real revenue by an order of magnitude.
#
# Real case this exists for (2026-09-10): Centraprise, an IT staffing firm, 330 employees, was
# discovered and matched as a $26M company by the proxy. Its real revenue is $500M-$1B -- 10-20x
# above every ICP ceiling. It reached the decision-maker stage before a human spotted it.
#
# Only applied above PASS_THROUGH_REVENUE_MIN_EMPLOYEES: small firms in these same industries are
# genuine ICP matches (Beyond Cloud Consulting at 41 employees, Saltech Systems at 34), and the
# proxy is not meaningfully wrong for them. This does not reject anything -- it records that the
# proxy cannot be trusted for this company, which surfaces as insufficient_information rather than
# a clean match, so a human checks the real figure before it is pushed. run_icp_matching_sweep
# already re-evaluates companies whose last check had missing information, so a later revenue
# backfill resolves it automatically.
PASS_THROUGH_REVENUE_INDUSTRY_KEYWORDS = [
    "it services", "staffing", "recruiting", "consulting", "human resources",
]
PASS_THROUGH_REVENUE_MIN_EMPLOYEES = 200


def _needs_real_revenue_check(company: Company) -> bool:
    if company.employee_count is None or company.employee_count <= PASS_THROUGH_REVENUE_MIN_EMPLOYEES:
        return False
    industry = (company.industry or "").lower()
    return any(keyword in industry for keyword in PASS_THROUGH_REVENUE_INDUSTRY_KEYWORDS)


def evaluate_icp_matches_for_company(company: Company, icp_config: list[dict]) -> list[dict]:
    """Pure, read-only evaluation of one Company against every configured ICP. Returns one
    structured result per ICP (matched or not) -- never a single score, never silently skips an
    ICP. Reads ONLY existing Company columns -- no new computation beyond the two documented
    derivations above (revenue midpoint, sales-team-size estimate), both computed from real,
    already-populated fields."""
    revenue_low, revenue_high, revenue_derived, revenue_evidence = _estimated_revenue_range(company)
    sales_low, sales_high, sales_derived, sales_team_evidence = _estimated_sales_team_size_range(company)

    results = []
    for icp in icp_config:
        reasons: list[str] = []
        missing_information: list[str] = []
        trigger_evidence: dict = {}
        checks_satisfied = True

        # Revenue range check
        if icp.get("revenue_min_usd") is not None or icp.get("revenue_max_usd") is not None:
            if revenue_low is None:
                missing_information.append("no estimated revenue available (Company.estimated_revenue_lower_usd/higher_usd both null)")
                checks_satisfied = False
            else:
                trigger_evidence["estimated_revenue_usd"] = revenue_low if revenue_low == revenue_high else [revenue_low, revenue_high]
                trigger_evidence["estimated_revenue_basis"] = revenue_evidence
                trigger_evidence["estimated_revenue_is_derived"] = revenue_derived
                if revenue_derived and _needs_real_revenue_check(company):
                    missing_information.append(
                        f"revenue is a headcount proxy and this is a {company.employee_count}-employee "
                        f"{company.industry!r} company -- revenue per employee in these industries routinely "
                        f"includes pass-through billings and runs far above the $54,348-$129,310 range this "
                        f"proxy assumes (real case: a 330-employee IT staffing firm proxied at $26M actually "
                        f"reports $500M-$1B). Needs a real revenue figure before this match can be trusted."
                    )
                    checks_satisfied = False
                # Overlap, not point-containment -- a range that straddles a band boundary still
                # means this company could genuinely be in the band. See _estimated_revenue_range.
                min_ok = icp.get("revenue_min_usd") is None or revenue_high >= icp["revenue_min_usd"]
                max_ok = icp.get("revenue_max_usd") is None or revenue_low <= icp["revenue_max_usd"]
                if min_ok and max_ok:
                    if revenue_derived:
                        reasons.append(f"plausible revenue ${revenue_low:,}-${revenue_high:,} (headcount proxy) overlaps configured range")
                    else:
                        reasons.append(f"reported revenue ${revenue_low:,}-${revenue_high:,} within configured range")
                else:
                    checks_satisfied = False

        # Employee count check (2026-09-09) -- the real, primary size gate now that icp_1/2/3
        # each configure an explicit employee_min/employee_max, independent of the revenue-derived
        # proxy (see icp_config.py's own note on Gokul's confirmed 11-50 gate for icp_1). Checked
        # directly against Company.employee_count, not the sales-team-size estimate below.
        if icp.get("employee_min") is not None or icp.get("employee_max") is not None:
            if company.employee_count is None:
                missing_information.append("no employee_count on file")
                checks_satisfied = False
            else:
                trigger_evidence["employee_count"] = company.employee_count
                min_ok = icp.get("employee_min") is None or company.employee_count >= icp["employee_min"]
                max_ok = icp.get("employee_max") is None or company.employee_count <= icp["employee_max"]
                if min_ok and max_ok:
                    reasons.append(f"employee count ({company.employee_count}) within configured range")
                else:
                    checks_satisfied = False

        # Sales team size check (only ICP 1 configures this today)
        if icp.get("sales_team_size_max") is not None:
            if sales_low is None:
                missing_information.append("cannot estimate sales team size (Company.employee_count and/or sales_headcount_percent null)")
                checks_satisfied = False
            else:
                trigger_evidence["estimated_sales_team_size"] = round(sales_low, 1) if sales_low == sales_high else [round(sales_low, 1), round(sales_high, 1)]
                trigger_evidence["estimated_sales_team_size_basis"] = sales_team_evidence
                trigger_evidence["estimated_sales_team_size_is_derived"] = sales_derived
                # The LOW end deciding it mirrors the revenue overlap rule above: a lean sales
                # function at this headcount would still satisfy the cap, so the ICP can still fit.
                if sales_low <= icp["sales_team_size_max"]:
                    if sales_derived:
                        reasons.append(f"plausible sales team size {round(sales_low, 1)}-{round(sales_high, 1)} (headcount proxy) can satisfy configured max ({icp['sales_team_size_max']})")
                    else:
                        reasons.append(f"measured sales team size ({round(sales_low, 1)}) within configured max ({icp['sales_team_size_max']})")
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


def verify_and_reconfirm_matches(db: Session, tenant_id: int, batch_id: int) -> dict:
    """Real revenue lookup + re-check for every company in this batch that matched an ICP on a
    DERIVED (headcount-proxy) revenue -- deletes the match (and the company) if the real number
    contradicts what the proxy allowed. Deliberately separate from run_icp_matching_sweep, which
    only ever evaluates the proxy; this is the step that checks whether the proxy was actually
    right, using the same estimate_company_revenue() waterfall (Google AI Overview, free/cheap,
    no Deepline) already used for partner tenants.

    Real gap this closes (2026-09-12): _needs_real_revenue_check() only ever fires for large
    IT-services/staffing-shaped companies -- it caught Centraprise, but nothing else. Every other
    ICP match, for any tenant, was trusted on the proxy alone. Confirmed live by manually
    auditing 17 companies that already had a contact: ASG matched Sales OS ($10-20M) on a proxy,
    real revenue is $25-50M, entirely above it; Symmetric Health Solutions matched Digital
    Playbook ($3-10M) on a proxy, real revenue is $1.7M, below every ICP's floor. Both had
    already been decision-maker-searched and were sitting live before this ran.

    A company whose revenue could not be found (estimate_company_revenue -> not_found) is left
    exactly as it was -- absence is not evidence of a bad fit, and this must never turn a real,
    proxy-consistent match into a false negative just because Google has nothing on a private
    company."""
    from app.gtm_os.icp.icp_config import get_icp_config
    from app.gtm_os.icp.revenue_estimation import estimate_company_revenue

    icp_config = get_icp_config(db, tenant_id)
    matches = (
        db.query(ICPMatch)
        .join(Company, ICPMatch.company_id == Company.id)
        .filter(Company.batch_id == batch_id, ICPMatch.tenant_id == tenant_id)
        .all()
    )
    checked_company_ids = {m.company_id for m in matches}

    kept, dropped = [], []
    for company_id in checked_company_ids:
        company = db.get(Company, company_id)
        if company is None:
            continue
        was_derived = company.estimated_revenue_lower_usd is None
        if not was_derived:
            continue  # already a real, reported figure -- nothing to re-check

        estimate_company_revenue(db, tenant_id, company)
        if company.estimated_revenue_lower_usd is None:
            continue  # genuinely not_found -- leave the proxy-based match standing, unresolved

        results = evaluate_icp_matches_for_company(company, icp_config)
        if any(r["matched"] for r in results):
            kept.append(company.name)
            continue

        dropped.append((company.name, company.estimated_revenue_lower_usd, company.estimated_revenue_higher_usd))
        db.query(ICPMatch).filter(ICPMatch.company_id == company_id).delete()
        db.query(Contact).filter(Contact.company_id == company_id).delete()
        db.delete(company)
        db.commit()

    return {"checked": len(checked_company_ids), "kept": kept, "dropped": dropped}


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


# An incomplete ICP verdict is retried because the company may get enriched between runs. With
# no limit on that, 737 of 804 companies were re-checked on EVERY run forever -- they crowded
# genuinely new companies out of this stage's own 500-row limit, and the funnel never converged
# (the UI showed them permanently stuck at "insufficient context").
#
# A company that lacked revenue/headcount yesterday almost never has it today: enrichment is
# weekly at best. So retries are spaced a week apart, and capped -- after ICP_MAX_EVALUATION_
# ATTEMPTS incomplete verdicts the company is left alone rather than re-checked for the rest of
# time. It is NOT deleted or marked bad: if it is ever enriched, revenue_backfill clears the
# incomplete flag and it becomes eligible again on its own.
ICP_RETRY_AFTER_DAYS = 7
ICP_MAX_EVALUATION_ATTEMPTS = 3


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
        .filter(
            # Never checked -- always eligible, and ordered first below.
            (Company.icp_last_evaluated_at.is_(None))
            | (
                # Incomplete, but only if it has waited a week AND has retries left.
                (Company.icp_last_evaluation_had_missing_information.is_(True))
                & (Company.icp_last_evaluated_at < datetime.utcnow() - timedelta(days=ICP_RETRY_AFTER_DAYS))
                & (Company.icp_evaluation_attempts < ICP_MAX_EVALUATION_ATTEMPTS)
            )
        )
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
                # Only an INCOMPLETE verdict burns an attempt -- a complete verdict is already
                # excluded from re-checking by the flag itself, so counting it would be noise.
                if had_missing_information:
                    company.icp_evaluation_attempts = (company.icp_evaluation_attempts or 0) + 1
                db.commit()

        except Exception:  # noqa: BLE001 -- one company's failure must never block the others
            db.rollback()  # 2026-08-26, same real fix as contact_discovery.py: never leave the shared session invalid for the next company
            counts["failed"] += 1

    return counts
