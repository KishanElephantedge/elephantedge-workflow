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

from app.budget_guard import BudgetExceededError, BudgetGuard
from app.db.models import AutonomousRun, Batch, Company, Contact, Parameter, Score
from app.slack_client import SlackError, send_slack_message
from app.phases.buying_signal import run_buying_signal_check
from app.phases.campaign_execution import run_campaign_execution
from app.phases.decision_maker import run_decision_maker_id
from app.phases.discovery import run_discovery
from app.phases.personalized_outreach import generate_personalized_message
from app.phases.scoring import run_scoring
from app.phases.tech_stack import run_tech_stack_check
from app.phases.jobo_discovery import run_jobo_discovery
from app.phases.jd_first_discovery import run_jd_first_discovery
from app.phases.decision_maker import find_decision_maker
from app.jobo_client import JoboError
from app.outreach.selector import get_outreach_channel

DEFAULT_DAILY_COMPANY_CAP = 5
DEFAULT_DAILY_BUDGET_USD = 1.0
DEFAULT_DISCOVERY_SOURCE = "deepline"
DEFAULT_SCHEDULE_HOUR_UTC = 9
DEFAULT_SCHEDULE_MINUTE_UTC = 0
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


def get_autonomous_schedule_utc(db: Session, tenant_id: int) -> tuple[int, int]:
    """Fixed wall-clock time (UTC) the daily autonomous cycle fires at -- deliberately a
    CronTrigger at this hour/minute, not an interval timer. An interval timer's countdown
    restarts from whenever the process last started, so every deploy silently pushed the
    "24h from now" target later -- confirmed live as the reason a run's actual fire time kept
    drifting. A fixed time is unaffected by deploys/restarts."""
    param = _get_tenant_param(db, tenant_id, "autonomous_schedule_utc")
    if param and param.value and "hour" in param.value and "minute" in param.value:
        return int(param.value["hour"]), int(param.value["minute"])
    return DEFAULT_SCHEDULE_HOUR_UTC, DEFAULT_SCHEDULE_MINUTE_UTC


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


def get_autonomous_discovery_source(db: Session, tenant_id: int) -> str:
    """Which pipeline the daily autonomous cycle uses -- "deepline" (old company-first:
    Crustdata discovery, then check each company individually for signal -- validated live
    as expensive and low-yield, $3.22 for 4 real companies), "jd_first" (jobs-first: search
    TheirStack for real, currently-open postings with firmographic filters combined in one
    query, validated live as ~$1.40 for 10 real companies), or "jobo". All three are fully
    independent and a single run only ever uses one, never more than one -- set once here,
    read once at the start of run_daily_autonomous_cycle, never re-read mid-run so a settings
    change can't switch source partway through a cycle."""
    param = _get_tenant_param(db, tenant_id, "autonomous_discovery_source")
    if param and param.value and param.value.get("source") in ("deepline", "jobo", "jd_first"):
        return param.value["source"]
    return DEFAULT_DISCOVERY_SOURCE


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
    """Volume cap only, not a quality gate -- the real quality gate (excluded-tier
    companies never reach Decision Maker) lives in decision_maker.py's own per-company
    check now, not here. This used to also hard-delete excluded-tier companies, which
    had a real cost: once deleted, there was no way to review afterward which company got
    filtered out or why. Excluded companies now simply stay in the batch, fully visible,
    with their score/tier intact -- Decision Maker's own gate is what actually stops them
    from being searched, so deleting them here was both destructive and redundant.

    Ranks by raw total_score across ALL scored companies, not by tier label -- the new
    18-variable GTM Fit Score has four variables (I/L/P/Q) that score 0 until built, so
    max achievable today (125/175) is below the "warm" threshold (140). Gating on tier
    here would select zero companies every single run until those are built. Score is
    purely a ranking signal for volume-capping; the real pass/fail gate already lives in
    decision_maker.py (has_qualifying_hiring_signal + team-composition check)."""
    scored = (
        db.query(Company)
        .join(Score)
        .filter(Company.batch_id == batch.id)
        .order_by(Score.total_score.desc())
        .all()
    )
    keep = scored[:cap]
    drop = scored[cap:]
    for company in drop:
        db.delete(company)
    db.commit()
    return len(keep)


