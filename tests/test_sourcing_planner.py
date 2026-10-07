"""Mapping an ICP to the best tool, and switching tools when one fails.

Two behaviours matter most: the choice is driven by what the partner's ICP needs (not a fixed
preference order), and a failing provider hands off instead of taking the run down with it.
"""
from datetime import datetime, timedelta

import pytest

from app.db.models import Credential, Parameter, Tenant
from app.gtm_os.sourcing import atoms as A
from app.gtm_os.sourcing import exclusions as EX
from app.gtm_os.sourcing import outcomes as O
from app.gtm_os.sourcing import planner as P
from app.gtm_os.sourcing.models import (
    IcpExclusion, IcpTermResolution, ProviderTaxonomyValue, RouteAttempt,
)

PARTNER = 15
ICP = {"employee_min": 11, "employee_max": 50, "industries": ["Professional Services"]}


@pytest.fixture
def db(db_factory):
    return db_factory([Tenant, Parameter, Credential, IcpExclusion, RouteAttempt,
                       ProviderTaxonomyValue, IcpTermResolution])


@pytest.fixture(autouse=True)
def _clean_adapters():
    original = dict(P._ADAPTERS)
    P._ADAPTERS.clear()
    yield
    P._ADAPTERS.clear()
    P._ADAPTERS.update(original)


# ---- exclusions: stop paying for what we keep rejecting ----

def test_one_rejection_is_not_evidence_about_a_whole_industry(db):
    EX.record_rejection(db, PARTNER, "icypeas", A.INDUSTRY, "Public Safety", "government body")
    assert EX.learned_exclusions(db, PARTNER, "icypeas", A.INDUSTRY) == []


def test_a_repeated_rejection_becomes_a_provider_side_exclude(db):
    for _ in range(2):
        EX.record_rejection(db, PARTNER, "icypeas", A.INDUSTRY, "Public Safety", "government body")
    assert EX.learned_exclusions(db, PARTNER, "icypeas", A.INDUSTRY) == ["Public Safety"]


def test_what_the_partner_asked_for_is_never_excluded(db):
    """Their stated intent outranks our inference, however often rows from it were rejected."""
    for _ in range(5):
        EX.record_rejection(db, PARTNER, "icypeas", A.INDUSTRY, "Law Practice", "qualifier reject")
    assert EX.learned_exclusions(db, PARTNER, "icypeas", A.INDUSTRY,
                                protected={"law practice"}) == []


def test_exclusions_are_per_partner(db):
    for _ in range(2):
        EX.record_rejection(db, PARTNER, "icypeas", A.INDUSTRY, "Insurance", "not a buyer")
    assert EX.learned_exclusions(db, 99, "icypeas", A.INDUSTRY) == []


def test_a_wrong_inference_can_be_overridden_without_losing_the_evidence(db):
    for _ in range(3):
        EX.record_rejection(db, PARTNER, "icypeas", A.INDUSTRY, "Insurance", "not a buyer")
    EX.suppress(db, PARTNER, "icypeas", A.INDUSTRY, "Insurance")
    assert EX.learned_exclusions(db, PARTNER, "icypeas", A.INDUSTRY) == []
    assert db.query(IcpExclusion).one().rejection_count == 3      # evidence kept


def test_learned_exclusions_reach_the_real_query(db):
    from app.gtm_os.plays.icp_filters import icypeas_filters_for_icp

    for _ in range(2):
        EX.record_rejection(db, PARTNER, "icypeas", A.INDUSTRY, "Public Safety", "government body")
    filters = icypeas_filters_for_icp(ICP, db=db, tenant_id=PARTNER)
    assert "Public Safety" in filters["industry"]["exclude"]
    assert "Staffing and Recruiting" in filters["industry"]["exclude"]   # policy excludes remain


# ---- ranking: the ICP decides the tool ----

def test_a_provider_that_cannot_enforce_the_must_haves_ranks_below_one_that_can(db):
    P.register_adapter("icypeas", lambda *a, **k: {"companies": 1})
    ranked = P.rank(db, ICP)
    by_provider = {c.provider: c for c in ranked}
    assert by_provider["icypeas"].score > by_provider["apollo"].score
    assert ranked[0].provider == "icypeas"       # the only one with an adapter AND coverage


def test_a_provider_with_no_adapter_is_reported_rather_than_silently_dropped(db):
    ranked = P.rank(db, ICP)
    apollo = next(c for c in ranked if c.provider == "apollo")
    assert apollo.executable is False
    assert "no adapter" in apollo.why


