"""V2-owned contact discovery -- Phase 3+4 Part E.

Calls app.phases.decision_maker.find_decision_makers() (the proven capability both V1's daily
cycle and V2 call directly -- see that module's own docstring for the 2026-08-20 multi-contact
refactor) for a real, qualified Opportunity's company. No V1 scheduler/cron dependency, no
parallel provider/search system, no duplicated credential logic -- the exact "Good" reuse
pattern already established by V2 discovery (Phase 1, app/gtm_os/orchestration/discovery.py):
V2 stage -> shared/proven capability -> result returns directly into V2 -> V2 continues.

Runs ONLY for Opportunities that genuinely need contacts -- never for every Company. See
is_contact_discovery_eligible() for the exact real, non-invented conditions."""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.budget_guard import BudgetExceededError, BudgetGuard
from app.db.models import Batch, Company, Contact
from app.deepline_client import DeeplineError, get_credit_balance_usd
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run, get_control_config
from app.gtm_os.strategy.strategy import GtmStrategy
from app.phases.decision_maker import CANDIDATES_PER_SEARCH, MAX_CONTACTS_PER_COMPANY, find_decision_makers

logger = logging.getLogger(__name__)

# Real, documented paid-fallback cost model (decision_maker.py's own comments -- not invented
# here): search_contact bills $0.056/result, up to CANDIDATES_PER_SEARCH results in one billed
# call, and find_decision_makers tries at most PAID_FALLBACK_MAX_TIERS such calls per company
# (CEO/Founder tier, then sales-leader tier, only if the quota still isn't filled).
PAID_FALLBACK_COST_PER_RESULT_USD = 0.056
PAID_FALLBACK_MAX_TIERS = 2


def _estimate_max_paid_fallback_cost_usd() -> float:
    return PAID_FALLBACK_MAX_TIERS * CANDIDATES_PER_SEARCH * PAID_FALLBACK_COST_PER_RESULT_USD


def _check_paid_fallback_budget(contact_budget_usd: float) -> tuple[bool, str | None]:
    """Real PRE-FLIGHT check, run BEFORE find_decision_makers() is ever given
    allow_paid_fallback=True -- unlike BudgetGuard's own guard.check() (only called AFTER a paid
    call already happened; a post-hoc spend detector, not a pre-emptive block, and the real gap
    this fix closes). Returns (allowed, reason). Never raises -- an unavailable OR negative real
    balance fails CLOSED (blocked), same fail-safe discipline as every other budget guard in this
    codebase. Does not replace BudgetGuard -- that stays as a second, defense-in-depth check on
    the ACTUAL post-call spend, unchanged."""
    estimated_cost = _estimate_max_paid_fallback_cost_usd()
    if estimated_cost > contact_budget_usd:
        return False, f"estimated max paid-fallback cost ${estimated_cost:.3f} exceeds configured limits.contact_discovery_daily_budget_usd ${contact_budget_usd:.2f}"

    try:
        balance = get_credit_balance_usd()
    except DeeplineError as e:
        return False, f"could not verify real Deepline balance before paid fallback: {e}"

    if balance < estimated_cost:
        return False, f"real Deepline balance ${balance:.4f} is insufficient for estimated max paid-fallback cost ${estimated_cost:.3f}"

    return True, None


def update_contact_email(db: Session, tenant_id: int, contact_id: int, email: str) -> Contact:
    """2026-08-25, explicit instruction: a human reviewing a message draft can add/correct a
    contact's email directly (e.g. the contact has none on file, or the pattern-guessed one is
    wrong) rather than being blocked from an email draft entirely. Real tenant-scoping check via
    the same Company -> Batch -> tenant_id chain every other Company/Contact query in this
    codebase already uses (Contact has no direct tenant_id column). Sets email_source="manual" --
    a human-provided email is real, verified-by-a-person data, a strictly higher confidence tier
    than "pattern_guess" (see Contact.email_source's own column comment), never conflated with it."""
    contact = db.get(Contact, contact_id)
    if contact is None:
        raise ValueError(f"no Contact {contact_id}")

    owns_contact = (
        db.query(Contact.id)
        .join(Company, Contact.company_id == Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Contact.id == contact_id, Batch.tenant_id == tenant_id)
        .first()
        is not None
    )
    if not owns_contact:
        raise ValueError(f"Contact {contact_id} does not belong to tenant {tenant_id}")

    contact.email = email
    contact.email_source = "manual"
    db.commit()
    return contact


def get_eligible_contacts(db: Session, company_id: int) -> list[Contact]:
    """Real Contact rows for this company that are NOT suppressed -- Contact.excluded_from_push,
    the exact same field run_campaign_execution already respects, reused as-is (no new field).
    .isnot(True) (not .is_(False)) deliberately tolerates NULL -- rows created before this
    column existed (added via an ALTER TABLE ADD COLUMN) have NULL here, not False, and NULL
    must mean "not suppressed," never silently "ineligible.\""""
    return (
        db.query(Contact)
        .filter(Contact.company_id == company_id, Contact.excluded_from_push.isnot(True))
        .all()
    )


