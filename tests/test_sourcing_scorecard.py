"""Phase 8: turn route_attempts into scorecards and drift flags, without ever letting history
override this run's own free pre-flight.

The real gap this phase starts by closing: run_icp_filters (the actual production entry point)
called search_icypeas() directly, bypassing the planner entirely -- phase 6's ranking and
failover were built and tested but had never once run in production, and route_attempts was
always empty. Scorecards would have had nothing to aggregate.
"""
from datetime import datetime, timedelta

import pytest

from app.db.models import Batch, CampaignPush, Company, Contact, Parameter, Tenant
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
from app.gtm_os.learning.message_draft import MessageDraft
from app.gtm_os.opportunity.opportunity import Opportunity
from app.gtm_os.orchestration.control import DEFAULT_GTM_OS_CONTROL_CONFIG, set_control_config
from app.gtm_os.plays.lead import GtmLead
from app.gtm_os.strategy.strategy import GtmStrategy
from app.gtm_os.sourcing import atoms as A
from app.gtm_os.sourcing import outcomes as O
from app.gtm_os.sourcing import planner as P
from app.gtm_os.sourcing import scorecard as S
from app.gtm_os.sourcing.models import (
    IcpExclusion, IcpTermResolution, ProviderTaxonomyValue, RouteAttempt,
)
from app.spend_ledger import ProviderSpend

PARTNER, BILLING = 15, 2
ICP = {"employee_min": 11, "employee_max": 50, "industries": ["Professional Services"]}


@pytest.fixture
def db(db_factory):
    db = db_factory([Tenant, Parameter, ProviderSpend, GtmLead, Batch, Company, Contact,
                     CampaignPush, ProblemHypothesis, DemandHypothesis, Opportunity, GtmStrategy,
                     MessageDraft, RouteAttempt, IcpExclusion, IcpTermResolution,
                     ProviderTaxonomyValue])
    for tid, name in ((BILLING, "Elephant Edge"), (PARTNER, "Majji")):
        db.add(Tenant(id=tid, name=name, slug=name.lower()))
    config = DEFAULT_GTM_OS_CONTROL_CONFIG.copy()
    config["spend"] = {"daily_cap_usd": 1.0, "run_cap_usd": 0.5}
    set_control_config(db, BILLING, config)
    db.commit()
    return db


def _attempt(db, provider, outcome, fingerprint, when, rows=0, cost=None):
    db.add(RouteAttempt(tenant_id=PARTNER, provider=provider, endpoint="find-companies",
                        outcome=outcome, icp_fingerprint=fingerprint, rows=rows, cost_usd=cost,
                        attempted_at=when))
    db.commit()


# ---- fingerprint: same shape, different wording ----

def test_the_same_shape_fingerprints_identically_regardless_of_wording():
    a = A.fingerprint({"employee_min": 11, "employee_max": 50, "industries": ["Professional Services"]})
    b = A.fingerprint({"employee_max": 50, "employee_min": 11, "industries": ["Professional Services"]})
    assert a == b


def test_a_different_shape_fingerprints_differently():
    a = A.fingerprint({"employee_min": 11, "employee_max": 50})
    b = A.fingerprint({"employee_min": 100, "employee_max": 500})
    assert a != b


# ---- aggregation ----

def test_provider_scorecard_aggregates_across_shapes(db):
    now = datetime.utcnow()
    _attempt(db, "icypeas", O.OK, "shape-a", now, rows=5, cost=0.175)
    _attempt(db, "icypeas", O.UNAVAILABLE, "shape-b", now, rows=0)
    card = S.provider_scorecard(db, "icypeas")
    assert card.attempts == 2
    assert card.successes == 1
    assert card.success_rate == 0.5


def test_shape_scorecard_only_counts_that_one_shape(db):
    now = datetime.utcnow()
    _attempt(db, "icypeas", O.OK, "shape-a", now, rows=5, cost=0.175)
    _attempt(db, "icypeas", O.UNAVAILABLE, "shape-b", now, rows=0)
    card = S.shape_scorecard(db, "icypeas", "shape-a")
    assert card.attempts == 1
    assert card.success_rate == 1.0


