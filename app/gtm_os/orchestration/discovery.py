"""V2-owned discovery -- Phase 1 of the Autonomous GTM Flow build-out (see progress-log.md).

SOURCE SWITCHED 2026-09-16 (real incident, not a preference): this used to call V1's
app.phases.discovery.run_discovery() (Crustdata-based Company discovery) directly. That exact
function was already flagged unsafe in TODO.md on 2026-09-10 -- "confirmed live that its raw
data includes person names as company_name, foreign companies labelled hq_country USA, and $0
revenue bounds that pass any revenue filter... 20 of 20 companies it produced had to be
deleted" -- and this module kept calling it anyway, because the flag was written down but this
call site was never updated. Ran for real on 2026-09-16 (batch 129, target 15): every single
company was an Indian SMB with no US ICP relevance (Cosmetic Surgery India, Rockford Fosgate
(India), Axiom BPM Services PVT LTD, ...), all 15 deleted.

Now calls _run_apify_discovery_across_offerings() (app/phases/autonomous_orchestrator.py) --
the SAME per-offering-profile Apify pipeline this week's real work fixed and verified for V1
(the free public-LinkedIn-page check, the real ICP + revenue gate, Jobo-first decision-maker
resolution). One capability, two callers, same "Good" reuse pattern the module already followed
-- just pointed at the source that has actually been proven this week, not the one flagged
unsafe and never fixed.

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

from app.db.models import Batch
from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run, get_control_config
from app.phases.autonomous_orchestrator import _run_apify_discovery_across_offerings

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


ICP_GATE_TIMEOUT_SECONDS = 180

# 2026-09-22 -- REAL FIX for a live hang confirmed in run 152 (triggered 2026-09-21 04:00 UTC,
# never completed, zero spend movement for 28+ minutes, stage_results never reported once).
# run_v2_discovery_if_due (this function) has never had a timeout at all -- a known,
# pre-existing gap this project's own diagnosis flagged before this session started ("discovery
# has no timeout and runs on the shared aged DB session") -- and the ICP gate added the same day
# as run 152 (gate_batch_before_decision_makers, added below) put a real, unbounded, sequential
# per-company Deepline/Google revenue lookup inside that exact unbounded stage. Deepline's own
# subprocess timeout is 120s PER CALL; a handful of companies needing revenue enrichment,
# checked one at a time, easily exceeds any tick's real budget with nothing reported until it
# either finishes or the 120-minute stale-run recovery eventually kills the whole run.
#
# Bounded here, LOCALLY, rather than by wrapping the whole discovery stage (sweep.py's own
# _run_stage_with_timeout pattern) -- this is the one new, slow addition; the pre-existing keep
# loop above has run reliably for weeks without this failure mode, so the surgical fix is to
# bound the new call, not to restructure a stage that was not the problem.
#
# On timeout: the gate result is simply not applied. Discovery's own companies -- already found
# and already paid for -- are returned exactly as before this fix existed (the gate is an
# ADDITIVE safety check, not a precondition for discovery to succeed), same "a gate failure must
# not lose an otherwise-good discovery result" principle the surrounding try/except already
# established for a real exception. The abandoned thread may still be mid-write when this
# returns -- the SAME accepted, documented trade-off sweep.py's own _run_stage_with_timeout
# makes everywhere else in this codebase, not a new risk class introduced here.
def _run_icp_gate_with_timeout(tenant_id: int, batch_id: int, timeout_seconds: int = ICP_GATE_TIMEOUT_SECONDS) -> dict:
    import queue
    import threading

    from app.db.session import SessionLocal
    from app.gtm_os.icp.icp_matching import gate_batch_before_decision_makers

    result_queue: queue.Queue = queue.Queue(maxsize=1)

    def _target():
        gate_db = SessionLocal()
        try:
            result_queue.put(("ok", gate_batch_before_decision_makers(gate_db, tenant_id, batch_id)))
        except Exception as e:  # noqa: BLE001 -- reported through the queue, never crashes the thread silently
            result_queue.put(("error", str(e)))
        finally:
            gate_db.close()

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    try:
        status, value = result_queue.get(timeout=timeout_seconds)
    except queue.Empty:
        return {"status": "timed_out", "reason": f"ICP gate exceeded {timeout_seconds}s -- abandoned, discovery result kept as-is"}
    if status == "error":
        raise RuntimeError(value)
    return value


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
    # daily_budget_usd (V2's own discovery.daily_budget_usd) is no longer read here -- Apify
    # spend for this call is already gated by the real, tested check_apify_budget guard inside
    # run_apify_discovery() (control_config's own apify.daily_budget_usd), so a second,
    # differently-sourced budget number for the same spend would just be redundant/confusing,
    # not a real second layer of protection.

    batch = Batch(tenant_id=tenant_id, name=f"v2-discovery-{datetime.utcnow().isoformat()}", source=V2_DISCOVERY_BATCH_SOURCE)
    db.add(batch)
    db.commit()
    db.refresh(batch)

    try:
        result = _run_apify_discovery_across_offerings(batch, db, tenant_id, daily_target)
        status = "failed" if result.get("api_error") and result.get("companies_discovered") == 0 else "succeeded"

        # ICP GATE ADDED 2026-09-21. This path had no ICP/real-revenue check at all -- the
        # autonomous_orchestrator.py jd_first branch and the manual POST /gtm-os/companies/
        # {batch_id}/icp-gate route both call gate_batch_before_decision_makers() before any
        # contact spend, but the V2 sweep's own discovery went straight from "kept by the job
        # actor's headcount filter" to sensing/investigation/decision-maker work. Headcount
        # cannot predict revenue: gate_batch_before_decision_makers()'s own docstring records
        # batch 127 (2026-09-13), where 4 of 10 surviving companies were below every ICP's
        # revenue floor and the engine searched (and paid for) decision makers for all of them
        # before a real revenue figure ever arrived.
        #
        # Same uses_gtm_os_icp guard autonomous_orchestrator.py already uses for the jd_first
        # branch: Elephant Edge and any tenant with its OWN icp config get the real gate; a
        # tenant with no icp config row would otherwise silently inherit Elephant Edge's ICPs
        # via get_icp_config()'s fallback, which is meaningless for that tenant's real business
        # -- see the icp_matching.py-vs-partner_icp.py distinction recorded in TODO.md.
        if status == "succeeded" and result.get("companies_discovered", 0) > 0:
            from app.gtm_os.orchestration.discovery_profiles import ELEPHANT_EDGE_TENANT_ID as _EE, _has_own_icp_config

            if tenant_id == _EE or _has_own_icp_config(db, tenant_id):
                try:
                    # ICP_GATE_TIMEOUT_SECONDS read at call time, not baked in as the wrapped
                    # function's default -- a Python default argument is bound once at import,
                    # so passing it explicitly here is what makes the module-level constant
                    # actually reconfigurable (and testable) rather than frozen at import time.
                    icp_gate = _run_icp_gate_with_timeout(tenant_id, batch.id, timeout_seconds=ICP_GATE_TIMEOUT_SECONDS)
                    result["icp_gate"] = icp_gate
                except Exception as e:  # noqa: BLE001 -- gate failure must not lose an otherwise-good discovery result
                    result["icp_gate_error"] = str(e)

        return {"status": status, "batch_id": batch.id, "reason": reason, **result}
    except Exception as e:  # noqa: BLE001 -- a provider failure must not crash the sweep; see module docstring
        return {"status": "failed", "batch_id": batch.id, "error": str(e)}
