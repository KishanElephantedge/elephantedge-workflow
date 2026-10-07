"""Pick the best tool for THIS partner's ICP, run it, and switch tools when it fails.

This is the piece the whole router exists for. Everything before it describes capability; this
decides and acts.

    rank          score every registered provider against this specific ICP
    execute       call the winner through its own adapter
    classify      map the result onto the closed outcome set (outcomes.py)
    switch        on an outcome whose policy says so, take the next candidate and try again

WHAT MAKES A PROVIDER "BEST" -- in this order, deliberately:

  1. Can it enforce the partner's MUST-HAVE requirements? A provider that cannot filter the thing
     the partner actually cares about is not cheap, it is wrong.
  2. Is it healthy right now? A provider that failed its last few calls is not a candidate,
     however good its capabilities look on paper.
  3. Only then, cost.

Quality before economics. A cheaper provider returning the wrong companies costs more than an
expensive one returning the right ones, because every wrong row is paid for again downstream in
decision-maker resolution, qualification and a human's attention.

HEALTH IS OBSERVED, NOT ASSUMED. It comes from what actually happened on recent runs
(route_attempts), not from a static ranking -- which is the difference between a fixed waterfall
and a router that adapts. But health only ever REORDERS candidates: it can never substitute for
the free pre-flight checks, because a provider's behaviour today is not evidence about today.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.gtm_os.sourcing import atoms as A
from app.gtm_os.sourcing import outcomes as O
from app.gtm_os.sourcing import registry as R
from app.gtm_os.sourcing.models import RouteAttempt

# A provider that failed this many times in a row recently is skipped rather than retried. It is
# still reported, so a run never silently loses a route without saying why.
CIRCUIT_BREAK_FAILURES = 3
CIRCUIT_BREAK_WINDOW = timedelta(hours=1)

# Adapters: provider -> the function that actually calls it. Registering one is the ONLY code a
# new provider needs; the ranking, failover and recording below never change.
#   signature: fn(db, tenant_id, icp, **kwargs) -> dict   (the play's own result shape)
_ADAPTERS: dict[str, Callable] = {}


def register_adapter(provider: str, fn: Callable) -> None:
    _ADAPTERS[provider] = fn


def has_adapter(provider: str) -> bool:
    return provider in _ADAPTERS


@dataclass
class Candidate:
    endpoint: R.ProviderEndpoint
    coverage: R.Coverage
    must_have_gap: list[A.Atom] = field(default_factory=list)
    healthy: bool = True
    recent_failures: int = 0
    executable: bool = False
    score: float = 0.0
    why: str = ""

    @property
    def provider(self) -> str:
        return self.endpoint.provider


def recent_failures(db: Session, provider: str, now: datetime | None = None) -> int:
    """Consecutive recent failures, newest first. Any success resets the count."""
    since = (now or datetime.utcnow()) - CIRCUIT_BREAK_WINDOW
    attempts = (db.query(RouteAttempt)
                .filter(RouteAttempt.provider == provider, RouteAttempt.attempted_at >= since)
                .order_by(RouteAttempt.attempted_at.desc()).limit(10).all())
    failures = 0
    for attempt in attempts:
        policy = O.POLICIES.get(attempt.outcome)
        if policy is not None and policy.counts_against_health:
            failures += 1
        else:
            break
    return failures


def rank(db: Session, icp: dict, now: datetime | None = None) -> list[Candidate]:
    """Score every registered company-search provider against this ICP."""
    icp_atoms = A.decompose_icp(icp)
    candidates: list[Candidate] = []

    for endpoint in R.endpoints_for_job("company_search"):
        coverage = R.coverage_for(endpoint, icp_atoms)
        failures = recent_failures(db, endpoint.provider, now=now)
        candidate = Candidate(
            endpoint=endpoint,
            coverage=coverage,
            must_have_gap=coverage.must_have_gap,
            healthy=failures < CIRCUIT_BREAK_FAILURES,
            recent_failures=failures,
            executable=has_adapter(endpoint.provider),
        )

        must_haves = [a for a in icp_atoms.atoms if a.necessity == A.MUST_HAVE] or [1]
        enforced = len([a for a in coverage.enforced if a.necessity == A.MUST_HAVE])
        candidate.score = enforced / len(must_haves)

        reasons = [f"enforces {enforced}/{len(must_haves)} must-haves"]
        if candidate.must_have_gap:
            reasons.append(f"cannot enforce {', '.join(a.name for a in candidate.must_have_gap)}")
        if not candidate.executable:
            reasons.append("no adapter registered yet")
        if not candidate.healthy:
            reasons.append(f"circuit-broken after {failures} recent failures")
        candidate.why = "; ".join(reasons)
        candidates.append(candidate)

    # Unhealthy and non-executable providers sink to the bottom but are never dropped -- a run
    # that cannot proceed must be able to say exactly which routes it considered and why.
    candidates.sort(key=lambda c: (c.executable and c.healthy, c.score, -c.recent_failures),
                    reverse=True)
    return candidates


def record_attempt(db: Session, tenant_id: int, provider: str, endpoint: str, outcome: str,
                   detail: str | None = None, rows: int = 0, cost_usd: float | None = None) -> None:
    db.add(RouteAttempt(tenant_id=tenant_id, provider=provider, endpoint=endpoint,
                        outcome=outcome, detail=(detail or "")[:500], rows=rows, cost_usd=cost_usd))
    db.commit()


@dataclass
class RoutedRun:
    result: dict | None = None
    provider: str | None = None
    attempts: list[dict] = field(default_factory=list)
    considered: list[dict] = field(default_factory=list)
    stopped: str | None = None


def execute(db: Session, tenant_id: int, icp: dict, max_providers: int = 2, **kwargs) -> RoutedRun:
    """Run the best available tool for this ICP, switching tools when an outcome says to.

    `max_providers` bounds the chain the way Deepline's own maxFallbacks does: trying every
    provider on a bad day turns one failed run into several paid ones.
    """
    run = RoutedRun()
    candidates = rank(db, icp)
    run.considered = [{"provider": c.provider, "score": round(c.score, 2), "healthy": c.healthy,
                       "executable": c.executable, "why": c.why} for c in candidates]

    usable = [c for c in candidates if c.executable and c.healthy]
    if not usable:
        run.stopped = "no usable provider: " + "; ".join(
            f"{c.provider} ({c.why})" for c in candidates) or "no providers registered"
        return run

    for candidate in usable[:max_providers]:
        adapter = _ADAPTERS[candidate.provider]
        try:
            result = adapter(db, tenant_id, icp, **kwargs)
            outcome = _outcome_of(result)
        except Exception as exc:  # noqa: BLE001 -- every failure must become a typed outcome
            call = O.classify_exception(exc)
            outcome, result = call.outcome, {"stopped": call.detail}

        record_attempt(db, tenant_id, candidate.provider, candidate.endpoint.endpoint, outcome,
                       detail=(result or {}).get("stopped"), rows=(result or {}).get("companies", 0),
                       cost_usd=(result or {}).get("spent_usd"))
        run.attempts.append({"provider": candidate.provider, "outcome": outcome,
                             "detail": (result or {}).get("stopped")})

        if outcome == O.OK or not O.POLICIES[outcome].should_switch_provider:
            run.result, run.provider = result, candidate.provider
            return run
        # else: this outcome's policy says switch -- fall through to the next candidate

    run.result = result
    run.provider = candidate.provider
    run.stopped = f"all {len(run.attempts)} attempted providers failed"
    return run


def _outcome_of(result: dict) -> str:
    """Map a play's own result shape onto the closed outcome set."""
    if not result:
        return O.UNAVAILABLE
    if result.get("outcome"):
        return result["outcome"]
    stopped = (result.get("stopped") or "").lower()
    if "budget" in stopped:
        return O.BUDGET_BLOCKED
    if result.get("companies"):
        return O.OK
    if result.get("exhausted") or result.get("free_count_checked") == 0:
        # Zero, but the filter set was validated for free first: a real gap in this provider's
        # data rather than a bad value of ours, so a provider with different coverage is worth
        # trying. See outcomes.py for why that distinction changes the next move.
        return O.EMPTY_VALIDATED
    if stopped:
        return O.EMPTY_SUSPECT
    return O.OK