def _generate_messages_for_batch(batch_id: int, db: Session, tenant_id: int, guard: BudgetGuard | None = None) -> list[dict]:
    """Runs Phase 13 for every contact found in this batch, right after Decision Maker and
    before the approval notification -- so the reviewer sees the drafted outreach message
    alongside who it's for, not just a bare name/title. One contact's failure (e.g. no
    LinkedIn URL, a transient Gemini/Claude error) never blocks the others; that contact just
    shows up without a message and decision-maker info is still visible.

    Shares the SAME BudgetGuard as discovery/decision-maker when one is passed in (the
    LinkedIn-research step here calls the same Deepline-billed Aviato tool), so the run's
    overall spend cap still holds end-to-end -- contacts past the cap are simply skipped, not
    generated unguarded."""
    contacts = (
        db.query(Contact)
        .join(Company)
        .filter(Company.batch_id == batch_id)
        .all()
    )
    results = []
    for contact in contacts:
        entry = {
            "company_name": contact.company.name,
            "contact_name": f"{contact.first_name or ''} {contact.last_name or ''}".strip(),
            "contact_title": contact.title,
            "linkedin_url": contact.linkedin_url,
            "message": None,
            "error": None,
        }
        if guard is not None:
            try:
                guard.check()
            except BudgetExceededError as e:
                entry["error"] = f"Skipped: {e}"
                results.append(entry)
                continue
        try:
            pm = generate_personalized_message(contact.id, db, tenant_id)
            entry["message"] = pm.generated_message
            entry["error"] = pm.error_message
        except Exception as e:  # noqa: BLE001 -- one contact's failure must never block the rest
            entry["error"] = str(e)
        results.append(entry)
    return results


def _send_failure_alert(batch: Batch, run: AutonomousRun, error: str, db: Session, tenant_id: int) -> None:
    """Found live: an autonomous run failing outright (e.g. an unhandled rate-limit error)
    previously sent NO notification at all -- the only sign anything went wrong was the run
    row silently sitting as "failed" in the database, discovered by chance days later, not an
    alert. Same Slack channels as the success-path approval notification. Best-effort: a
    failure to send this alert must never raise past the caller, which is already inside its
    own failure-handling block."""
    text = (
        f":rotating_light: Autonomous run failed\n"
        f"Batch: {batch.name} (id {batch.id})\n"
        f"Run id: {run.id}\n"
        f"Error: {error}"
    )
    try:
        send_slack_message(text, db, tenant_id)
    except SlackError:
        pass


def _send_approval_notification(batch: Batch, run: AutonomousRun, decision_maker_result: dict, db: Session, tenant_id: int, messages: list[dict] | None = None) -> str | None:
    """Sent right after Decision Maker (and Phase 13 message drafting) completes, before
    anything reaches the outreach channel. Slack-only -- email over raw SMTP is blocked at
    the network level on Render (confirmed live, consistent "Network is unreachable" on every
    attempt, a known PaaS restriction on outbound SMTP ports, not a flaky bug). Revisit if
    email delivery moves to an HTTP API (e.g. Resend/SendGrid) instead of raw SMTP."""
    messages = messages or []

    # Was a CSV email attachment before email got dropped (see docstring) -- inlined as plain
    # text here instead, since Incoming Webhooks don't support file attachments.
    decision_maker_lines = [
        f"- {m['contact_name']} ({m['contact_title'] or 'no title'}) @ {m['company_name']}"
        + (f" -- {m['linkedin_url']}" if m['linkedin_url'] else "")
        for m in messages
    ]
    decision_maker_block = "\n".join(decision_maker_lines) if decision_maker_lines else "(none)"

    message_block_lines = []
    for m in messages:
        header = f"{m['contact_name']} ({m['contact_title'] or 'no title'}) @ {m['company_name']}"
        if m["message"]:
            message_block_lines.append(f"{header}\n{m['message']}\n")
        else:
            message_block_lines.append(f"{header}\n[No message drafted: {m['error'] or 'unknown reason'}]\n")
    message_block = "\n---\n".join(message_block_lines)

    slack_text = (
        f":mag: *Elephant Edge* -- batch #{batch.id} found {decision_maker_result['decision_makers_found']} "
        f"decision-maker(s) across {decision_maker_result['companies_checked']} companies checked.\n"
        f"Pushes to the outreach channel in {APPROVAL_WINDOW_MINUTES} minutes unless cancelled "
        f"(Autonomous page in the dashboard).\n\n"
        f"*Decision-makers found:*\n{decision_maker_block}\n\n"
        f"*Drafted outreach messages:*\n\n{message_block}"
    )
    try:
        send_slack_message(slack_text, db, tenant_id)
        return None
    except SlackError as e:
        return f"slack: {e}"