def count_contacts_created_today(db: Session, tenant_id: int, now: datetime | None = None) -> int:
    """Backing signal for control.py's limits.max_contacts_per_day. Deliberately a TENANT-WIDE
    count of every Contact created today (V1's own decision-maker phase included), not just
    V2-sourced ones -- no column anywhere distinguishes which pipeline created a given Contact
    row, and a shared daily cap is the more conservative, honest reading of "max contacts per
    day" regardless: it bounds real total contact-creation volume, not just this one stage's
    slice of it. Flagged as a real scoping decision in the Phase 3/4 report, not silently
    assumed."""
    now = now or datetime.utcnow()
    start_of_day = datetime(now.year, now.month, now.day)
    return (
        db.query(Contact.id)
        .join(Company, Contact.company_id == Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id, Contact.created_at >= start_of_day)
        .count()
    )


def is_contact_discovery_eligible(db: Session, tenant_id: int, opportunity: Opportunity) -> tuple[bool, str]:
    """Returns (eligible, reason). Real conditions only:
    - the Opportunity has a real company_id (same hard requirement Opportunity creation itself
      already enforces -- see opportunity.py's own Part E boundary).
    - a real GtmStrategy already exists for this Opportunity -- the real signal that the
      strategy pipeline has actually engaged with it (Part E's "strategy/contact requirement
      exists"), not just that an Opportunity row exists.
    - fewer than MAX_CONTACTS_PER_COMPANY eligible (non-suppressed) contacts currently exist
      for its company."""
    if opportunity.company_id is None:
        return False, "opportunity has no company_id"

    has_strategy = (
        db.query(GtmStrategy.id)
        .filter(GtmStrategy.tenant_id == tenant_id, GtmStrategy.opportunity_id == opportunity.id)
        .first()
        is not None
    )
    if not has_strategy:
        return False, "no GtmStrategy exists yet for this opportunity"

    eligible_contacts = get_eligible_contacts(db, opportunity.company_id)
    if len(eligible_contacts) >= MAX_CONTACTS_PER_COMPANY:
        return False, f"already {len(eligible_contacts)}/{MAX_CONTACTS_PER_COMPANY} eligible contacts"

    all_contacts_count = db.query(Contact.id).filter(Contact.company_id == opportunity.company_id).count()
    if all_contacts_count > 0 and not eligible_contacts:
        return False, "all known contacts for this company are suppressed"

    return True, f"{len(eligible_contacts)}/{MAX_CONTACTS_PER_COMPANY} eligible contacts so far"


def discover_contacts_for_opportunity(db: Session, tenant_id: int, opportunity: Opportunity) -> dict:
    """The Phase 3/4 entrypoint for one Opportunity. Never raises EXCEPT BudgetExceededError,
    which the caller (run_v2_contact_discovery_sweep) is expected to catch to stop the loop
    early -- same contract as app.phases.decision_maker.run_decision_maker_id's own
    budget_guard usage."""
    try:
        check_can_run(db, tenant_id)
    except ControlPlaneHalted as e:
        return {"status": "skipped", "reason": str(e)}

    eligible, reason = is_contact_discovery_eligible(db, tenant_id, opportunity)
    if not eligible:
        return {"status": "skipped", "reason": reason}

    config = get_control_config(db, tenant_id)
    contact_budget_usd = config["limits"]["contact_discovery_daily_budget_usd"]
    if not contact_budget_usd:
        return {"status": "skipped", "reason": "limits.contact_discovery_daily_budget_usd not configured"}

    max_contacts_per_day = config["limits"]["max_contacts_per_day"]
    if max_contacts_per_day is not None:
        created_today = count_contacts_created_today(db, tenant_id)
        if created_today >= max_contacts_per_day:
            return {"status": "skipped", "reason": f"limits.max_contacts_per_day reached ({created_today}/{max_contacts_per_day})"}

    company = db.get(Company, opportunity.company_id)
    if company is None:
        return {"status": "failed", "opportunity_id": opportunity.id, "error": f"company_id {opportunity.company_id} does not resolve to a real Company row"}

    existing_contacts = db.query(Contact).filter(Contact.company_id == company.id).all()

    # Real, PRE-FLIGHT budget check -- runs BEFORE find_decision_makers() is ever allowed to
    # attempt the paid Deepline fallback, closing the real gap where the free Jobo path always
    # ran fine, but a paid attempt could fire before any budget/balance check happened at all
    # (the old BudgetGuard.check() below only ever caught it AFTER the fact). The free path
    # itself is never affected by this -- allow_paid_fallback=False still lets find_decision_makers
    # run its free tier exactly as before, at $0 cost.
    paid_allowed, paid_blocked_reason = _check_paid_fallback_budget(contact_budget_usd)

    # Real bug fix (2026-08-25, confirmed live): BudgetGuard's constructor calls
    # get_credit_balance_usd() itself, unguarded -- when paid_allowed is already False (e.g. a
    # real Deepline outage/CLI issue, confirmed live), constructing this guard at all was an
    # unnecessary second balance call that could crash the WHOLE sweep with an unhandled
    # DeeplineError, even though the guard is only ever actually used below when used_paid is
    # True -- which can never happen when paid_allowed is False (find_decision_makers only
    # attempts the paid tier when allow_paid_fallback=True). Only constructed when actually
    # needed now, same "one opportunity's failure must never crash the sweep" discipline this
    # function already applies everywhere else.
    guard = None
    if paid_allowed:
        try:
            guard = BudgetGuard(contact_budget_usd)
        except Exception as e:  # noqa: BLE001 -- a provider/balance-check failure must not crash the sweep
            # Real bug fix (2026-08-26, confirmed live): a raw DB-level exception caught here
            # (as opposed to a provider/network error) leaves this SHARED session's transaction
            # "invalid" until rolled back -- confirmed live, the very next opportunity's
            # `db.get(Opportunity, ...)` in run_v2_contact_discovery_sweep's loop crashed the
            # WHOLE sweep with sqlalchemy.exc.PendingRollbackError, because nothing here ever
            # rolled the poisoned transaction back before returning "failed" and moving on.
            # rollback() is always safe to call even when the real cause was a non-DB error
            # (a no-op on a still-valid transaction), so this isn't gated on the exception type.
            db.rollback()
            return {"status": "failed", "opportunity_id": opportunity.id, "error": f"could not initialize budget guard: {e}"}

    try:
        new_contacts, used_paid = find_decision_makers(
            company, db, tenant_id, allow_paid_fallback=paid_allowed, max_contacts=MAX_CONTACTS_PER_COMPANY,
            existing_contacts=existing_contacts,
        )
    except BudgetExceededError:
        raise
    except Exception as e:  # noqa: BLE001 -- a provider failure must not crash the sweep
        # Same real fix as above -- this is the exact except block whose swallowed DB-level
        # exception caused the live PendingRollbackError crash.
        db.rollback()
        return {"status": "failed", "opportunity_id": opportunity.id, "error": str(e)}

    if used_paid and guard is not None:
        guard.check()  # still a second, defense-in-depth check on the ACTUAL post-call spend

    result = {
        "status": "succeeded",
        "opportunity_id": opportunity.id,
        "company_id": company.id,
        "new_contacts_found": len(new_contacts),
        "new_contact_ids": [c.id for c in new_contacts],
        "used_paid_fallback": used_paid,
    }
    if not paid_allowed:
        result["paid_fallback_blocked_reason"] = paid_blocked_reason
    return result


