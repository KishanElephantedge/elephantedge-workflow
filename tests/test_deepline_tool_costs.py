"""DEEPLINE_TOOL_COST_USD -- every tool a governed run can call must have a price here, or
execute_tool() refuses it (fail-closed, not fail-open). Found live 2026-10-08: the first real
Crustdata test call was correctly BLOCKED at $0 spent because this entry didn't exist yet --
proof the guard works, and the reason this file exists, so the next new adapter's tool doesn't
get discovered missing the same way.
"""
from app.deepline_client import DEEPLINE_TOOL_COST_USD


def test_crustdata_v3_company_search_has_a_real_price():
    estimate = DEEPLINE_TOOL_COST_USD["crustdata_v3_company_search"]
    assert estimate({"limit": 20}) == 0.002 * 20
    assert estimate({}) == 0.002 * 20  # worst-case default when limit is omitted


def test_every_adapter_tool_this_play_can_call_has_a_price():
    # The exact class of gap this test exists to catch: an adapter registered in planner.py
    # whose actual tool id was never added here, which only surfaces as a live budget_blocked
    # outcome the first time someone tries to run it for real.
    for tool_id in ("icypeas_find_companies", "crustdata_v3_company_search"):
        assert tool_id in DEEPLINE_TOOL_COST_USD