def test_cost_per_row_divides_real_totals(db):
    now = datetime.utcnow()
    _attempt(db, "icypeas", O.OK, "shape-a", now, rows=25, cost=0.175)
    card = S.provider_scorecard(db, "icypeas")
    assert round(card.cost_per_row, 4) == round(0.175 / 25, 4)


def test_too_few_attempts_is_not_conclusive(db):
    _attempt(db, "icypeas", O.OK, "shape-a", datetime.utcnow(), rows=5)
    card = S.shape_scorecard(db, "icypeas", "shape-a")
    assert card.conclusive is False
    assert card.attempts < S.MIN_ATTEMPTS


# ---- drift: the actual point of this phase ----

def test_a_shape_that_always_worked_shows_no_drift(db):
    now = datetime.utcnow()
    for days_ago in range(10):
        _attempt(db, "icypeas", O.OK, "shape-a", now - timedelta(days=days_ago), rows=5, cost=0.175)
    assert S.detect_drift(db, "icypeas", "shape-a", now=now) is None


def test_a_shape_that_regressed_is_flagged(db):
    now = datetime.utcnow()
    # Healthy baseline, well outside the recent window.
    for days_ago in range(10, 20):
        _attempt(db, "icypeas", O.OK, "shape-a", now - timedelta(days=days_ago), rows=5, cost=0.175)
    # Recent window: consistently failing.
    for hours_ago in (1, 12, 24):
        _attempt(db, "icypeas", O.EMPTY_SUSPECT, "shape-a", now - timedelta(hours=hours_ago))

    flag = S.detect_drift(db, "icypeas", "shape-a", now=now)
    assert flag is not None
    assert flag.baseline.success_rate == 1.0
    assert flag.recent.success_rate == 0.0
    assert "shape-a" not in flag.reason  # reason is plain language, not a fingerprint dump


def test_a_shape_that_never_worked_is_not_regression_it_is_just_bad(db):
    """Never having worked is not a REGRESSION -- there is nothing to compare it against."""
    now = datetime.utcnow()
    for days_ago in range(10, 20):
        _attempt(db, "icypeas", O.EMPTY_SUSPECT, "shape-a", now - timedelta(days=days_ago))
    for hours_ago in (1, 12, 24):
        _attempt(db, "icypeas", O.EMPTY_SUSPECT, "shape-a", now - timedelta(hours=hours_ago))
    assert S.detect_drift(db, "icypeas", "shape-a", now=now) is None


def test_ordinary_noise_is_not_flagged_as_drift(db):
    """A dip from 100% to 60% is real variance, not a collapse -- must not spam flags."""
    now = datetime.utcnow()
    for days_ago in range(10, 20):
        _attempt(db, "icypeas", O.OK, "shape-a", now - timedelta(days=days_ago), rows=5)
    outcomes = [O.OK, O.OK, O.OK, O.EMPTY_SUSPECT, O.EMPTY_SUSPECT]
    for i, outcome in enumerate(outcomes):
        _attempt(db, "icypeas", outcome, "shape-a", now - timedelta(hours=i + 1), rows=5 if outcome == O.OK else 0)
    assert S.detect_drift(db, "icypeas", "shape-a", now=now) is None


def test_insufficient_recent_evidence_is_not_flagged(db):
    now = datetime.utcnow()
    for days_ago in range(10, 20):
        _attempt(db, "icypeas", O.OK, "shape-a", now - timedelta(days=days_ago), rows=5)
    _attempt(db, "icypeas", O.EMPTY_SUSPECT, "shape-a", now - timedelta(hours=1))   # just one
    assert S.detect_drift(db, "icypeas", "shape-a", now=now) is None


def test_sweep_drift_only_checks_shapes_actually_seen_recently(db):
    now = datetime.utcnow()
    for days_ago in range(10, 20):
        _attempt(db, "icypeas", O.OK, "stale-shape", now - timedelta(days=days_ago + 40))  # outside window
    shapes = S.known_shapes(db, "icypeas", now=now)
    assert "stale-shape" not in shapes


# ---- the "never substitutes" rule, enforced in the planner ----

