"""Hard spend circuit-breaker for real, Deepline-billed phases. Checked after every
single company processed (not just once between phases, like the orchestrator used
to) -- a bug in any one phase's loop can then burn through at most one company's
worth of credits past the cap, not an entire batch's.

Real balance is re-fetched from Deepline itself on every check (never accumulated
locally from assumed per-call costs) -- same "trust the platform's own numbers, not
an estimate" discipline used everywhere else this project (see the page_size mistake
this was born from)."""

from app.deepline_client import get_credit_balance_usd


class BudgetExceededError(Exception):
    pass


class BudgetGuard:
    def __init__(self, budget_usd: float):
        self.budget_usd = budget_usd
        self.start_balance_usd = get_credit_balance_usd()

    def spent_so_far_usd(self) -> float:
        return self.start_balance_usd - get_credit_balance_usd()

    def check(self) -> None:
        spent = self.spent_so_far_usd()
        if spent >= self.budget_usd:
            raise BudgetExceededError(
                f"Run stopped: spent ${spent:.4f}, which reached this run's ${self.budget_usd:.2f} cap."
            )
