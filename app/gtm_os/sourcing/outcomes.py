"""The closed set of ways a provider call can end, and what each one means for the next move.

WHY A CLOSED SET. Today a failure is an exception string that gets logged, and the run either
dies or carries on blind. That is not enough to route around a bad provider, because the right
response differs completely by cause:

    a provider that is DOWN            -> switch, and do not retry (retrying a dead endpoint is
                                          how a run burns its wall-clock budget on nothing)
    a filter that matched NOTHING      -> depends entirely on whether the filter was validated:
                                          a validated empty is a real market gap; an unvalidated
                                          empty is our own bad value and must not cost more money
    results that are WRONG             -> the most dangerous, because it looks like success. This
                                          is the 21-hospitals case: 200 OK, rows returned, every
                                          one of them outside the ICP
    the budget ran out                 -> stop cleanly, keep everything already paid for

So the outcome -- not the exception text -- drives the decision, and every outcome has exactly one
documented policy. `should_switch_provider` and `should_retry` are what phase 6's planner reads.
"""
from __future__ import annotations

from dataclasses import dataclass, field

OK = "ok"
EMPTY_VALIDATED = "empty_validated"
EMPTY_SUSPECT = "empty_suspect"
QUALITY_FAIL = "quality_fail"
SCHEMA_ERROR = "schema_error"
AUTH_ERROR = "auth_error"
RATE_LIMITED = "rate_limited"
UNAVAILABLE = "unavailable"
BUDGET_BLOCKED = "budget_blocked"


@dataclass(frozen=True)
class Policy:
    should_retry: bool
    should_switch_provider: bool
    counts_against_health: bool
    explanation: str


POLICIES: dict[str, Policy] = {
    OK: Policy(False, False, False, "Results returned and a sample passed the ICP check."),
    EMPTY_VALIDATED: Policy(
        False, True, False,
        "Zero results, but the filter set was validated for free first -- a genuine gap in this "
        "provider's data, not our mistake. Cool down this filter set here and try a provider with "
        "different coverage."),
    EMPTY_SUSPECT: Policy(
        False, False, False,
        "Zero results from a filter set we never validated. Our value is probably wrong, so going "
        "to another provider would just repeat the same mistake at a new price. Re-resolve first."),
    QUALITY_FAIL: Policy(
        False, True, True,
        "Rows came back but a sample did not match the ICP. Looks like success and is not; this is "
        "what sent 21 hospitals and law firms into a Professional Services ICP."),
    SCHEMA_ERROR: Policy(
        False, True, True,
        "The provider rejected our request shape. Our registry entry is wrong or the provider "
        "changed -- mark the capability stale and re-verify before using it again."),
    AUTH_ERROR: Policy(
        False, True, True,
        "Credential missing, invalid or expired. Never retry in a loop; surface it to a human."),
    RATE_LIMITED: Policy(
        True, True, False,
        "Throttled. One backoff inside budget is reasonable, otherwise switch."),
    UNAVAILABLE: Policy(
        False, True, True,
        "Provider down or timed out. Do not retry it -- take the next candidate."),
    BUDGET_BLOCKED: Policy(
        False, False, False,
        "Our own cap, not the provider's problem. Stop cleanly and keep everything already paid "
        "for; resume on the next run."),
}


@dataclass
class CallOutcome:
    outcome: str
    detail: str | None = None
    rows: int = 0
    sample_match_rate: float | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def policy(self) -> Policy:
        return POLICIES[self.outcome]

    @property
    def ok(self) -> bool:
        return self.outcome == OK


def classify_exception(exc: Exception) -> CallOutcome:
    """Map a provider exception onto the closed set.

    Deliberately conservative: anything unrecognised becomes UNAVAILABLE, whose policy is "switch,
    do not retry". An unknown failure treated as retryable is how a run spends its whole window
    hammering something broken.
    """
    from app.deepline_client import DeeplineError, DeeplineSpendBlocked

    name = type(exc).__name__
    text = str(exc).lower()

    if isinstance(exc, DeeplineSpendBlocked):
        return CallOutcome(BUDGET_BLOCKED, detail=str(exc))
    if any(k in text for k in ("401", "403", "unauthorized", "forbidden", "invalid api key", "auth")):
        return CallOutcome(AUTH_ERROR, detail=str(exc))
    if any(k in text for k in ("429", "rate limit", "too many requests")):
        return CallOutcome(RATE_LIMITED, detail=str(exc))
    if any(k in text for k in ("422", "400", "validation", "schema", "unknown field", "bad request")):
        return CallOutcome(SCHEMA_ERROR, detail=str(exc))
    if any(k in text for k in ("timeout", "timed out", "503", "502", "connection", "unavailable")):
        return CallOutcome(UNAVAILABLE, detail=str(exc))
    if isinstance(exc, DeeplineError):
        return CallOutcome(UNAVAILABLE, detail=f"{name}: {exc}")
    return CallOutcome(UNAVAILABLE, detail=f"{name}: {exc}")