def test_history_only_breaks_ties_never_overrides_coverage():
    """A provider with WORSE coverage must never outrank one with better coverage, no matter how
    good its track record is for this shape -- the design doc's own explicit rule, tested directly
    against the sort key rather than two real providers, which may happen to tie on coverage for
    any given ICP today (Icypeas and Prospeo currently do, for every atom either verifiably
    supports)."""
    worse_coverage_perfect_history = P.Candidate(
        endpoint=None, coverage=None, score=0.5, shape_success_rate=1.0,
        executable=True, healthy=True, recent_failures=0)
    better_coverage_no_history = P.Candidate(
        endpoint=None, coverage=None, score=0.9, shape_success_rate=None,
        executable=True, healthy=True, recent_failures=0)

    ranked = sorted([worse_coverage_perfect_history, better_coverage_no_history],
                    key=P._sort_key, reverse=True)
    assert ranked[0] is better_coverage_no_history


def test_among_equal_coverage_track_record_breaks_the_tie():
    proven = P.Candidate(endpoint=None, coverage=None, score=0.7, shape_success_rate=1.0,
                         executable=True, healthy=True, recent_failures=0)
    unproven = P.Candidate(endpoint=None, coverage=None, score=0.7, shape_success_rate=None,
                           executable=True, healthy=True, recent_failures=0)
    ranked = sorted([unproven, proven], key=P._sort_key, reverse=True)
    assert ranked[0] is proven


def test_an_inconclusive_shape_history_is_neutral_not_penalized(db):
    """A brand-new ICP shape has no history anywhere -- that must not rank it below a shape with
    a mediocre-but-conclusive history. icypeas is already registered at import time by
    app.gtm_os.plays.icp_filters, so this reads real state rather than registering its own --
    clearing _ADAPTERS here would leak into every test that runs afterward in the same session."""
    ranked = P.rank(db, ICP)
    icypeas = next(c for c in ranked if c.provider == "icypeas")
    assert icypeas.shape_success_rate is None
    assert icypeas.executable is True


# ---- wiring: run_icp_filters actually goes through the planner now ----

def test_run_icp_filters_records_a_real_route_attempt(db, monkeypatch):
    """The gap this phase closed: the production entry point used to call search_icypeas()
    directly, so route_attempts stayed empty forever and scorecards had nothing to read."""
    import app.deepline_client as dc
    from app.gtm_os.plays import icp_filters as play
    from app.phases.partner_icp import PARTNER_ICP_PARAMETER_KEY

    monkeypatch.setattr(dc, "_call_deepline_cli", lambda tool, payload: {
        "toolResponse": {"raw": {"leads": [], "pagination": {"token": None}}}})
    db.add(Parameter(tenant_id=PARTNER, key=PARTNER_ICP_PARAMETER_KEY, value=ICP))
    db.commit()

    result = play.run_icp_filters(db, PARTNER, run_cap_usd=0.5)

    assert result["status"] == "completed"
    assert "routing" in result
    assert result["routing"]["provider"] == "icypeas"
    attempt = db.query(RouteAttempt).filter(RouteAttempt.tenant_id == PARTNER).one()
    assert attempt.provider == "icypeas"
    assert attempt.icp_fingerprint is not None


# ---- explanation: plain language, not an outcome-code dump ----

def test_explain_run_is_plain_language_not_raw_outcome_codes(db):
    _attempt(db, "icypeas", O.UNAVAILABLE, "shape-a", datetime.utcnow(), rows=0)
    explanation = S.explain_run(db, PARTNER)
    assert len(explanation) == 1
    assert explanation[0]["result"] == O.POLICIES[O.UNAVAILABLE].explanation
    assert "UNAVAILABLE" not in explanation[0]["result"]


def test_explain_run_is_scoped_to_the_asking_tenant(db):
    db.add(Tenant(id=99, name="Other", slug="other"))
    db.commit()
    db.add(RouteAttempt(tenant_id=99, provider="icypeas", outcome=O.OK,
                        attempted_at=datetime.utcnow()))
    db.commit()
    assert S.explain_run(db, PARTNER) == []
