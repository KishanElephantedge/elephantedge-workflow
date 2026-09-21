"""V2 discovery now calls gate_batch_before_decision_makers before any contact spend.

THE GAP. run_v2_discovery_if_due() created a batch, discovered companies, and returned --
nothing between that and sensing/investigation/decision-maker work checked ICP fit or real
revenue. gate_batch_before_decision_makers()'s own docstring records the concrete cost: batch
127 (2026-09-13) had 4 of 10 surviving companies below every ICP's revenue floor, invisible to
anything the job search itself returns, and the engine paid for decision-maker lookups on all of
them before a real revenue figure ever arrived. autonomous_orchestrator.py's jd_first branch
already called the gate; the V2 sweep's own discovery never did.
"""
import pytest

from app.db.models import Batch, Company, Parameter
from app.gtm_os.orchestration.discovery import run_v2_discovery_if_due

TENANT = 2


@pytest.fixture
def db(db_factory):
    return db_factory([Batch, Company, Parameter])


def _running_config(db, extra=None):
    cfg = {
        "state": "running",
        "discovery": {"cadence_hours": 24, "daily_target": 5, "daily_budget_usd": 1.0},
    }
    if extra:
        cfg.update(extra)
    db.add(Parameter(tenant_id=TENANT, key="gtm_os_control_config", value=cfg))
    db.commit()


def test_the_gate_runs_after_a_successful_discovery(db, monkeypatch):
    """THE FIX. A successful discovery that found companies must call the gate before returning."""
    import app.gtm_os.orchestration.discovery as discovery

    _running_config(db)
    monkeypatch.setattr(discovery, "is_discovery_due", lambda d, t: (True, "due"))

    def _fake_discovery(batch, db, tenant_id, target):
        company = Company(batch_id=batch.id, name="Acme", domain="acme.com")
        db.add(company)
        db.commit()
        return {"companies_discovered": 1, "postings_checked": 5, "rejection_breakdown": {}, "api_error": None}

    monkeypatch.setattr(discovery, "_run_apify_discovery_across_offerings", _fake_discovery)

    gate_calls = {"n": 0}

    def _fake_gate(db, tenant_id, batch_id, dry_run=False):
        gate_calls["n"] += 1
        return {"eligible_company_ids": [], "dropped_on_real_revenue": [], "unmatched_kept_for_review": [], "matched_on_proxy_revenue_only": []}

    monkeypatch.setattr("app.gtm_os.icp.icp_matching.gate_batch_before_decision_makers", _fake_gate)

    result = run_v2_discovery_if_due(db, TENANT)

    assert gate_calls["n"] == 1, "the ICP gate must run once discovery found companies"
    assert "icp_gate" in result


def test_the_gate_is_skipped_when_nothing_was_discovered(db, monkeypatch):
    """No companies -> nothing to gate. Must not error or call the gate pointlessly."""
    import app.gtm_os.orchestration.discovery as discovery

    _running_config(db)
    monkeypatch.setattr(discovery, "is_discovery_due", lambda d, t: (True, "due"))
    monkeypatch.setattr(
        discovery, "_run_apify_discovery_across_offerings",
        lambda batch, db, tenant_id, target: {"companies_discovered": 0, "postings_checked": 0, "rejection_breakdown": {}, "api_error": None},
    )

    gate_calls = {"n": 0}
    monkeypatch.setattr(
        "app.gtm_os.icp.icp_matching.gate_batch_before_decision_makers",
        lambda *a, **k: gate_calls.__setitem__("n", gate_calls["n"] + 1),
    )

    result = run_v2_discovery_if_due(db, TENANT)
    assert gate_calls["n"] == 0
    assert "icp_gate" not in result


def test_a_gate_failure_does_not_lose_an_otherwise_good_discovery_result(db, monkeypatch):
    """The companies were already found and paid for -- a bug in the gate itself must not
    discard that result. Same error-isolation discipline as everything else in this sweep."""
    import app.gtm_os.orchestration.discovery as discovery

    _running_config(db)
    monkeypatch.setattr(discovery, "is_discovery_due", lambda d, t: (True, "due"))

    def _fake_discovery(batch, db, tenant_id, target):
        db.add(Company(batch_id=batch.id, name="Acme", domain="acme.com"))
        db.commit()
        return {"companies_discovered": 1, "postings_checked": 5, "rejection_breakdown": {}, "api_error": None}

    monkeypatch.setattr(discovery, "_run_apify_discovery_across_offerings", _fake_discovery)

    def _broken_gate(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.gtm_os.icp.icp_matching.gate_batch_before_decision_makers", _broken_gate)

    result = run_v2_discovery_if_due(db, TENANT)
    assert result["status"] == "succeeded"
    assert result["companies_discovered"] == 1
    assert "icp_gate_error" in result


def test_a_partner_tenant_with_no_icp_config_is_not_gated(db, monkeypatch):
    """The gate reads get_icp_config(), which silently falls back to Elephant Edge's own ICPs
    for a tenant with no config row -- meaningless for that tenant's real business. Only run it
    for Elephant Edge or a tenant with a genuine icp config, same guard
    autonomous_orchestrator.py's jd_first branch already uses."""
    import app.gtm_os.orchestration.discovery as discovery

    PARTNER_TENANT = 9
    db.add(Parameter(tenant_id=PARTNER_TENANT, key="gtm_os_control_config", value={
        "state": "running", "discovery": {"cadence_hours": 24, "daily_target": 5, "daily_budget_usd": 1.0},
    }))
    db.commit()
    monkeypatch.setattr(discovery, "is_discovery_due", lambda d, t: (True, "due"))

    def _fake_discovery(batch, db, tenant_id, target):
        db.add(Company(batch_id=batch.id, name="Acme", domain="acme.com"))
        db.commit()
        return {"companies_discovered": 1, "postings_checked": 5, "rejection_breakdown": {}, "api_error": None}

    monkeypatch.setattr(discovery, "_run_apify_discovery_across_offerings", _fake_discovery)

    gate_calls = {"n": 0}
    monkeypatch.setattr(
        "app.gtm_os.icp.icp_matching.gate_batch_before_decision_makers",
        lambda *a, **k: gate_calls.__setitem__("n", gate_calls["n"] + 1),
    )

    result = run_v2_discovery_if_due(db, PARTNER_TENANT)
    assert gate_calls["n"] == 0
    assert "icp_gate" not in result