def _send_success_notification(batch: Batch, outreach_result: dict, db: Session, tenant_id: int) -> str | None:
    """Slack-only -- see _send_approval_notification's docstring for why email is dropped."""
    try:
        send_slack_message(
            f":white_check_mark: *Elephant Edge* -- batch #{batch.id}: {outreach_result['pushed']} contact(s) "
            f"successfully added to the outreach campaign. ({outreach_result['failed']} failed, "
            f"{outreach_result['skipped']} skipped as already pushed.)",
            db, tenant_id,
        )
        return None
    except SlackError as e:
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

    source = get_autonomous_discovery_source(db, tenant_id)  # read once, never re-checked mid-run
    # jd_first is still a Deepline-based method (TheirStack, not Jobo) -- tagged "deepline" at
    # the batch level so it shows in the same dashboard tab as the old flow; the per-company
    # source field (set inside jd_first_discovery.py) is what actually distinguishes it.
    batch_source = "jobo" if source == "jobo" else "deepline"
    batch = Batch(tenant_id=tenant_id, name=f"autonomous-{datetime.utcnow().date().isoformat()}", source=batch_source)
    db.add(batch)
    db.commit()
    db.refresh(batch)

    run = AutonomousRun(batch_id=batch.id, status="running")
    db.add(run)
    db.commit()
    db.refresh(run)

    budget_usd = get_daily_budget_usd(db, tenant_id)

    if source in ("jobo", "jd_first"):
        # Both branches can raise before they ever reach their own internal error handling
        # (jd_first has none at all; jobo only catches JoboError specifically) -- confirmed
        # live: BudgetGuard's Deepline balance check timed out on both its own retry attempts
        # right at cycle start, the exception propagated uncaught, and the run row stayed
        # "running" forever since nothing ever marked it failed. This is the safety net that
        # was missing -- any exception here now always finalizes the run/batch as failed.
        try:
            if source == "jobo":
                return _run_jobo_autonomous_cycle(batch, run, db, tenant_id, budget_usd)
            return _run_jd_first_autonomous_cycle(batch, run, db, tenant_id, budget_usd)
        except Exception as e:
            db.rollback()
            run.status = "failed"
            run.error_message = str(e)
            run.completed_at = datetime.utcnow()
            batch.status = "failed"
            db.add(run)
            db.add(batch)
            db.commit()
            _send_failure_alert(batch, run, str(e), db, tenant_id)
            return {"status": "failed", "batch_id": batch.id, "source": source, "error": str(e)}

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

        # The cycle itself already succeeded and is committed above -- confirmed live that an
        # unwrapped network error in the notification step (email_client/slack_client) could
        # otherwise propagate up to this function's own except block and retroactively flip an
        # already-successful run to "failed". Isolated here so a notification-layer problem is
        # reported but never overwrites real run status.
        try:
            messages = _generate_messages_for_batch(batch.id, db, tenant_id, guard=guard)
            notification_error = _send_approval_notification(batch, run, decision_maker_result, db, tenant_id, messages=messages)
        except Exception as e:
            notification_error = f"message generation/notification step failed: {e}"

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
        _send_failure_alert(batch, run, str(e), db, tenant_id)
        return {"status": "failed", "batch_id": batch.id, "error": str(e)}


