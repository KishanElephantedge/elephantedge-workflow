"""
Phase 5 — Buying Signal Intelligence, with Phase 8's job-posting finding folded in as a third
signal branch (see phase5-buying-signal-intelligence.md, Finding 4).

Stage 1 (this module): free/cheap, evaluated once per company at (or shortly after) Discovery
time. Stage 2 (thread_role, per Phase 4's Finding 2) is evaluated separately, after Decision
Maker Intelligence runs -- combined at Opportunity Scoring time (Phase 9), not here.
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.budget_guard import BudgetExceededError, BudgetGuard
from app.db.models import Company
from app.deepline_client import DeeplineError
from app.phases.hiring_signal import classify_hiring_signal

FUNDING_RECENCY_DAYS = 180  # 3-6 month window, per Phase 5's evidenced range
HEADCOUNT_GROWTH_THRESHOLD_PERCENT = 15  # carried forward from Synefi's unvalidated design


def _has_recent_funding(company: Company) -> bool:
    if not company.last_funding_date:
        return False
    return company.last_funding_date >= datetime.utcnow() - timedelta(days=FUNDING_RECENCY_DAYS)


def _has_headcount_growth(company: Company) -> bool:
    return (company.headcount_growth_12m_percent or 0) > HEADCOUNT_GROWTH_THRESHOLD_PERCENT


def check_buying_signal(company: Company, db: Session) -> dict:
    """Evaluates Stage 1 for one company and persists the result. Idempotent via
    buying_signal_checked_at -- callers should skip companies already checked, same pattern as
    Decision Maker's decision_maker_searched_at.

    The job-posting check (the only one of the three signals that depends on an external,
    separately-billed provider) is allowed to fail without losing the other two free signals --
    found live: Bloomberry's own upstream account ran out of its monthly quota, which would
    otherwise have silently blocked funding/growth from ever being recorded too."""
    recent_funding = _has_recent_funding(company)
    headcount_growth = _has_headcount_growth(company)

    job_posting_error = None
    hiring_signal_result = None
    active_posting = None
    try:
        hiring_signal_result = classify_hiring_signal(company, db)
        active_posting = hiring_signal_result["role"] is not None
    except DeeplineError as e:
        job_posting_error = str(e)

    company.active_head_of_sales_posting = active_posting
    company.buying_signal_checked_at = datetime.utcnow()
    db.commit()

    return {
        "recent_funding_event": recent_funding,
        "headcount_growth_12m_percent": company.headcount_growth_12m_percent,
        "active_head_of_sales_posting": active_posting,
        "hiring_signal": hiring_signal_result,
        "job_posting_check_error": job_posting_error,
        "stage_1_signal_present": recent_funding or headcount_growth or bool(active_posting),
    }


def run_buying_signal_check(batch_id: int, db: Session, budget_guard: BudgetGuard | None = None) -> dict:
    """Phase 5 entrypoint. Skips companies already checked -- the job-posting check is billed
    (free when no match, but still a real call), so re-checking on every re-run would waste
    calls on companies whose signal status won't have meaningfully changed same-day.

    budget_guard, if given, is checked after every company -- stops the loop (rather than
    raising out of it) the moment this run's hard spend cap is reached, leaving already-checked
    companies' results in place."""
    companies = db.query(Company).filter(Company.batch_id == batch_id).all()

    checked = 0
    signal_present = 0
    skipped = 0
    job_posting_errors = []
    budget_stopped_early = False
    for company in companies:
        if company.buying_signal_checked_at:
            skipped += 1
            continue
        result = check_buying_signal(company, db)
        checked += 1
        if result["stage_1_signal_present"]:
            signal_present += 1
        if result["job_posting_check_error"]:
            job_posting_errors.append(f"{company.name}: {result['job_posting_check_error']}")

        if budget_guard is not None:
            try:
                budget_guard.check()
            except BudgetExceededError:
                budget_stopped_early = True
                break

    return {
        "companies_checked": checked,
        "companies_with_signal": signal_present,
        "companies_skipped_already_checked": skipped,
        "job_posting_check_errors": job_posting_errors,
        "budget_stopped_early": budget_stopped_early,
    }