def run_v2_contact_discovery_sweep(db: Session, tenant_id: int, limit: int = 50) -> dict:
    """Hourly-sweep-integrated entrypoint -- iterates real, qualifying Opportunities (see
    is_contact_discovery_eligible) and tops up their contacts. Never raises: per-opportunity
    failures are isolated (same discipline as every other sweep stage); a real
    BudgetExceededError stops the loop early (same as run_decision_maker_id) rather than
    aborting the whole hourly sweep."""
    try:
        check_can_run(db, tenant_id)
    except ControlPlaneHalted as e:
        return {"status": "skipped", "reason": str(e)}

    counts = {
        "status": "succeeded",
        "evaluated": 0,
        "eligible": 0,
        "contacts_found": 0,
        "skipped": 0,
        "failed": 0,
        "budget_stopped_early": False,
    }

    opportunity_ids = [
        row[0]
        for row in db.query(Opportunity.id)
        .filter(Opportunity.tenant_id == tenant_id)
        .order_by(Opportunity.id)
        .limit(limit)
        .all()
    ]

    for opp_id in opportunity_ids:
        # Real bug fix (2026-08-26, confirmed live) -- belt-and-suspenders alongside the
        # rollback() fixes inside discover_contacts_for_opportunity itself: db.get() is the exact
        # call that crashed the whole sweep live with PendingRollbackError, because a PRIOR
        # iteration's swallowed DB-level exception had left this shared session's transaction
        # invalid. A stray, still-invalid transaction from anywhere else this session touched
        # would hit the identical failure here; rolling back defensively before every iteration's
        # own DB.get() costs nothing on a healthy session (a no-op) and keeps this loop's own
        # "per-opportunity failures are isolated" promise true even if some other bug elsewhere
        # leaves the session dirty.
        db.rollback()
        opportunity = db.get(Opportunity, opp_id)
        if opportunity is None or opportunity.tenant_id != tenant_id:
            continue
        counts["evaluated"] += 1
        try:
            result = discover_contacts_for_opportunity(db, tenant_id, opportunity)
        except BudgetExceededError:
            counts["budget_stopped_early"] = True
            break
        except Exception as e:  # noqa: BLE001 -- see module docstring: one opportunity's failure must never crash the sweep
            db.rollback()
            counts["failed"] += 1
            logger.error("run_v2_contact_discovery_sweep: opportunity %s failed unexpectedly -- %s", opp_id, e)
            continue

        if result["status"] == "succeeded":
            counts["eligible"] += 1
            counts["contacts_found"] += result["new_contacts_found"]
        elif result["status"] == "skipped":
            counts["skipped"] += 1
        else:
            counts["failed"] += 1

    return counts