def test_repeated_failures_circuit_break_a_provider(db):
    for _ in range(P.CIRCUIT_BREAK_FAILURES):
        P.record_attempt(db, PARTNER, "icypeas", "find-companies", O.UNAVAILABLE, detail="timeout")
    P.register_adapter("icypeas", lambda *a, **k: {"companies": 1})
    candidate = next(c for c in P.rank(db, ICP) if c.provider == "icypeas")
    assert candidate.healthy is False
    assert "circuit-broken" in candidate.why


def test_a_success_resets_health(db):
    for _ in range(3):
        P.record_attempt(db, PARTNER, "icypeas", "find-companies", O.UNAVAILABLE)
    P.record_attempt(db, PARTNER, "icypeas", "find-companies", O.OK, rows=5)
    assert P.recent_failures(db, "icypeas") == 0


def test_old_failures_do_not_hold_a_provider_down_forever(db):
    stale = datetime.utcnow() - (P.CIRCUIT_BREAK_WINDOW + timedelta(minutes=5))
    for _ in range(5):
        attempt = RouteAttempt(tenant_id=PARTNER, provider="icypeas", endpoint="find-companies",
                               outcome=O.UNAVAILABLE, attempted_at=stale)
        db.add(attempt)
    db.commit()
    assert P.recent_failures(db, "icypeas") == 0


# ---- execution: switch tools on the right failures ----

def test_a_dead_provider_hands_off_to_the_next_one(db):
    calls = []

    def dead(db_, tenant_id, icp, **kw):
        calls.append("icypeas")
        raise TimeoutError("timed out")

    def alive(db_, tenant_id, icp, **kw):
        calls.append("prospeo")
        return {"companies": 7, "created": 3}

    P.register_adapter("icypeas", dead)
    P.register_adapter("prospeo", alive)

    run = P.execute(db, PARTNER, ICP)

    assert calls == ["icypeas", "prospeo"]
    assert run.provider == "prospeo"
    assert run.result["companies"] == 7
    assert [a["outcome"] for a in run.attempts] == [O.UNAVAILABLE, O.OK]


def test_a_budget_block_stops_rather_than_spending_at_another_provider(db):
    """Our own cap is not the provider's fault -- switching would just spend the money elsewhere."""
    from app.deepline_client import DeeplineSpendBlocked

    calls = []

    def blocked(db_, tenant_id, icp, **kw):
        calls.append("icypeas")
        raise DeeplineSpendBlocked("run cap reached")

    P.register_adapter("icypeas", blocked)
    P.register_adapter("prospeo", lambda *a, **k: calls.append("prospeo") or {"companies": 5})

    run = P.execute(db, PARTNER, ICP)

    assert calls == ["icypeas"]
    assert run.attempts[0]["outcome"] == O.BUDGET_BLOCKED


def test_an_unvalidated_empty_does_not_pay_another_provider_to_repeat_our_mistake(db):
    calls = []
    P.register_adapter("icypeas", lambda *a, **k: calls.append("icypeas") or
                       {"companies": 0, "stopped": "no results"})
    P.register_adapter("prospeo", lambda *a, **k: calls.append("prospeo") or {"companies": 9})

    run = P.execute(db, PARTNER, ICP)

    assert calls == ["icypeas"]
    assert run.attempts[0]["outcome"] == O.EMPTY_SUSPECT


def test_a_validated_empty_does_try_a_provider_with_different_coverage(db):
    calls = []
    P.register_adapter("icypeas", lambda *a, **k: calls.append("icypeas") or
                       {"companies": 0, "free_count_checked": 0, "exhausted": True})
    P.register_adapter("prospeo", lambda *a, **k: calls.append("prospeo") or {"companies": 9})

    run = P.execute(db, PARTNER, ICP)

    assert calls == ["icypeas", "prospeo"]
    assert run.provider == "prospeo"


def test_every_attempt_is_recorded_for_the_next_run_to_rank_by(db):
    P.register_adapter("icypeas", lambda *a, **k: {"companies": 4, "spent_usd": 0.098})
    P.execute(db, PARTNER, ICP)
    attempt = db.query(RouteAttempt).one()
    assert (attempt.provider, attempt.outcome, attempt.rows) == ("icypeas", O.OK, 4)
    assert attempt.cost_usd == 0.098


def test_with_no_usable_provider_the_run_says_exactly_what_it_considered(db):
    run = P.execute(db, PARTNER, ICP)
    assert run.result is None
    assert "no usable provider" in run.stopped
    assert {c["provider"] for c in run.considered} >= {"icypeas", "prospeo", "apollo"}
