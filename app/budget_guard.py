"""Hard spend circuit-breaker for real, Deepline-billed phases. Checked after every
single company processed (not just once between phases, like the orchestrator used
to) -- a bug in any one phase's loop can then burn through at most one company's
worth of credits past the cap, not an entire batch's.

Real balance is re-fetched from Deepline itself on every check (never accumulated
locally from assumed per-call costs) -- same "trust the platform's own numbers, not
an estimate" discipline used everywhere else this project (see the page_size mistake
this was born from)."""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.deepline_client import DeeplineError, get_credit_balance_usd


class BudgetExceededError(Exception):
    pass


class BudgetGuard:
    def __init__(self, budget_usd: float):
        self.budget_usd = budget_usd
        self.start_balance_usd = get_credit_balance_usd()

    def spent_so_far_usd(self) -> float:
        return self.start_balance_usd - get_credit_balance_usd()

    def check(self) -> None:
        # Fail SAFE, not fail silent or fail crashed: if the balance itself can't be
        # verified (get_credit_balance_usd already retries once internally -- this is
        # after that), we can't confirm we're still within budget, so stop the run
        # rather than either continuing unguarded or letting an uncaught error take
        # down the whole phase loop.
        try:
            spent = self.spent_so_far_usd()
        except DeeplineError as e:
            raise BudgetExceededError(f"Run stopped: could not verify remaining budget ({e}).") from e

        if spent >= self.budget_usd:
            raise BudgetExceededError(
                f"Run stopped: spent ${spent:.4f}, which reached this run's ${self.budget_usd:.2f} cap."
            )


_DAILY_BASELINE_PARAMETER_KEY = "deepline_daily_baseline"


def check_daily_deepline_budget(db: Session, tenant_id: int, budget_usd: float) -> None:
    """Raises BudgetExceededError if today's (UTC) real Deepline spend already reached
    `budget_usd`. Persists a real UTC-midnight balance snapshot in the `parameters` table and
    computes today's spend as (snapshot - current balance), instead of BudgetGuard's own
    from-this-instant baseline.

    Fixes a real, confirmed-live bug (2026-09-15): three call sites
    (company_resolution.py x2, investigation_execution.py) each did
    `BudgetGuard(budget_usd=X).check()` in the same breath -- creating the guard sets its
    baseline to the CURRENT balance, so spent_so_far_usd() is ~0 at the very next line, and the
    check can never actually fire. Each call re-created its own guard fresh, so no real
    cumulative spend was ever being compared against anything. This was not "a small ceiling
    per call" as the removed comments claimed -- it was no ceiling at all. A shared, longer-
    lived guard (the correct existing pattern in orchestration/discovery.py and
    sales/contact_discovery.py, where one instance is created once and its own .check() is
    called repeatedly across a real loop) isn't practical for these three call sites -- each is
    invoked from a different, independent point across a sweep, not one shared loop -- so this
    persists the baseline instead of holding it in memory.

    Clamps negative "spent" (a manual top-up mid-day) to 0 rather than treating it as
    unlimited headroom -- a top-up should reset the day's ceiling, not extend it indefinitely
    the way a naive baseline-still-in-the-past subtraction would."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    from app.db.models import Parameter  # local import: avoids a circular import at module load

    row = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id, Parameter.key == _DAILY_BASELINE_PARAMETER_KEY)
        .first()
    )
    try:
        current_balance = get_credit_balance_usd()
    except DeeplineError as e:
        raise BudgetExceededError(f"Run stopped: could not verify remaining budget ({e}).") from e

    if row is None or not isinstance(row.value, dict) or row.value.get("date") != today_str:
        # First check of a new UTC day (or ever) -- snapshot today's starting balance. No spend
        # is attributed to today yet, so this check always passes on the very first call of the
        # day, same as it should.
        if row is None:
            row = Parameter(tenant_id=tenant_id, key=_DAILY_BASELINE_PARAMETER_KEY, value={})
            db.add(row)
        row.value = {"date": today_str, "balance_usd": current_balance}
        db.commit()
        return

    spent_today = max(0.0, row.value["balance_usd"] - current_balance)
    if spent_today >= budget_usd:
        raise BudgetExceededError(
            f"Run stopped: today's real Deepline spend ${spent_today:.4f} reached the ${budget_usd:.2f} daily cap."
        )