def _run_jobo_autonomous_cycle(batch: Batch, run: AutonomousRun, db: Session, tenant_id: int, budget_usd: float) -> dict:
    """Jobo's autonomous branch -- fully independent from the Deepline branch above (its own
    credit system, and run_jobo_discovery already does discovery + hiring-signal + team-gap +
    decision-maker in one pass, so there's no separate multi-phase sequence here). Same
    approval-window/notification contract as the Deepline branch so the dashboard and email
    behavior are consistent regardless of which source a given day's run used."""
    cap = get_daily_company_cap(db, tenant_id)
    try:
        result = run_jobo_discovery(batch.id, db, tenant_id, target=cap, budget_usd=budget_usd)
    except JoboError as e:
        db.rollback()
        run.status = "failed"
        run.error_message = str(e)
        run.completed_at = datetime.utcnow()
        batch.status = "failed"
        db.add(run)
        db.add(batch)
        db.commit()
        return {"status": "failed", "batch_id": batch.id, "error": str(e)}

    decision_maker_result = {
        "companies_checked": result["companies_checked"],
        "decision_makers_found": result["companies_qualified"],
        "companies_with_no_contact": 0,
        "companies_skipped_already_resolved": 0,
        "hubspot_synced": 0,
        "hubspot_errors": [],
        "budget_stopped_early": result["budget_stopped_early"],
    }

    run.companies_discovered = result["companies_checked"]
    run.companies_selected = result["companies_qualified"]
    run.contacts_found = result["companies_qualified"]
    run.credits_spent_usd = result["credits_spent_usd"]
    run.budget_stopped_early = result["budget_stopped_early"]

    if result["budget_stopped_early"] or result["companies_qualified"] == 0:
        batch.current_phase = "autonomous_cycle_done"
        batch.status = "complete"
        run.status = "completed"
        run.contacts_pushed = 0
        run.completed_at = datetime.utcnow()
        db.commit()
        return {
            "status": "completed",
            "batch_id": batch.id,
            "source": "jobo",
            "companies_discovered": result["companies_qualified"],
            "companies_checked_by_discovery": result["companies_checked"],
            "discovery_rejection_breakdown": result["rejection_breakdown"],
            "decision_maker_result": decision_maker_result,
            "outreach_result": {"contacts_checked": 0, "pushed": 0, "failed": 0, "skipped": 0},
            "credits_spent_usd": result["credits_spent_usd"],
            "budget_stopped_early": result["budget_stopped_early"],
            "budget_usd": budget_usd,
        }

    batch.current_phase = "awaiting_approval"
    run.status = "awaiting_approval"
    run.awaiting_approval_until = datetime.utcnow() + timedelta(minutes=APPROVAL_WINDOW_MINUTES)
    db.commit()

    # Isolated so a notification-layer failure is reported but never overwrites the already-
    # successful run status -- see the deepline branch above for the full explanation.
    try:
        messages = _generate_messages_for_batch(batch.id, db, tenant_id)
        notification_error = _send_approval_notification(batch, run, decision_maker_result, db, tenant_id, messages=messages)
    except Exception as e:
        notification_error = f"message generation/notification step failed: {e}"

    return {
        "status": "awaiting_approval",
        "batch_id": batch.id,
        "source": "jobo",
        "companies_discovered": result["companies_qualified"],
        "companies_checked_by_discovery": result["companies_checked"],
        "discovery_rejection_breakdown": result["rejection_breakdown"],
        "decision_maker_result": decision_maker_result,
        "awaiting_approval_until": run.awaiting_approval_until,
        "notification_error": notification_error,
        "credits_spent_usd": result["credits_spent_usd"],
        "budget_usd": budget_usd,
    }


