"""Pins the real bug found live 2026-09-26: $8.01 spent in one sweep against a configured
$0.50/day contact-discovery cap, 97 search_contact calls. Root cause was
_check_paid_fallback_budget constructing a FRESH BudgetGuard per opportunity -- its baseline
reset to the current balance on every call, so cumulative spend across a run was always ~$0 and
the daily cap was, in effect, never enforced. Fixed by delegating to check_daily_deepline_budget,
which persists a real UTC-day balance snapshot and compares against it.

This test proves the fix the way the bug actually manifested: two SEPARATE calls to
_check_paid_fallback_budget (standing in for two different Opportunities processed in the same
sweep), with the real balance dropping between them -- the second call must see the first call's
spend and correctly block once the cumulative total reaches the cap."""
import app.budget_guard as budget_guard
from app.db.models import Parameter
from app.gtm_os.sales.contact_discovery import _check_paid_fallback_budget

DAILY_CAP_USD = 0.50


def test_cumulative_spend_blocks_second_opportunity_once_cap_reached(db_factory, monkeypatch):
    db = db_factory([Parameter])
    balances = iter([10.00, 9.80, 9.40])  # one real balance read per call
    monkeypatch.setattr(budget_guard, "get_credit_balance_usd", lambda: next(balances))

    # First opportunity of the day: no baseline exists yet, so this call establishes it at
    # $10.00 and passes (nothing spent yet).
    allowed_1, reason_1 = _check_paid_fallback_budget(db, tenant_id=2, contact_budget_usd=DAILY_CAP_USD)
    assert allowed_1 is True
    assert reason_1 is None

    # Second opportunity: real balance has dropped to $9.80 (the first opportunity's actual
    # Deepline spend, $0.20) -- still under the $0.50 cap, so this must still pass. Before the
    # fix, this call would have constructed a fresh guard baselined at $9.80 itself, making
    # "spent today" look like $0 all over again.
    allowed_2, reason_2 = _check_paid_fallback_budget(db, tenant_id=2, contact_budget_usd=DAILY_CAP_USD)
    assert allowed_2 is True
    assert reason_2 is None

    # Third opportunity: balance now at $9.40 -- cumulative spend today is $0.60, past the
    # $0.50 cap. This is the exact case that silently passed before the fix.
    allowed_3, reason_3 = _check_paid_fallback_budget(db, tenant_id=2, contact_budget_usd=DAILY_CAP_USD)
    assert allowed_3 is False
    assert "daily cap" in reason_3.lower() or "reached" in reason_3.lower()


def test_single_opportunity_within_cap_still_allowed(db_factory, monkeypatch):
    db = db_factory([Parameter])
    monkeypatch.setattr(budget_guard, "get_credit_balance_usd", lambda: 10.00)

    allowed, reason = _check_paid_fallback_budget(db, tenant_id=2, contact_budget_usd=DAILY_CAP_USD)
    assert allowed is True
    assert reason is None
