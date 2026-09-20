"""Shared, durable LLM call budget -- the one thing that stops a run stalling on quota.

WHY THIS EXISTS. LLM volume in this system is not primarily a cash cost (Gemini's free tier is
$0); it is the mechanism behind the stalls. The free tier allows 500 requests/model/day, and a
single sweep still makes roughly 325-375 calls across 78 call sites in 27 modules (commit
ac9c1e4 removed ~150-200 of an original ~525, from one function). One run can therefore consume
the whole day's allowance; after that every call walks the fallback list collecting 429s, each
burning seconds, until stages blow their timeouts and the run looks frozen.

Two properties the previous in-process approach could not have:

1. IT SURVIVES A REDEPLOY. llm_client._EXHAUSTED is a plain dict, cleared on every restart --
   flagged in that module's own docstring as unfixed. With ~40 redeploys in 3 days, every deploy
   erased the knowledge of which models were dead, so the next run re-attempted known-dead models
   and spent its allowance on rejections. The file records the measurement: "run 124: 368
   quota-rejected requests for 1 useful answer". Patching faster actively made runs slower.

2. IT IS SHARED BETWEEN INSTANCES. Three Render deployments run the same scheduler against ONE
   Gemini project. A per-process counter sees at most its own half of the spend, so both
   instances can believe they are within budget while together they are well past it.

COUNTS ATTEMPTS, NOT SUCCESSES -- deliberately. A 429-rejected request still counts against
Google's daily quota, which is exactly why the retry storm above was self-defeating. A budget
that only counted successful answers would be blind to the failure mode it exists to prevent.
"""
import logging
from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, Integer, String
from sqlalchemy.orm import Session

from app.db.models import Base

logger = logging.getLogger(__name__)

# Half the free tier's 500/model/day, leaving real headroom for the other deployed instance and
# for manual/ad-hoc work. A cap that consumed the whole allowance would leave a run no margin to
# finish, which is the exact failure this is here to prevent.
DEFAULT_DAILY_LLM_CALL_BUDGET = 250
_BUDGET_PARAMETER_KEY = "llm_daily_call_budget"


class LlmBudgetExceeded(Exception):
    """Raised instead of letting a run grind through hundreds of doomed 429s.

    Stopping deliberately is strictly better than the alternative: a stage that keeps calling a
    dead provider does not fail fast, it fails at timeout speed, and the run reads as hung."""


class LlmDailyUsage(Base):
    """One row per (tenant, day, model). Created by ensure_indexes(); see app/db/session.py."""
    __tablename__ = "llm_daily_usage"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    usage_date = Column(Date, nullable=False)
    model = Column(String, nullable=False)
    call_count = Column(Integer, nullable=False, default=0)
    # Set when the provider itself says the per-DAY quota is gone. Distinct from call_count
    # hitting our own cap: theirs is a hard wall until UTC midnight, ours is a policy.
    quota_exhausted_at = Column(DateTime, nullable=True)


def _today() -> date:
    # UTC because that is when Gemini's free daily quota actually resets.
    return datetime.utcnow().date()


def _row(db: Session, tenant_id: int, model: str, create: bool = False) -> LlmDailyUsage | None:
    row = (
        db.query(LlmDailyUsage)
        .filter(
            LlmDailyUsage.tenant_id == tenant_id,
            LlmDailyUsage.usage_date == _today(),
            LlmDailyUsage.model == model,
        )
        .first()
    )
    if row or not create:
        return row
    row = LlmDailyUsage(tenant_id=tenant_id, usage_date=_today(), model=model, call_count=0)
    db.add(row)
    try:
        db.commit()
    except Exception:  # noqa: BLE001 -- another instance created the same row first
        db.rollback()
        row = _row(db, tenant_id, model, create=False)
    return row


def get_daily_llm_call_budget(db: Session, tenant_id: int) -> int:
    from app.db.models import Parameter

    row = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id, Parameter.key == _BUDGET_PARAMETER_KEY)
        .first()
    )
    if row and isinstance(row.value, dict) and "calls" in row.value:
        return int(row.value["calls"])
    return DEFAULT_DAILY_LLM_CALL_BUDGET


def record_call(db: Session, tenant_id: int, model: str) -> None:
    """Count one ATTEMPT. Never raises -- accounting must not break the caller."""
    try:
        row = _row(db, tenant_id, model, create=True)
        if row is None:
            return
        row.call_count = (row.call_count or 0) + 1
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("llm_budget: failed to record a call for model=%s", model)


def mark_exhausted(db: Session, tenant_id: int, model: str) -> None:
    """Remember, durably, that the provider says this model's DAILY quota is gone.

    This is the fact that used to die on every redeploy."""
    try:
        row = _row(db, tenant_id, model, create=True)
        if row is None:
            return
        row.quota_exhausted_at = datetime.utcnow()
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("llm_budget: failed to mark model=%s exhausted", model)


def is_exhausted(db: Session, tenant_id: int, model: str) -> bool:
    """True only for TODAY -- Gemini's daily quota resets at UTC midnight, so yesterday's
    exhaustion must never suppress today's model. The date is part of the row key, so this
    expires naturally rather than needing a cleanup job."""
    try:
        row = _row(db, tenant_id, model)
        return bool(row and row.quota_exhausted_at)
    except Exception:  # noqa: BLE001 -- fail OPEN: a bookkeeping outage must not block real work
        db.rollback()
        logger.exception("llm_budget: exhaustion check failed for model=%s", model)
        return False


def calls_today(db: Session, tenant_id: int) -> int:
    rows = (
        db.query(LlmDailyUsage)
        .filter(LlmDailyUsage.tenant_id == tenant_id, LlmDailyUsage.usage_date == _today())
        .all()
    )
    return sum(r.call_count or 0 for r in rows)


def check_daily_llm_budget(db: Session, tenant_id: int) -> None:
    """Raise LlmBudgetExceeded once today's attempts reach the cap.

    Fails OPEN on a bookkeeping error: if the budget itself cannot be read, blocking every LLM
    call would turn a monitoring outage into a total outage. The provider's own quota is still
    the hard wall behind this."""
    try:
        budget = get_daily_llm_call_budget(db, tenant_id)
        used = calls_today(db, tenant_id)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("llm_budget: could not verify daily budget; allowing the call")
        return

    if used >= budget:
        raise LlmBudgetExceeded(
            f"LLM stopped: {used} calls already made today, which reached the {budget}/day cap "
            f"for tenant {tenant_id}. Gemini's free tier is 500/model/day and is shared across "
            f"every deployed instance."
        )


def usage_summary(db: Session, tenant_id: int) -> dict:
    """What actually got spent today, per model -- so 'why did the run stall' is answerable
    from data instead of from log archaeology across three deployments."""
    rows = (
        db.query(LlmDailyUsage)
        .filter(LlmDailyUsage.tenant_id == tenant_id, LlmDailyUsage.usage_date == _today())
        .all()
    )
    return {
        "date": _today().isoformat(),
        "budget": get_daily_llm_call_budget(db, tenant_id),
        "total_calls": sum(r.call_count or 0 for r in rows),
        "by_model": {
            r.model: {"calls": r.call_count or 0, "quota_exhausted_at": r.quota_exhausted_at}
            for r in rows
        },
    }
