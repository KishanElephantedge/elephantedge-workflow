"""Tests for the investigation objective selection order.

THE BUG THIS PINS. Sorting by `attempts` ascending as the primary tiebreak means a
never-attempted objective (attempts=0) always outranks one that already made an attempt and is
ready to retry (attempts>=1), no matter how long the retry-ready one has been sitting eligible.
S2 creates fresh attempts=0 objectives every tick with no cap, so the supply of "beats everything
at attempts>=1" candidates never runs out -- a starved objective is starved forever, not just
temporarily.

Confirmed live against production before the fix: 200 eligible objectives (124 never-attempted,
76 cooldown-elapsed retry-ready), with every one of the 76 ranked dead last.
"""
from datetime import datetime, timedelta

from app.gtm_os.intelligence.investigation_cycle import _objective_priority_key


class _Obj:
    """A minimal stand-in for InvestigationObjective -- only the fields the sort reads."""
    def __init__(self, id, target_company_id=1, attempts=0, next_eligible_at=None,
                created_at=None, evidence_sought="opening_tier"):
        self.id = id
        self.target_company_id = target_company_id
        self.attempts = attempts
        self.next_eligible_at = next_eligible_at
        self.created_at = created_at
        self.evidence_sought = evidence_sought


T0 = datetime(2026, 9, 16, 8, 0, 0)


def test_the_production_starvation_case_is_fixed():
    """THE FIX, using the exact live shape: a retry-ready objective whose cooldown cleared
    hours ago must outrank a fresh objective created moments ago -- the opposite of the old
    attempts-first order."""
    retry_ready = _Obj(id=1, attempts=1, next_eligible_at=T0)  # eligible since T0
    just_created = _Obj(id=2, attempts=0, created_at=T0 + timedelta(hours=2))  # eligible since T0+2h

    ordered = sorted([just_created, retry_ready], key=_objective_priority_key)
    assert ordered[0] is retry_ready, "the objective waiting longer must be selected first"


def test_many_fresh_objectives_cannot_permanently_bury_one_retry_ready_objective():
    """The concrete production shape: 124 fresh objectives created across 09-16 to 09-18,
    against 76 retry-ready ones whose cooldown elapsed earlier. Every retry-ready objective
    must rank ahead of every objective created after it became eligible."""
    retry_ready = _Obj(id=1000, attempts=1, next_eligible_at=T0)

    fresh_created_later = [
        _Obj(id=i, attempts=0, created_at=T0 + timedelta(hours=i))
        for i in range(1, 125)
    ]

    ordered = sorted([retry_ready, *fresh_created_later], key=_objective_priority_key)
    assert ordered[0] is retry_ready, (
        "an objective that has been eligible since T0 must not be starved by 124 objectives "
        "created after it became eligible, regardless of their lower attempt count"
    )


def test_a_never_attempted_objective_still_wins_against_a_LATER_retry():
    """Not a blanket 'retries always first' rule -- wait time is what matters. A never-attempted
    objective created early outranks a retry-ready one whose cooldown cleared later."""
    created_early = _Obj(id=1, attempts=0, created_at=T0)
    retry_ready_later = _Obj(id=2, attempts=1, next_eligible_at=T0 + timedelta(hours=1))

    ordered = sorted([retry_ready_later, created_early], key=_objective_priority_key)
    assert ordered[0] is created_early


def test_company_specific_still_outranks_company_agnostic_regardless_of_wait():
    """Preserved from the original design: a real target_company_id (backed by an actual
    ICPMatch) outranks a company-agnostic objective, even one waiting much longer."""
    agnostic_long_wait = _Obj(id=1, target_company_id=None, created_at=T0)
    specific_short_wait = _Obj(id=2, target_company_id=42, created_at=T0 + timedelta(days=1))

    ordered = sorted([agnostic_long_wait, specific_short_wait], key=_objective_priority_key)
    assert ordered[0] is specific_short_wait


def test_attempts_is_still_a_real_tiebreak_on_exact_wait_time_ties():
    a = _Obj(id=1, attempts=2, created_at=T0)
    b = _Obj(id=2, attempts=0, created_at=T0)  # same instant

    ordered = sorted([a, b], key=_objective_priority_key)
    assert ordered[0] is b, "fewer attempts wins only as a tiebreak, not as the primary order"


def test_a_never_created_timestamp_does_not_crash_the_sort():
    """Defensive: created_at is nullable in principle even though it defaults on write."""
    broken = _Obj(id=1, attempts=0, created_at=None, next_eligible_at=None)
    normal = _Obj(id=2, attempts=0, created_at=T0)

    ordered = sorted([broken, normal], key=_objective_priority_key)
    assert ordered[0] is broken, "missing timestamps sort as the oldest, not crash or sort last"
