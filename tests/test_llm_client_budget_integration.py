"""End-to-end: the budget actually gates the real generate_json/generate_text entry points.

Unit-testing app/llm_budget.py in isolation would prove the accounting works while the router
happily ignored it. These tests drive the actual public functions every one of the 78 call
sites uses, with the provider stubbed, and assert on what the router does.
"""
import pytest

import app.llm_client as llm
from app.db.models import Parameter
from app.gemini_client import GeminiError
from app.llm_budget import LlmBudgetExceeded, LlmDailyUsage, calls_today, is_exhausted

TENANT = 2


@pytest.fixture
def db(db_factory):
    return db_factory([LlmDailyUsage, Parameter])


@pytest.fixture(autouse=True)
def _clear_process_memo():
    """llm_client keeps an in-process fast-path dict alongside the DB record. Clear it between
    tests so one test's exhaustion cannot leak into the next."""
    llm._EXHAUSTED.clear()
    yield
    llm._EXHAUSTED.clear()


def test_a_successful_call_is_counted(db, monkeypatch):
    monkeypatch.setattr(llm, "PRIMARY", "gemini")
    monkeypatch.setattr(llm, "call_gemini_json", lambda p, d, t, max_tokens, model: {"ok": True})

    assert llm.generate_json("hi", db, TENANT) == {"ok": True}
    assert calls_today(db, TENANT) == 1


def test_rejected_calls_are_counted_too(db, monkeypatch):
    """A 429-rejected request still counts against Google's daily quota -- which is exactly why
    the retry storm this module documents ('368 quota-rejected requests for 1 useful answer')
    was self-defeating. A budget counting only successes would be blind to it."""
    monkeypatch.setattr(llm, "PRIMARY", "gemini")

    def _minute_limited(p, d, t, max_tokens, model):
        raise GeminiError("429 GenerateRequestsPerMinutePerProjectPerModel-FreeTier")

    monkeypatch.setattr(llm, "call_gemini_json", _minute_limited)
    monkeypatch.setattr(llm, "call_claude_json", lambda p, d, t, max_tokens: {"via": "claude"})
    # The per-minute path deliberately sleeps 20s before its single retry; skip that here.
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    llm.generate_json("hi", db, TENANT)
    assert calls_today(db, TENANT) > 0, "attempts that were rejected must still be counted"


def test_daily_quota_429_marks_the_model_exhausted_durably(db, monkeypatch):
    """THE REDEPLOY FIX, through the real router. This fact used to live only in a dict that
    every restart cleared."""
    monkeypatch.setattr(llm, "PRIMARY", "gemini")

    def _day_limited(p, d, t, max_tokens, model):
        raise GeminiError("429 GenerateRequestsPerDayPerProjectPerModel-FreeTier")

    monkeypatch.setattr(llm, "call_gemini_json", _day_limited)
    monkeypatch.setattr(llm, "call_claude_json", lambda p, d, t, max_tokens: {"via": "claude"})

    llm.generate_json("hi", db, TENANT)

    from app.gemini_client import DEFAULT_MODEL
    assert is_exhausted(db, TENANT, DEFAULT_MODEL) is True, (
        "a PerDay 429 must be remembered in the database, not just in process memory"
    )


def test_the_cap_stops_the_call_before_the_provider_is_touched(db, monkeypatch):
    """THE POINT. Once the cap is reached the router must refuse, not grind through doomed
    429s at timeout speed -- which is what makes a run look hung rather than out of quota."""
    db.add(Parameter(tenant_id=TENANT, key="llm_daily_call_budget", value={"calls": 2}))
    db.commit()
    monkeypatch.setattr(llm, "PRIMARY", "gemini")

    calls = {"n": 0}

    def _ok(p, d, t, max_tokens, model):
        calls["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(llm, "call_gemini_json", _ok)

    llm.generate_json("one", db, TENANT)
    llm.generate_json("two", db, TENANT)
    assert calls["n"] == 2

    with pytest.raises(LlmBudgetExceeded):
        llm.generate_json("three", db, TENANT)
    assert calls["n"] == 2, "the provider must not be called once the cap is reached"


def test_generate_text_is_gated_too(db, monkeypatch):
    """Both public entry points, not just the JSON one -- generate_text is used by
    message_draft.py, which is the single largest LLM consumer in the system."""
    db.add(Parameter(tenant_id=TENANT, key="llm_daily_call_budget", value={"calls": 1}))
    db.commit()
    monkeypatch.setattr(llm, "PRIMARY", "gemini")
    monkeypatch.setattr(llm, "call_gemini", lambda p, d, t, max_tokens, model: "hello")

    assert llm.generate_text("one", db, TENANT) == "hello"
    with pytest.raises(LlmBudgetExceeded):
        llm.generate_text("two", db, TENANT)


def test_an_exhausted_model_is_skipped_without_being_retried(db, monkeypatch):
    """The whole point of remembering exhaustion: never spend quota rediscovering it."""
    from app.gemini_client import DEFAULT_MODEL

    monkeypatch.setattr(llm, "PRIMARY", "gemini")
    tried = []

    def _track(p, d, t, max_tokens, model):
        tried.append(model)
        return {"ok": True}

    monkeypatch.setattr(llm, "call_gemini_json", _track)

    from app.llm_budget import mark_exhausted
    mark_exhausted(db, TENANT, DEFAULT_MODEL)

    llm.generate_json("hi", db, TENANT)
    assert DEFAULT_MODEL not in tried, "a model known to be out of daily quota must not be retried"
