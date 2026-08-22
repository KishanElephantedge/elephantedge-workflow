"""Investigation memory -- Autonomous Sensing Phase S1.

Per the approved autonomous-sensing architecture design: the durable business memory is the
STRUCTURED InvestigationObjective (icp_id, target_company_id, claim, evidence_sought, reason),
never generated search-query text -- there is no query text yet, since S1/S2 deliberately do not
generate queries (that's S3+, explicitly out of scope here).

One row per (tenant, icp_id, target_company_id) "investigation thread". A thread transitions
through exactly three states -- kept this narrow on purpose, no extra status vocabulary invented:

    OPEN     -- eligible for investigation right now (never attempted, or cooldown has elapsed)
    COOLDOWN -- attempted at least once, inconclusive, waiting for next_eligible_at before retry
    STOPPED  -- terminal. Either max attempts reached (see record_investigation_attempt) OR the
               gap-identification sweep found the thread no longer needs sensing (evidence became
               sufficient, or the blocker turned out to be downstream eligibility, not evidence) --
               `stopped_reason` always says which.

Idempotency is enforced in application code (get_or_create_objective), not a DB unique
constraint: target_company_id is nullable and multiple independent OPEN threads can legitimately
exist per icp_id (one company-agnostic "any ICP1 company" thread, plus company-specific threads
once companies are matched) -- a single unique index can't express "at most one non-terminal row
per (icp_id, target_company_id)" across that mix cleanly, so the lookup itself does the work,
mirroring the same discipline linkedin_search_config.py's phrase_last_searched_at already uses
for its own cooldown bookkeeping.

Reused, not duplicated: attempts/cooldown timing reuses control.py's own `retry.max_attempts`/
`retry.backoff_minutes` (get_control_config) -- no second retry concept invented here."""

from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Session

from app.db.models import Base
from app.gtm_os.orchestration.control import get_control_config

STATUS_OPEN = "open"
STATUS_COOLDOWN = "cooldown"
STATUS_STOPPED = "stopped"
STATUSES = {STATUS_OPEN, STATUS_COOLDOWN, STATUS_STOPPED}

# Attempt outcomes S3+ will eventually report back via record_investigation_attempt(). Kept
# narrow -- these are the only three real outcomes a sensing attempt can have.
RESULT_FOUND_EVIDENCE = "found_evidence"
RESULT_INCONCLUSIVE = "inconclusive"
RESULT_ERROR = "error"


class InvestigationObjective(Base):
    """The structured, durable unit of autonomous-sensing memory. See module docstring."""
    __tablename__ = "investigation_objectives"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    icp_id = Column(String, nullable=False)  # references icp_config's own "id" field, not a DB FK -- same convention as ICPMatch.icp_id
    target_company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)  # None = company-agnostic ("find any ICP-consistent company"), matches real (icp, company) granularity, never invented

    claim = Column(Text, nullable=False)  # conceptual, e.g. "Find evidence that an ICP1-consistent company is experiencing a sales/GTM scaling problem." -- never a search query
    evidence_sought = Column(String, nullable=False)  # "opening_tier" (declared|implied_gap, per problem_detection.OPENING_TIERS) -- the only real evidence-sufficiency vocabulary that exists today
    reason = Column(Text, nullable=False)  # why this gap was identified, in terms of real observed state (see gap_identification.py)

    status = Column(String, nullable=False, default=STATUS_OPEN)
    attempts = Column(Integer, nullable=False, default=0)
    last_attempted_at = Column(DateTime, nullable=True)
    next_eligible_at = Column(DateTime, nullable=True)
    stopped_reason = Column(Text, nullable=True)
    source_attempted = Column(String, nullable=True)  # last sensing source tried -- stays None until S3 exists; NOT the query text itself

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def get_active_objective(db: Session, tenant_id: int, icp_id: str, target_company_id: int | None) -> InvestigationObjective | None:
    """The idempotency check: is there already a non-terminal thread for this exact
    (tenant, icp, company) key? Used both to avoid duplicate creation and to decide whether an
    existing thread is currently eligible for a new attempt."""
    return (
        db.query(InvestigationObjective)
        .filter(InvestigationObjective.tenant_id == tenant_id)
        .filter(InvestigationObjective.icp_id == icp_id)
        .filter(InvestigationObjective.target_company_id == target_company_id)
        .filter(InvestigationObjective.status != STATUS_STOPPED)
        .order_by(InvestigationObjective.id.desc())
        .first()
    )


