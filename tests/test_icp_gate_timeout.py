"""Tests for the ICP gate's timeout wrapper -- the real fix for run 152's hang.

Confirmed live before this fix: run 152 (triggered 2026-09-21 04:00 UTC) never completed, zero
spend movement for 28+ minutes, stage_results never reported once. The cause: the ICP gate added
the same day (gate_batch_before_decision_makers) does sequential, per-company Deepline/Google
revenue lookups (each up to Deepline's own 120s subprocess timeout) inside discovery's
already-unbounded stage. A handful of companies easily exceeds any real tick budget with nothing
reported until the 120-minute stale-run recovery eventually kills the whole run.
"""
import time

import pytest

from app.gtm_os.orchestration.discovery import _run_icp_gate_with_timeout


def test_a_fast_gate_returns_its_real_result(monkeypatch):
    import app.gtm_os.icp.icp_matching as icp_matching

    monkeypatch.setattr(
        icp_matching, "gate_batch_before_decision_makers",
        lambda db, tid, bid: {"eligible_company_ids": [1, 2], "dropped_on_real_revenue": []},
    )
    result = _run_icp_gate_with_timeout(tenant_id=2, batch_id=99, timeout_seconds=5)
    assert result["eligible_company_ids"] == [1, 2]


def test_a_slow_gate_times_out_instead_of_hanging_forever():
    """THE FIX. Before this, a gate call that never returned meant run_v2_discovery_if_due
    never returned either -- exactly what run 152 did for 28+ minutes and counting."""
    import app.gtm_os.icp.icp_matching as icp_matching
    import app.gtm_os.orchestration.discovery as discovery_module

    def _slow_gate(db, tid, bid):
        time.sleep(2)
        return {"eligible_company_ids": []}

    # Patch at the source module -- _run_icp_gate_with_timeout imports it fresh inside its own
    # thread function, so patching app.gtm_os.icp.icp_matching (not the discovery module's own
    # namespace) is what the thread will actually pick up.
    icp_matching.gate_batch_before_decision_makers = _slow_gate
    try:
        started = time.time()
        result = _run_icp_gate_with_timeout(tenant_id=2, batch_id=99, timeout_seconds=1)
        elapsed = time.time() - started
    finally:
        del icp_matching.gate_batch_before_decision_makers

    assert elapsed < 1.5, "must return promptly at the timeout, not wait for the slow call"
    assert result["status"] == "timed_out"
    assert "1s" in result["reason"], "must report the ACTUAL timeout used, not a hardcoded one"


def test_a_real_exception_is_raised_not_silently_swallowed():
    """A genuine bug in the gate itself must still surface -- this wrapper only bounds TIME,
    it does not turn a real error into a silent success."""
    import app.gtm_os.icp.icp_matching as icp_matching

    def _broken_gate(db, tid, bid):
        raise ValueError("real bug")

    icp_matching.gate_batch_before_decision_makers = _broken_gate
    try:
        with pytest.raises(RuntimeError, match="real bug"):
            _run_icp_gate_with_timeout(tenant_id=2, batch_id=99, timeout_seconds=5)
    finally:
        del icp_matching.gate_batch_before_decision_makers


def test_default_timeout_matches_the_documented_constant():
    from app.gtm_os.orchestration.discovery import ICP_GATE_TIMEOUT_SECONDS
    assert ICP_GATE_TIMEOUT_SECONDS == 180


def test_a_timeout_does_not_lose_the_surrounding_discovery_result(db_factory, monkeypatch):
    """End-to-end through run_v2_discovery_if_due: a gate that never returns must not prevent
    discovery's own companies (already found and already paid for) from being reported back."""
    from app.db.models import Batch, Company, Parameter
    import app.gtm_os.orchestration.discovery as discovery
    import app.gtm_os.icp.icp_matching as icp_matching

    db = db_factory([Batch, Company, Parameter])
    db.add(Parameter(tenant_id=2, key="gtm_os_control_config", value={
        "state": "running", "discovery": {"cadence_hours": 24, "daily_target": 5, "daily_budget_usd": 1.0},
    }))
    db.commit()
    monkeypatch.setattr(discovery, "is_discovery_due", lambda d, t: (True, "due"))

    def _fake_discovery(batch, d, tenant_id, target):
        d.add(Company(batch_id=batch.id, name="Acme", domain="acme.com"))
        d.commit()
        return {"companies_discovered": 1, "postings_checked": 5, "rejection_breakdown": {}, "api_error": None}

    monkeypatch.setattr(discovery, "_run_apify_discovery_across_offerings", _fake_discovery)
    # Fast timeout, deliberately-slow gate -- forces the timeout path within the test's own budget.
    monkeypatch.setattr(discovery, "ICP_GATE_TIMEOUT_SECONDS", 1)

    def _slow_gate(gate_db, tid, bid):
        time.sleep(3)
        return {"eligible_company_ids": []}

    icp_matching.gate_batch_before_decision_makers = _slow_gate
    try:
        result = discovery.run_v2_discovery_if_due(db, 2)
    finally:
        del icp_matching.gate_batch_before_decision_makers

    assert result["status"] == "succeeded"
    assert result["companies_discovered"] == 1
    assert result["icp_gate"]["status"] == "timed_out"
