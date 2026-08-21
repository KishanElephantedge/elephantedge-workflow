"""V2-owned discovery -- Phase 1 of the Autonomous GTM Flow build-out (see progress-log.md).

Calls V1's proven run_discovery() (Crustdata-based Company discovery, app/phases/discovery.py)
directly -- the capability's result returns straight into this function's own control flow.
This module never triggers, waits on, or inspects the V1 daily cron
(app/phases/autonomous_orchestrator.py::run_daily_autonomous_cycle); it is a second, independent
caller of the same underlying discovery capability, exactly the "Good" reuse pattern from the
approved architecture (V2 stage -> shared/proven V1 capability -> result returns directly into
V2 -> V2 continues).

V2 decides WHEN discovery runs (cadence/daily_target, both configured through the Phase 0
control plane -- app/gtm_os/orchestration/control.py) and enforces the existing BudgetGuard
around it. No default cadence or daily_target is invented here: both are None (unconfigured) by
default (see control.py's DEFAULT_GTM_OS_CONTROL_CONFIG), and discovery simply never runs until
an operator sets real values for both -- this is a deliberate business decision nobody has made
yet, not a technical constant to seed a guess for.

Budget: uses its OWN dedicated `discovery.daily_budget_usd` control-plane figure (2026-08-21
product decision), deliberately NOT V1's daily_credit_budget_usd Parameter -- the two budgets
are kept fully separate, not shared or aggregated. V1's own budget behavior is untouched by
this. No cross-run spend tracking is built (explicit instruction) -- V2 discovery simply has
its own independent cap, same as cadence_hours/daily_target: None (unconfigured) until an
operator sets a real number, never a guessed default."""
from datetime import datetime

from sqlalchemy.orm import Session

from app.budget_guard import BudgetGuard
from app.db.models import Batch
from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run, get_control_config
from app.phases.discovery import run_discovery

# Tags every Batch this module creates -- V1's own batches are always "deepline" or "jobo"
# (app/phases/autonomous_orchestrator.py::run_daily_autonomous_cycle), never this value, so
# Batch.source doubles as the cadence anchor below without a new marker table.
V2_DISCOVERY_BATCH_SOURCE = "v2_discovery"


def _last_v2_discovery_at(db: Session, tenant_id: int) -> datetime | None:
    batch = (
        db.query(Batch)
        .filter(Batch.tenant_id == tenant_id, Batch.source == V2_DISCOVERY_BATCH_SOURCE)
        .order_by(Batch.created_at.desc())
        .first()
    )
    return batch.created_at if batch else None


def is_discovery_due(db: Session, tenant_id: int, now: datetime | None = None) -> tuple[bool, str]:
    """Returns (due, reason). Never due while cadence_hours/daily_target/daily_budget_usd are
    unconfigured -- see module docstring for why no default is invented for any of them."""
    config = get_control_config(db, tenant_id)
    discovery_config = config.get("discovery") or {}
    cadence_hours = discovery_config.get("cadence_hours")
    daily_target = discovery_config.get("daily_target")
    daily_budget_usd = discovery_config.get("daily_budget_usd")
    if not cadence_hours or not daily_target or not daily_budget_usd:
        return False, "discovery cadence_hours/daily_target/daily_budget_usd not configured"

    last_run_at = _last_v2_discovery_at(db, tenant_id)
    if last_run_at is None:
        return True, "no prior V2 discovery run"

    now = now or datetime.utcnow()
    elapsed_hours = (now - last_run_at).total_seconds() / 3600
    if elapsed_hours < cadence_hours:
        return False, f"last run {elapsed_hours:.1f}h ago, cadence is {cadence_hours}h"
    return True, f"last run {elapsed_hours:.1f}h ago, cadence is {cadence_hours}h -- due"


def run_v2_discovery_if_due(db: Session, tenant_id: int) -> dict:
    """The Phase 1 entrypoint -- called once per hourly V2 intelligence sweep tick (see
    run_gtm_intelligence_sweep in sweep.py). Never raises: a provider failure here must not
    crash the rest of the hourly sweep, same error-isolation discipline every other sweep stage
    already follows.

    Redundant control-plane check (the hourly sweep's own caller already gates the whole tick
    on check_can_run) -- kept here too so this function stays safe to call directly (a manual
    test route, a future direct invocation) without relying on an outer caller to have already
    checked."""
    try:
        check_can_run(db, tenant_id)
    except ControlPlaneHalted as e:
        return {"status": "skipped", "reason": str(e)}

    due, reason = is_discovery_due(db, tenant_id)
    if not due:
        return {"status": "skipped", "reason": reason}

    config = get_control_config(db, tenant_id)
    daily_target = config["discovery"]["daily_target"]
    daily_budget_usd = config["discovery"]["daily_budget_usd"]

    batch = Batch(tenant_id=tenant_id, name=f"v2-discovery-{datetime.utcnow().isoformat()}", source=V2_DISCOVERY_BATCH_SOURCE)
    db.add(batch)
    db.commit()
    db.refresh(batch)

    try:
        guard = BudgetGuard(daily_budget_usd)
        result = run_discovery(batch.id, db, tenant_id, target=daily_target, budget_guard=guard)
        return {"status": "succeeded", "batch_id": batch.id, "reason": reason, **result}
    except Exception as e:  # noqa: BLE001 -- a provider failure must not crash the sweep; see module docstring
        return {"status": "failed", "batch_id": batch.id, "error": str(e)}