def get_or_create_objective(
    db: Session, tenant_id: int, icp_id: str, target_company_id: int | None, claim: str, evidence_sought: str, reason: str,
) -> tuple[InvestigationObjective, bool]:
    """Returns (objective, created). Reuses an existing non-terminal thread for this exact key
    rather than duplicating it -- this IS the "repeated identical objectives" guard. On reuse,
    refreshes claim/evidence_sought/reason to the current assessment (the gap for the same
    company/ICP can shift, e.g. Rule 2 -> Rule 4, without the thread itself restarting) but
    deliberately leaves status/attempts/next_eligible_at untouched -- a still-cooling-down
    thread does not get its cooldown reset just because gap-identification re-ran."""
    existing = get_active_objective(db, tenant_id, icp_id, target_company_id)
    if existing:
        existing.claim = claim
        existing.evidence_sought = evidence_sought
        existing.reason = reason
        db.commit()
        db.refresh(existing)
        return existing, False
    objective = InvestigationObjective(
        tenant_id=tenant_id, icp_id=icp_id, target_company_id=target_company_id,
        claim=claim, evidence_sought=evidence_sought, reason=reason, status=STATUS_OPEN,
    )
    db.add(objective)
    db.commit()
    db.refresh(objective)
    return objective, True


def is_eligible_for_attempt(objective: InvestigationObjective, now: datetime | None = None) -> bool:
    """OPEN threads are always eligible. COOLDOWN threads are eligible only once next_eligible_at
    has passed. STOPPED threads are never eligible -- this is the "duplicate investigation without
    cooldown"/"endless loop" guard S3+ must call before attempting anything."""
    now = now or datetime.utcnow()
    if objective.status == STATUS_STOPPED:
        return False
    if objective.status == STATUS_COOLDOWN:
        return objective.next_eligible_at is not None and now >= objective.next_eligible_at
    return True


def record_investigation_attempt(
    db: Session, tenant_id: int, objective: InvestigationObjective, result: str, source_attempted: str | None = None,
) -> InvestigationObjective:
    """Records one real sensing attempt against this objective (called by S3+, not by this
    phase -- exercised here only via synthetic verification, per this phase's explicit scope).
    Reuses control.py's own retry.max_attempts/backoff_minutes -- no second retry concept."""
    if result not in (RESULT_FOUND_EVIDENCE, RESULT_INCONCLUSIVE, RESULT_ERROR):
        raise ValueError(f"unknown investigation result: {result!r}")

    control_config = get_control_config(db, tenant_id)
    max_attempts = control_config["retry"]["max_attempts"]
    backoff_minutes = control_config["retry"]["backoff_minutes"]

    now = datetime.utcnow()
    objective.attempts += 1
    objective.last_attempted_at = now
    if source_attempted:
        objective.source_attempted = source_attempted

    if result == RESULT_FOUND_EVIDENCE:
        # Evidence found -- this thread's job is done; the existing Problem/Demand/Opportunity
        # pipeline takes over from here, unchanged. Not this module's concern beyond stopping.
        objective.status = STATUS_STOPPED
        objective.stopped_reason = "evidence found; handed off to existing sensing/interpretation/evidence pipeline"
    elif objective.attempts >= max_attempts:
        objective.status = STATUS_STOPPED
        objective.stopped_reason = f"max attempts reached ({objective.attempts}/{max_attempts}), result={result!r}"
    else:
        objective.status = STATUS_COOLDOWN
        objective.next_eligible_at = now + timedelta(minutes=backoff_minutes)

    db.commit()
    db.refresh(objective)
    return objective


def mark_superseded(db: Session, objective: InvestigationObjective, reason: str) -> InvestigationObjective:
    """Marks an objective STOPPED because external state changed and the gap it was tracking no
    longer exists (sufficient evidence appeared some other way, or the blocker is downstream
    eligibility rather than more sensing) -- distinct from record_investigation_attempt's
    outcomes, since no sensing attempt actually happened here."""
    objective.status = STATUS_STOPPED
    objective.stopped_reason = reason
    db.commit()
    db.refresh(objective)
    return objective