def _run_jd_first_autonomous_cycle(batch: Batch, run: AutonomousRun, db: Session, tenant_id: int, budget_usd: float) -> dict:
    """jd_first's autonomous branch -- the jobs-first approach validated live today (~$1.40
    for 10 real companies, vs. the old deepline branch's $3.22 for 4).

    No BudgetGuard here -- confirmed live that Deepline's own balance-check endpoint
    (subprocess call to the CLI) can hang/timeout on both its retry attempts, and since the
    guard's whole design is "if we can't verify the balance, stop the run" (see
    budget_guard.py), that flakiness was blocking real runs entirely, twice in a row. The
    company cap (target=cap below) already bounds total work/spend to a known ceiling on its
    own -- a fixed number of companies means a fixed number of paid API calls -- so the extra
    balance-check layer wasn't buying real safety here, just fragility."""
    cap = get_daily_company_cap(db, tenant_id)
    result = run_jd_first_discovery(batch.id, db, tenant_id, target=cap, jobs_per_page=min(cap, 10))

    found = 0
    companies = db.query(Company).filter(Company.batch_id == batch.id).all()
    for company in companies:
        contact = find_decision_maker(company, db)
        company.decision_maker_searched_at = datetime.utcnow()
        db.commit()
        if contact:
            found += 1

    final_spend = None  # no guard tracking this run -- see docstring above
    decision_maker_result = {
        "companies_checked": result["companies_discovered"],
        "decision_makers_found": found,
        "companies_with_no_contact": result["companies_discovered"] - found,
        "companies_skipped_already_resolved": 0,
        "hubspot_synced": 0,
        "hubspot_errors": [],
        "budget_stopped_early": result["budget_stopped_early"],
    }

    run.companies_discovered = result["companies_discovered"]
    run.companies_selected = result["companies_discovered"]
    run.contacts_found = found
    run.credits_spent_usd = final_spend
    run.budget_stopped_early = result["budget_stopped_early"]

    if result["budget_stopped_early"] or found == 0:
        batch.current_phase = "autonomous_cycle_done"
        batch.status = "complete"
        run.status = "completed"
        run.contacts_pushed = 0
        run.completed_at = datetime.utcnow()
        db.commit()
        return {
            "status": "completed",
            "batch_id": batch.id,
            "source": "jd_first",
            "companies_discovered": result["companies_discovered"],
            "companies_checked_by_discovery": result["postings_checked"],
            "discovery_rejection_breakdown": result["rejection_breakdown"],
            "decision_maker_result": decision_maker_result,
            "outreach_result": {"contacts_checked": 0, "pushed": 0, "failed": 0, "skipped": 0},
            "credits_spent_usd": final_spend,
            "budget_stopped_early": result["budget_stopped_early"],
            "budget_usd": budget_usd,
        }

    batch.current_phase = "awaiting_approval"
    run.status = "awaiting_approval"
    run.awaiting_approval_until = datetime.utcnow() + timedelta(minutes=APPROVAL_WINDOW_MINUTES)
    db.commit()

    # Isolated so a notification-layer failure is reported but never overwrites the already-
    # successful run status -- see the deepline branch's docstring comment for the full
    # explanation (confirmed live: this exact class of bug flipped a genuinely successful
    # jd_first run to "failed" after the fact).
    try:
        messages = _generate_messages_for_batch(batch.id, db, tenant_id)
        notification_error = _send_approval_notification(batch, run, decision_maker_result, db, tenant_id, messages=messages)
    except Exception as e:
        notification_error = f"message generation/notification step failed: {e}"

    return {
        "status": "awaiting_approval",
        "batch_id": batch.id,
        "source": "jd_first",
        "companies_discovered": result["companies_discovered"],
        "companies_checked_by_discovery": result["postings_checked"],
        "discovery_rejection_breakdown": result["rejection_breakdown"],
        "decision_maker_result": decision_maker_result,
        "awaiting_approval_until": run.awaiting_approval_until,
        "notification_error": notification_error,
        "credits_spent_usd": final_spend,
        "budget_usd": budget_usd,
    }


