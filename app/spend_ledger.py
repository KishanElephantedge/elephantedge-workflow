"""Local, immediate record of money committed to a provider -- the fix for lagged budget reads.

THE BUG THIS EXISTS FOR. app/apify_budget_guard.py decides whether a call is affordable by
reading Apify's own `dailyServiceUsages` figure. That number is real, but it is an AGGREGATED
BILLING figure and it lags. Inside a single sweep, many calls in a row therefore read the SAME
stale "spent so far today" value, each concludes it has room, and the day's budget is passed
several times over before the provider's number catches up. That is a large part of why spend
was both high and unpredictable: the cap was not ignored, it was measured against a number that
could not move fast enough to enforce it.

The ledger closes that window. Spend is recorded the instant it is committed, so the very next
check in the same sweep sees it.

The provider stays the source of truth for SETTLED spend; the ledger only covers the gap between
committing money and the provider admitting it. Effective spend is therefore max(provider,
ledger) -- deliberately the more conservative of the two. Over-counting briefly costs a little
unused allowance; under-counting is what produced a $10 Apify day on a $5 account.

WHY IT IS WRITTEN INSIDE check_apify_budget(). That function is already called immediately
before every guarded paid Apify call, and already receives the estimate. Making it
reserve-and-check rather than just check means no call site has to remember to record anything
-- which matters, because "someone added a call site and nobody wrapped it" is exactly how the
two currently-unguarded paid paths got that way.
"""
import logging
from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, Float, Integer, String
from sqlalchemy.orm import Session

from app.db.models import Base

logger = logging.getLogger(__name__)

PROVIDER_APIFY = "apify"
PROVIDER_DEEPLINE = "deepline"


class ProviderSpend(Base):
    """One row per committed spend. Created by ensure_indexes(); see app/db/session.py."""
    __tablename__ = "provider_spend_ledger"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    provider = Column(String, nullable=False)
    operation = Column(String, nullable=True)     # which call committed it, for attribution
    entity_key = Column(String, nullable=True)    # company domain / objective id, where known
    estimated_usd = Column(Float, nullable=False, default=0.0)
    actual_usd = Column(Float, nullable=True)     # filled by reconciliation, when the provider says
    spend_date = Column(Date, nullable=False)     # UTC -- the day the cap applies to
    created_at = Column(DateTime, default=datetime.utcnow)


def _today() -> date:
    return datetime.utcnow().date()


def record_spend(db: Session, tenant_id: int, provider: str, estimated_usd: float,
                 operation: str | None = None, entity_key: str | None = None) -> None:
    """Commit a spend to the ledger. Never raises -- accounting must not break a real call, and
    the provider's own cap is still the hard wall behind this."""
    try:
        db.add(ProviderSpend(
            tenant_id=tenant_id, provider=provider, operation=operation, entity_key=entity_key,
            estimated_usd=float(estimated_usd or 0.0), spend_date=_today(),
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("spend_ledger: failed to record %s spend for tenant %s", provider, tenant_id)


def spend_today(db: Session, tenant_id: int, provider: str) -> float:
    """What this system believes it has committed today. Prefers a reconciled actual over the
    original estimate per row, so the ledger converges on truth as the provider confirms."""
    try:
        rows = (
            db.query(ProviderSpend)
            .filter(
                ProviderSpend.tenant_id == tenant_id,
                ProviderSpend.provider == provider,
                ProviderSpend.spend_date == _today(),
            )
            .all()
        )
        return sum(
            (r.actual_usd if r.actual_usd is not None else (r.estimated_usd or 0.0))
            for r in rows
        )
    except Exception:  # noqa: BLE001 -- fail to 0.0 so the provider figure alone still governs
        db.rollback()
        logger.exception("spend_ledger: failed to read today's %s spend", provider)
        return 0.0


def effective_spend_today(db: Session, tenant_id: int, provider: str,
                          provider_reported_usd: float | None) -> float:
    """The number a cap should actually be enforced against.

    max(provider, ledger) on purpose. The provider's figure is authoritative but lags; the
    ledger is immediate but estimated. Taking the higher of the two means a burst of calls
    inside one sweep is counted straight away, while a settled provider figure that exceeds our
    estimates still wins. Erring high costs a little unused allowance; erring low is what
    produced a $10 day on a $5 account."""
    ledger = spend_today(db, tenant_id, provider)
    if provider_reported_usd is None:
        return ledger
    return max(float(provider_reported_usd), ledger)


def spend_today_by_operation(db: Session, tenant_id: int, provider: str) -> dict:
    """Where today's money actually went -- so "cost per company" can be judged against what was
    bought, rather than averaged over unrelated jobs. A real instance of that confusion: 38% of
    one day's spend was a weekly content-marketing job that produced zero companies and zero
    contacts, and it was averaged into cost-per-company anyway."""
    try:
        rows = (
            db.query(ProviderSpend)
            .filter(
                ProviderSpend.tenant_id == tenant_id,
                ProviderSpend.provider == provider,
                ProviderSpend.spend_date == _today(),
            )
            .all()
        )
    except Exception:  # noqa: BLE001
        db.rollback()
        return {}

    out: dict[str, dict] = {}
    for r in rows:
        key = r.operation or "(unattributed)"
        bucket = out.setdefault(key, {"calls": 0, "usd": 0.0})
        bucket["calls"] += 1
        bucket["usd"] += (r.actual_usd if r.actual_usd is not None else (r.estimated_usd or 0.0))
    return out


def reconcile_drift(db: Session, tenant_id: int, provider: str,
                    provider_reported_usd: float | None) -> dict:
    """Compare what we estimated against what the provider actually billed.

    Reported, never silently corrected. Drift is the signal that a per-unit cost constant is
    wrong -- and those constants are local guesses (COST_PER_JOB_USD and friends), while what
    gets STORED as spend today is the estimate, never reconciled against
    GET /apify/usage/by-actor. A persistent gap here is a real defect to go fix, not a number to
    paper over."""
    ledger = spend_today(db, tenant_id, provider)
    if provider_reported_usd is None:
        return {"ledger_usd": ledger, "provider_usd": None, "drift_usd": None, "drift_pct": None}
    drift = float(provider_reported_usd) - ledger
    pct = (drift / provider_reported_usd * 100.0) if provider_reported_usd else None
    return {
        "ledger_usd": round(ledger, 4),
        "provider_usd": round(float(provider_reported_usd), 4),
        "drift_usd": round(drift, 4),
        "drift_pct": round(pct, 1) if pct is not None else None,
    }
