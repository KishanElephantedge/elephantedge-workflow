"""Tests for the daily flow loop's stop conditions.

THE BUG THESE PIN. _no_eligible_work_remaining() used to read objectives_PROCESSED, which counts
an objective refused outright (budget gone, source disabled, credentials missing, control plane
halted) exactly the same as one that did real work. So a fully blocked tick reported "work
happened", the loop did not stop, and it started another full iteration -- re-running every
stage and re-paying for anything not blocked -- until the iteration ceiling.

That is the mechanism behind the reported symptom: set a target, never reach it, keep spending.
Confirmed live in run 150, whose single objective came back blocked_by_budget ("today's real
Apify spend $3.0388 + estimated $0.1100 would exceed daily budget $3.00") while the run still
completed 2 iterations and produced nothing.
"""
from app.gtm_os.orchestration.sweep import _budget_exhausted, _no_eligible_work_remaining


def _cycle(processed, blocked, statuses=None):
    results = []
    if statuses is None:
        statuses = ["blocked_by_budget"] * blocked + ["succeeded"] * (processed - blocked)
    for s in statuses:
        results.append({"exec_status": s})
    return {
        "objectives_processed": processed,
        "objectives_blocked": blocked,
        "objectives_advanced": processed - blocked,
        "results": results,
    }


def test_a_fully_blocked_tick_is_not_progress():
    """THE FIX. Every objective refused for budget -> the loop must stop, not iterate again."""
    result = {
        "sources": {},
        "investigation_cycle": _cycle(processed=3, blocked=3),
        "interpretation": {"created": 0},
    }
    assert _no_eligible_work_remaining(result) is True


def test_real_work_still_counts_as_progress():
    """The guard must not over-correct: an objective that actually ran means keep going."""
    result = {
        "sources": {},
        "investigation_cycle": _cycle(processed=3, blocked=2),
        "interpretation": {"created": 0},
    }
    assert _no_eligible_work_remaining(result) is False


def test_new_signals_count_as_progress_even_with_everything_blocked():
    result = {
        "sources": {"linkedin_job": {"status": "succeeded", "signals_created": 4}},
        "investigation_cycle": _cycle(processed=2, blocked=2),
        "interpretation": {"created": 0},
    }
    assert _no_eligible_work_remaining(result) is False


def test_new_interpretations_count_as_progress():
    result = {
        "sources": {},
        "investigation_cycle": _cycle(processed=2, blocked=2),
        "interpretation": {"created": 7},
    }
    assert _no_eligible_work_remaining(result) is False


def test_falls_back_to_processed_for_an_older_cycle_result():
    """objectives_advanced is new. A result produced before it existed must not be read as
    zero-progress, which would stop a healthy loop early."""
    result = {
        "sources": {},
        "investigation_cycle": {"objectives_processed": 2},  # no advanced/blocked keys
        "interpretation": {"created": 0},
    }
    assert _no_eligible_work_remaining(result) is False


def test_signals_from_a_failed_source_are_not_counted():
    """Only succeeded sources contribute -- a failed source reporting a count is not progress."""
    result = {
        "sources": {"linkedin_job": {"status": "failed", "signals_created": 9}},
        "investigation_cycle": _cycle(processed=1, blocked=1),
        "interpretation": {"created": 0},
    }
    assert _no_eligible_work_remaining(result) is True


# --- budget_exhausted: naming the real cause -------------------------------------------------

def test_budget_exhausted_when_every_objective_was_refused_for_budget():
    result = {"sources": {}, "investigation_cycle": _cycle(processed=2, blocked=2)}
    assert _budget_exhausted(result) is True


def test_not_budget_exhausted_when_an_objective_actually_ran():
    result = {
        "sources": {},
        "investigation_cycle": _cycle(processed=2, blocked=1, statuses=["blocked_by_budget", "succeeded"]),
    }
    assert _budget_exhausted(result) is False


def test_a_different_block_reason_is_not_budget_exhaustion():
    """'Source disabled' is a configuration problem, not a spend problem -- raising the cap
    would not help, so it must not be reported as budget exhaustion."""
    result = {
        "sources": {},
        "investigation_cycle": _cycle(processed=1, blocked=1, statuses=["blocked_by_disabled_source"]),
    }
    assert _budget_exhausted(result) is False


def test_a_budget_blocked_sensing_source_also_counts():
    """From the loop's point of view an objective refused for budget and a sensing source
    refused for budget mean the same thing: another iteration cannot buy anything."""
    result = {
        "sources": {"linkedin_job": {"status": "blocked", "reason": "apify_budget_blocked: daily cap reached"}},
        "investigation_cycle": {},
    }
    assert _budget_exhausted(result) is True


def test_an_empty_result_is_not_budget_exhaustion():
    assert _budget_exhausted({"sources": {}, "investigation_cycle": {}}) is False
