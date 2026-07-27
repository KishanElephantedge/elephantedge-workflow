"""
Elephant Edge's daily autonomous cycle: Discovery (Phase 3) -> dedupe -> Buying Signal (Phase 5/8)
-> Tech Stack -> Scoring (Phase 9) -> select top N -> Decision Maker (Phase 6, which also
triggers HubSpot sync and reveals thread_role for Stage 2 of the signal).

The cycle then PAUSES for a 1-hour approval window before Outreach (Phase 12) -- an email is
sent with a CSV of the decision-makers found, and nothing gets pushed to the outreach channel
until that window elapses, unless the run is explicitly cancelled first. A separate periodic
sweep (not the one-shot daily trigger) resumes any run whose window has passed -- this also
makes it resilient to a server restart mid-window, since the sweep re-checks on every tick
rather than relying on a single scheduled callback surviving.

Same concurrency-lock, stale-run-recovery, and budget-cap discipline as Synefi's orchestrator --
deliberately mirrored, not reinvented, since that design is already proven in production.
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.budget_guard import BudgetGuard
from app.db.models import AutonomousRun, Batch, Company, Parameter, Score
from app.email_client import EmailError, send_email
from app.export import generate_decision_makers_csv
from app.phases.buying_signal import run_buying_signal_check
from app.phases.campaign_execution import run_campaign_execution
from app.phases.decision_maker import run_decision_maker_id
from app.phases.discovery import run_discovery
from app.phases.scoring import run_scoring
from app.phases.tech_stack import run_tech_stack_check
from app.outreach.selector import get_outreach_channel

DEFAULT_DAILY_COMPANY_CAP = 5
DEFAULT_DAILY_BUDGET_USD = 1.0
DISCOVERY_PAGE_SIZE = 10
DISCOVERY_MAX_CHECKED = 50
STALE_RUN_TIMEOUT_MINUTES = 120
APPROVAL_WINDOW_MINUTES = 60


def _get_tenant_param(db: Session, tenant_id: int, key: str) -> Parameter | None:
    return (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == key)
        .first()
    )


def is_autonomous_enabled(db: Session, tenant_id: int) -> bool:
    param = _get_tenant_param(db, tenant_id, "autonomous_enabled")
    return bool(param and param.value and param.value.get("enabled") is True)


def get_daily_budget_usd(db: Session, tenant_id: int) -> float:
    param = _get_tenant_param(db, tenant_id, "daily_credit_budget_usd")
    if param and param.value and "budget_usd" in param.value:
        return float(param.value["budget_usd"])
    return DEFAULT_DAILY_BUDGET_USD


def get_daily_company_cap(db: Session, tenant_id: int) -> int:
    param = _get_tenant_param(db, tenant_id, "daily_company_cap")
    if param and param.value and "cap" in param.value:
        return int(param.value["cap"])
    return DEFAULT_DAILY_COMPANY_CAP


def _clear_stale_running_flags(db: Session, tenant_id: int) -> None:
    cutoff = datetime.utcnow() - timedelta(minutes=STALE_RUN_TIMEOUT_MINUTES)
    stale_runs = (
        db.query(AutonomousRun)
        .join(Batch)
        .filter(Batch.tenant_id == tenant_id)
        .filter(AutonomousRun.status == "running")
        .filter(AutonomousRun.started_at < cutoff)
        .all()
    )
    for run in stale_runs:
        run.status = "failed"
        run.error_message = "Marked stale: exceeded max expected run time, likely a server restart mid-cycle"
        run.completed_at = datetime.utcnow()
    if stale_runs:
        db.commit()


def _dedupe_against_prior_days(batch: Batch, db: Session) -> int:
    todays_companies = db.query(Company).filter(Company.batch_id == batch.id).all()
    removed = 0
    for company in todays_companies:
        if not company.domain:
            continue
        prior_exists = (
            db.query(Company)
            .join(Batch)
            .filter(Company.domain == company.domain)
            .filter(Batch.tenant_id == batch.tenant_id)
            .filter(Company.batch_id != batch.id)
            .filter(Batch.status != "failed")
            .first()
        )
        if prior_exists:
            db.delete(company)
            removed += 1
    db.commit()
    return removed


def _select_top_companies(batch: Batch, db: Session, cap: int) -> int:
    """Real quality gate, not just a volume cap: a company scoring below Cool (tier ==
    "excluded", total_score < 70) never proceeds past this point, regardless of the daily
    cap -- firmographic fit + a bare hiring-signal match alone was previously enough to
    reach Decision Maker and Outreach (real batches showed this happen), even though the
    company's own computed score said it wasn't a good fit. This is the fix: the score
    that was already being calculated is now actually enforced, not just recorded.

    Excluded-tier companies are removed unconditionally first. Among the rest (hot/warm/
    cool), only hot/warm are capped to the day's target count -- cool-tier companies pass
    through uncapped, since the cap exists to bound *volume* of the best prospects, not to
    additionally restrict companies that already cleared the real quality bar."""
    excluded_companies = (
        db.query(Company)
        .join(Score)
        .filter(Company.batch_id == batch.id)
        .filter(Score.tier == "excluded")
        .all()
    )
    for company in excluded_companies:
        db.delete(company)
    db.commit()

    scored = (
        db.query(Company)
        .join(Score)
        .filter(Company.batch_id == batch.id)
        .filter(Score.tier.in_(["hot", "warm"]))
        .order_by(Score.total_score.desc())
        .all()
    )
    keep = scored[:cap]
    drop = scored[cap:]
    for company in drop:
        db.delete(company)
    db.commit()
    return len(keep)


def _send_approval_notification(batch: Batch, run: AutonomousRun, decision_maker_result: dict, db: Session, tenant_id: int) -> str | None:
    """Sent right after Decision Maker completes, before anything reaches the outreach
    channel. Returns an error string if sending failed -- never raised, since a failed
    notification must not prevent the approval window itself from being real and honored."""
    try:
        csv_bytes = generate_decision_makers_csv(batch.id, db)
        body = (
            f"Elephant Edge autonomous run for batch #{batch.id} found "
            f"{decision_maker_result['decision_makers_found']} decision-maker(s) across "
            f"{decision_maker_result['companies_checked']} companies checked today.\n\n"
            f"These will be pushed to the configured outreach channel in {APPROVAL_WINDOW_MINUTES} minutes "
            f"unless this run is cancelled first (Autonomous page in the dashboard).\n\n"
            f"See the attached CSV for the full list of companies and decision-makers found."
        )
        send_email(
            subject=f"Elephant Edge: {decision_maker_result['decision_makers_found']} decision-maker(s) ready for review (batch #{batch.id})",
            body=body,
            db=db,
            tenant_id=tenant_id,
            attachment_bytes=csv_bytes,
            attachment_filename=f"batch-{batch.id}-decision-makers.csv",
        )
        return None
    except EmailError as e:
        return str(e)


def _send_success_notification(batch: Batch, outreach_result: dict, db: Session, tenant_id: int) -> str | None:
    try:
        body = (
            f"Batch #{batch.id}: {outreach_result['pushed']} contact(s) successfully added to the "
            f"outreach campaign. ({outreach_result['failed']} failed, {outreach_result['skipped']} skipped "
            f"as already pushed.)"
        )
        send_email(
            subject=f"Elephant Edge: {outreach_result['pushed']} lead(s) successfully added to campaign (batch #{batch.id})",
            body=body,
            db=db,
            tenant_id=tenant_id,
        )
        return None
    except EmailError as e:
        return str(e)


def run_daily_autonomous_cycle(db: Session, tenant_id: int) -> dict:
    if not is_autonomous_enabled(db, tenant_id):
        return {"status": "skipped", "reason": "autonomous_enabled is off"}

    _clear_stale_running_flags(db, tenant_id)
    already_running = (
        db.query(AutonomousRun)
        .join(Batch)
        .filter(Batch.tenant_id == tenant_id)
        .filter(AutonomousRun.status.in_(["running", "awaiting_approval"]))
        .first()
    )
    if already_running:
        return {
            "status": "skipped",
            "reason": "a cycle is already running or awaiting approval",
            "running_since": already_running.started_at,
        }

    batch = Batch(tenant_id=tenant_id, name=f"autonomous-{datetime.utcnow().date().isoformat()}")
    db.add(batch)
    db.commit()
    db.refresh(batch)

    run = AutonomousRun(batch_id=batch.id, status="running")
    db.add(run)
    db.commit()
    db.refresh(run)

    budget_usd = get_daily_budget_usd(db, tenant_id)

    try:
        guard = BudgetGuard(budget_usd)  # one guard for the whole run -- shared cap across every phase
        cap = get_daily_company_cap(db, tenant_id)
        discovery_result = run_discovery(batch.id, db, tenant_id, target=cap, page_size=DISCOVERY_PAGE_SIZE, max_checked=DISCOVERY_MAX_CHECKED, budget_guard=guard)
        removed_dupes = _dedupe_against_prior_days(batch, db)
        run_buying_signal_check(batch.id, db, budget_guard=guard)
        run_tech_stack_check(batch.id, db, budget_guard=guard)
        run_scoring(batch.id, db)
        selected = _select_top_companies(batch, db, cap)

        budget_stopped_early = guard.spent_so_far_usd() >= budget_usd

        if budget_stopped_early:
            decision_maker_result = {"companies_checked": 0, "decision_makers_found": 0, "companies_with_no_contact": 0, "companies_skipped_already_resolved": 0, "hubspot_synced": 0, "hubspot_errors": [], "budget_stopped_early": True}
        else:
            decision_maker_result = run_decision_maker_id(batch.id, db, tenant_id, budget_guard=guard)
            budget_stopped_early = budget_stopped_early or decision_maker_result["budget_stopped_early"]

        final_spend = guard.spent_so_far_usd()

        run.companies_discovered = discovery_result["companies_discovered"]
        run.companies_selected = selected
        run.contacts_found = decision_maker_result["decision_makers_found"]
        run.credits_spent_usd = final_spend
        run.budget_stopped_early = budget_stopped_early

        if budget_stopped_early or decision_maker_result["decision_makers_found"] == 0:
            # Nothing to push -- no approval window needed, nothing to notify about.
            batch.current_phase = "autonomous_cycle_done"
            batch.status = "complete"
            run.status = "completed"
            run.contacts_pushed = 0
            run.completed_at = datetime.utcnow()
            db.commit()
            return {
                "status": "completed",
                "batch_id": batch.id,
                "companies_discovered": discovery_result["companies_discovered"],
                "companies_checked_by_discovery": discovery_result["companies_checked"],
                "discovery_rejection_breakdown": discovery_result["rejection_breakdown"],
                "duplicates_removed": removed_dupes,
                "companies_selected": selected,
                "decision_maker_result": decision_maker_result,
                "outreach_result": {"contacts_checked": 0, "pushed": 0, "failed": 0, "skipped": 0},
                "credits_spent_usd": final_spend,
                "budget_stopped_early": budget_stopped_early,
                "budget_usd": budget_usd,
            }

        # Real decision-makers found and budget available -- pause for the approval window
        # rather than pushing to the outreach channel immediately.
        batch.current_phase = "awaiting_approval"
        run.status = "awaiting_approval"
        run.awaiting_approval_until = datetime.utcnow() + timedelta(minutes=APPROVAL_WINDOW_MINUTES)
        db.commit()

        notification_error = _send_approval_notification(batch, run, decision_maker_result, db, tenant_id)

        return {
            "status": "awaiting_approval",
            "batch_id": batch.id,
            "companies_discovered": discovery_result["companies_discovered"],
            "companies_checked_by_discovery": discovery_result["companies_checked"],
            "discovery_rejection_breakdown": discovery_result["rejection_breakdown"],
            "duplicates_removed": removed_dupes,
            "companies_selected": selected,
            "decision_maker_result": decision_maker_result,
            "awaiting_approval_until": run.awaiting_approval_until,
            "notification_error": notification_error,
            "credits_spent_usd": final_spend,
            "budget_usd": budget_usd,
        }
    except Exception as e:
        db.rollback()
        run.status = "failed"
        run.error_message = str(e)
        run.completed_at = datetime.utcnow()
        batch.status = "failed"
        db.add(run)
        db.add(batch)
        db.commit()
        return {"status": "failed", "batch_id": batch.id, "error": str(e)}


def cancel_run(run_id: int, db: Session, tenant_id: int) -> dict:
    """Marks a run cancelled while it's still in its approval window -- the periodic resume
    sweep checks this flag before ever pushing to the outreach channel."""
    run = (
        db.query(AutonomousRun)
        .join(Batch)
        .filter(AutonomousRun.id == run_id)
        .filter(Batch.tenant_id == tenant_id)
        .first()
    )
    if not run:
        return {"status": "not_found"}
    if run.status != "awaiting_approval":
        return {"status": "not_cancellable", "reason": f"run status is '{run.status}', not 'awaiting_approval'"}
    run.cancelled = True
    run.status = "cancelled"
    run.completed_at = datetime.utcnow()
    db.commit()
    return {"status": "cancelled"}


def resume_pending_approvals(db: Session, tenant_id: int) -> dict:
    """Periodic sweep (called every few minutes, not just once) -- resumes any run whose
    approval window has elapsed and wasn't cancelled. Checking on every tick rather than
    relying on a single one-shot scheduled callback means a server restart mid-window doesn't
    lose the resume -- the next tick after restart picks it up."""
    due_runs = (
        db.query(AutonomousRun)
        .join(Batch)
        .filter(Batch.tenant_id == tenant_id)
        .filter(AutonomousRun.status == "awaiting_approval")
        .filter(AutonomousRun.awaiting_approval_until <= datetime.utcnow())
        .all()
    )
    resumed = []
    for run in due_runs:
        batch = run.batch
        try:
            channel = get_outreach_channel(db, tenant_id)
            outreach_result = run_campaign_execution(batch.id, db, channel)

            batch.current_phase = "outreach_done"
            batch.status = "complete"
            run.status = "completed"
            run.contacts_pushed = outreach_result["pushed"]
            run.completed_at = datetime.utcnow()
            db.commit()

            _send_success_notification(batch, outreach_result, db, tenant_id)
            resumed.append({"run_id": run.id, "batch_id": batch.id, "pushed": outreach_result["pushed"]})
        except Exception as e:
            db.rollback()
            run.status = "failed"
            run.error_message = str(e)
            run.completed_at = datetime.utcnow()
            db.commit()

    return {"resumed": resumed}