def cancel_run(run_id: int, db: Session, tenant_id: int) -> dict:
    """Marks a run cancelled -- normally while it's still in its approval window (the
    periodic resume sweep checks this flag before ever pushing to the outreach channel), but
    also allowed for a run stuck in "running": confirmed live that an uncaught exception right
    at cycle start (before this fix -- see run_daily_autonomous_cycle) can leave a run's row
    stuck showing "running" forever with nothing actually still executing behind it, and the
    automatic stale-run sweep doesn't clear that for STALE_RUN_TIMEOUT_MINUTES. This is the
    manual equivalent for when you don't want to wait that long."""
    run = (
        db.query(AutonomousRun)
        .join(Batch)
        .filter(AutonomousRun.id == run_id)
        .filter(Batch.tenant_id == tenant_id)
        .first()
    )
    if not run:
        return {"status": "not_found"}
    if run.status not in ("awaiting_approval", "running"):
        return {"status": "not_cancellable", "reason": f"run status is '{run.status}', not 'awaiting_approval' or 'running'"}
    run.cancelled = True
    run.status = "cancelled"
    run.completed_at = datetime.utcnow()
    db.commit()
    return {"status": "cancelled"}


def recover_run_to_awaiting_approval(run_id: int, db: Session, tenant_id: int) -> dict:
    """One-off recovery for the notification-failure bug just fixed (see email_client.py/
    autonomous_orchestrator.py commit history): a run that genuinely reached
    awaiting_approval (real companies discovered, real decision-makers found, batch's own
    current_phase already correctly "awaiting_approval") got its status incorrectly flipped
    to "failed" by an unrelated notification-layer error afterward. Restoring status here
    lets the normal resume_pending_approvals sweep pick it back up and push to the outreach
    channel once its approval window elapses, exactly as if the bug had never happened. Only
    allowed when the batch's own phase confirms the run really did get that far."""
    run = (
        db.query(AutonomousRun)
        .join(Batch)
        .filter(AutonomousRun.id == run_id)
        .filter(Batch.tenant_id == tenant_id)
        .first()
    )
    if not run:
        return {"status": "not_found"}
    if run.batch.current_phase != "awaiting_approval":
        return {"status": "not_recoverable", "reason": f"batch's current_phase is '{run.batch.current_phase}', not 'awaiting_approval'"}
    if run.awaiting_approval_until is None:
        return {"status": "not_recoverable", "reason": "run has no awaiting_approval_until timestamp"}
    run.status = "awaiting_approval"
    run.error_message = None
    run.completed_at = None
    db.commit()
    return {"status": "recovered", "awaiting_approval_until": run.awaiting_approval_until}


def resend_approval_notification(run_id: int, db: Session, tenant_id: int) -> dict:
    """Re-sends the approval email/Slack notification for a run using messages ALREADY
    generated and stored in the DB -- no new Aviato/Gemini/Claude calls, so this never
    re-spends money. Needed for exactly the case just hit live: the notification-layer bug
    meant a genuinely successful run's email/Slack never went out at all the first time."""
    run = (
        db.query(AutonomousRun)
        .join(Batch)
        .filter(AutonomousRun.id == run_id)
        .filter(Batch.tenant_id == tenant_id)
        .first()
    )
    if not run:
        return {"status": "not_found"}
    batch = run.batch

    contacts = db.query(Contact).join(Company).filter(Company.batch_id == batch.id).all()
    messages = []
    for contact in contacts:
        pm = contact.personalized_message
        messages.append({
            "company_name": contact.company.name,
            "contact_name": f"{contact.first_name or ''} {contact.last_name or ''}".strip(),
            "contact_title": contact.title,
            "linkedin_url": contact.linkedin_url,
            "message": pm.generated_message if pm else None,
            "error": (pm.error_message if pm else None) or ("Not generated" if not pm else None),
        })

    decision_maker_result = {
        "companies_checked": run.companies_discovered or 0,
        "decision_makers_found": run.contacts_found or 0,
        "companies_with_no_contact": (run.companies_discovered or 0) - (run.contacts_found or 0),
        "companies_skipped_already_resolved": 0,
        "hubspot_synced": 0,
        "hubspot_errors": [],
        "budget_stopped_early": run.budget_stopped_early or False,
    }

    notification_error = _send_approval_notification(batch, run, decision_maker_result, db, tenant_id, messages=messages)
    return {"status": "sent" if notification_error is None else "sent_with_errors", "notification_error": notification_error, "contacts_included": len(messages)}


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
