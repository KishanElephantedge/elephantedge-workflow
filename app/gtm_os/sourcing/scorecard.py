"""Turn the raw route_attempts log into something the planner, a human, and a partner can read.

Three things, in increasing order of how much they're allowed to influence a live run:

    provider_scorecard   per-provider health over a window -- informational
    shape_scorecard      per (provider, ICP SHAPE) health -- what drift detection reads
    detect_drift         a shape that used to work and stopped -- a flag, never an auto-action

WHY A SEPARATE SHAPE SCORECARD, NOT JUST A PROVIDER ONE. A provider can be perfectly healthy on
average while one specific filter shape has quietly broken -- a taxonomy value deprecated, a bound
the provider tightened, an endpoint change. That is exactly what happened to "Professional
Services" on Icypeas: the provider itself was fine; that one shape was not. Averaging across every
ICP a provider has ever served would hide precisely the failure this module exists to catch.

THE RULE FROM THE DESIGN DOC, restated because it is the one most tempting to break once history
exists to lean on: "A prior may only re-order candidates; it may never substitute for this run's
free pre-flight." Nothing here blocks a run, changes a filter, or skips a check. Drift detection
produces a flag for a human or the planner's ranking to weigh -- never a decision made for them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.gtm_os.sourcing import outcomes as O
from app.gtm_os.sourcing.models import RouteAttempt

# The comparison window for drift: how far back "used to work" looks, separate from and prior to
# the recent window that decides "currently broken".
BASELINE_WINDOW = timedelta(days=30)
RECENT_WINDOW = timedelta(days=3)
# Below this many attempts in a window, a rate is noise, not a verdict -- matches the same
# minimum-sample discipline as sample verification (DEFAULT_MIN_SAMPLE in verification.py).
MIN_ATTEMPTS = 3


@dataclass
class Scorecard:
    scope: str                  # "provider" or a shape fingerprint
    provider: str
    attempts: int = 0
    successes: int = 0
    total_cost_usd: float = 0.0
    total_rows: int = 0
    outcome_counts: dict | None = None

    @property
    def success_rate(self) -> float | None:
        return (self.successes / self.attempts) if self.attempts else None

    @property
    def cost_per_row(self) -> float | None:
        return (self.total_cost_usd / self.total_rows) if self.total_rows else None

    @property
    def conclusive(self) -> bool:
        return self.attempts >= MIN_ATTEMPTS


def _aggregate(db: Session, provider: str, scope_label: str, since: datetime,
               icp_fingerprint: str | None = None, before: datetime | None = None) -> Scorecard:
    query = db.query(RouteAttempt).filter(RouteAttempt.provider == provider,
                                          RouteAttempt.attempted_at >= since)
    if before is not None:
        query = query.filter(RouteAttempt.attempted_at < before)
    if icp_fingerprint is not None:
        query = query.filter(RouteAttempt.icp_fingerprint == icp_fingerprint)
    rows = query.all()

    card = Scorecard(scope=scope_label, provider=provider, attempts=len(rows), outcome_counts={})
    for row in rows:
        card.outcome_counts[row.outcome] = card.outcome_counts.get(row.outcome, 0) + 1
        if row.outcome == O.OK:
            card.successes += 1
        card.total_cost_usd += row.cost_usd or 0.0
        card.total_rows += row.rows or 0
    return card


def provider_scorecard(db: Session, provider: str, window: timedelta = BASELINE_WINDOW,
                       now: datetime | None = None) -> Scorecard:
    since = (now or datetime.utcnow()) - window
    return _aggregate(db, provider, "provider", since)


def shape_scorecard(db: Session, provider: str, icp_fingerprint: str, window: timedelta = BASELINE_WINDOW,
                    now: datetime | None = None) -> Scorecard:
    since = (now or datetime.utcnow()) - window
    return _aggregate(db, provider, icp_fingerprint, since, icp_fingerprint=icp_fingerprint)


@dataclass
class DriftFlag:
    provider: str
    icp_fingerprint: str
    baseline: Scorecard
    recent: Scorecard
    reason: str


def detect_drift(db: Session, provider: str, icp_fingerprint: str, now: datetime | None = None,
                 min_baseline_rate: float = 0.5) -> DriftFlag | None:
    """A shape that used to succeed and has recently stopped.

    Both windows must be conclusive (MIN_ATTEMPTS) before this returns anything -- an inconclusive
    verdict is not a flag, the same discipline as sample verification's `passed()`. A shape with
    too little history either way is silently fine, not silently flagged.
    """
    now = now or datetime.utcnow()
    recent_starts_at = now - RECENT_WINDOW
    # Baseline is the window BEFORE the recent one, not merely "everything before now" -- an
    # open-ended baseline would include the very attempts the recent window is judging, diluting
    # exactly the regression this function exists to catch.
    baseline = _aggregate(db, provider, icp_fingerprint, recent_starts_at - BASELINE_WINDOW,
                          before=recent_starts_at, icp_fingerprint=icp_fingerprint)
    recent = _aggregate(db, provider, icp_fingerprint, recent_starts_at,
                        icp_fingerprint=icp_fingerprint)

    if not baseline.conclusive or not recent.conclusive:
        return None
    if baseline.success_rate is None or recent.success_rate is None:
        return None
    if baseline.success_rate < min_baseline_rate:
        return None          # it never reliably worked -- nothing to call a regression
    if recent.success_rate >= baseline.success_rate * 0.5:
        return None          # a real dip, not a collapse -- avoid flagging ordinary noise

    return DriftFlag(
        provider=provider, icp_fingerprint=icp_fingerprint, baseline=baseline, recent=recent,
        reason=(f"{provider} succeeded {baseline.success_rate:.0%} of {baseline.attempts} attempts "
               f"for this filter shape over the last {BASELINE_WINDOW.days} days, but only "
               f"{recent.success_rate:.0%} of {recent.attempts} in the last {RECENT_WINDOW.days}."),
    )


def known_shapes(db: Session, provider: str, window: timedelta = BASELINE_WINDOW,
                 now: datetime | None = None) -> list[str]:
    """Every ICP shape this provider has actually been asked to serve recently -- what a drift
    sweep iterates over, rather than guessing which shapes exist."""
    since = (now or datetime.utcnow()) - window
    rows = (db.query(RouteAttempt.icp_fingerprint).filter(
        RouteAttempt.provider == provider, RouteAttempt.attempted_at >= since,
        RouteAttempt.icp_fingerprint.isnot(None)).distinct().all())
    return [r[0] for r in rows]


def sweep_drift(db: Session, provider: str, now: datetime | None = None) -> list[DriftFlag]:
    flags = []
    for shape in known_shapes(db, provider, now=now):
        flag = detect_drift(db, provider, shape, now=now)
        if flag is not None:
            flags.append(flag)
    return flags


def explain_run(db: Session, tenant_id: int, since: datetime | None = None, limit: int = 20) -> list[dict]:
    """Partner-visible: why did recent companies come from the tool they came from.

    Plain-language per the outcome policy, not a dump of the raw log -- a partner asking "why
    these companies" should get an answer, not a table of enum values.
    """
    since = since or (datetime.utcnow() - timedelta(days=7))
    rows = (db.query(RouteAttempt)
            .filter(RouteAttempt.tenant_id == tenant_id, RouteAttempt.attempted_at >= since)
            .order_by(RouteAttempt.attempted_at.desc()).limit(limit).all())
    return [
        {
            "when": r.attempted_at.isoformat() if r.attempted_at else None,
            "provider": r.provider,
            "rows_found": r.rows,
            "cost_usd": r.cost_usd,
            "result": O.POLICIES.get(r.outcome, O.POLICIES[O.UNAVAILABLE]).explanation,
        }
        for r in rows
    ]
