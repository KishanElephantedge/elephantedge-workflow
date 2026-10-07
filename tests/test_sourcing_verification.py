"""Phase 4: judge the rows, not the filter we believe we sent.

Replays the real 2026-10-03 batch -- a Professional Services ICP that returned hospitals, law
firms, construction and a fire department with a 200 OK, and was noticed only after 21 companies
had been bought and processed.
"""
from app.gtm_os.sourcing import atoms as A
from app.gtm_os.sourcing import outcomes as O
from app.gtm_os.sourcing.verification import verify_sample

MAJJI = {"employee_min": 11, "employee_max": 50, "revenue_min_usd": 2_500_000,
         "revenue_max_usd": 5_000_000, "industries": ["Professional Services"]}


def _row(name, employees=None, industry=None, revenue=None):
    row = {"name": name, "url": f"https://www.linkedin.com/company/{name.lower()}"}
    if employees is not None:
        row["numberOfEmployees"] = employees
    if industry is not None:
        row["industry"] = industry
    if revenue is not None:
        lo, hi = revenue
        row["estimatedRevenuRange"] = {
            "estimatedMinRevenue": {"amount": lo, "unit": "MILLION"},
            "estimatedMaxRevenue": {"amount": hi, "unit": "MILLION"},
        }
    return row


def test_the_real_wrong_batch_is_caught_on_the_sample():
    """The 21 companies that actually came back, by headcount: all far outside the 11-50 band."""
    rows = [
        _row("AmeriBest Home Health", employees=150),
        _row("The Irwin Law Firm", employees=120),
        _row("Peak Alliance", employees=200),
        _row("Danpal A/S", employees=300),
        _row("Longboat Key Fire Rescue", employees=160),
        _row("Lakeland Elementary Schools", employees=140),
    ]
    atoms = A.decompose_icp(MAJJI)
    checkable = [a for a in atoms.must_haves() if a.key == A.HEADCOUNT]

    verification = verify_sample(rows, atoms, checkable_atoms=checkable)

    assert verification.conclusive is True
    assert verification.match_rate == 0.0
    assert verification.passed() is False
    assert verification.violations["headcount"] == 6


def test_a_good_batch_passes():
    rows = [_row(f"Co{i}", employees=20, revenue=(3, 4)) for i in range(6)]
    atoms = A.decompose_icp(MAJJI)
    checkable = [a for a in atoms.must_haves() if a.key in (A.HEADCOUNT, A.REVENUE)]
    verification = verify_sample(rows, atoms, checkable_atoms=checkable)
    assert (verification.match_rate, verification.passed()) == (1.0, True)


def test_missing_data_is_never_counted_as_a_violation():
    """The BePresent lesson, already paid for once: a stale or absent value is not evidence of a
    mismatch, and treating it as one discards genuine matches."""
    rows = [_row(f"Co{i}") for i in range(6)]          # no headcount, no revenue, no industry
    atoms = A.decompose_icp(MAJJI)
    verification = verify_sample(rows, atoms, checkable_atoms=atoms.must_haves())

    assert verification.checked == 0
    assert verification.violations == {}
    assert verification.unverifiable["headcount"] == 6
    assert verification.passed() is True               # refusing to judge is not failing


def test_a_tiny_sample_cannot_disable_a_working_provider():
    rows = [_row("Co1", employees=900)]                # clearly wrong, but one row is not evidence
    atoms = A.decompose_icp(MAJJI)
    verification = verify_sample(rows, atoms, checkable_atoms=[a for a in atoms.must_haves()
                                                              if a.key == A.HEADCOUNT])
    assert verification.conclusive is False
    assert verification.passed() is True


def test_revenue_ranges_only_have_to_overlap():
    # An estimate of $1M-$5M genuinely can sit inside a $2.5M-$5M band; rejecting it would discard
    # a real match on the width of someone else's estimate.
    atoms = A.decompose_icp(MAJJI)
    checkable = [a for a in atoms.must_haves() if a.key == A.REVENUE]
    overlapping = verify_sample([_row(f"Co{i}", revenue=(1, 5)) for i in range(6)], atoms,
                                checkable_atoms=checkable)
    far_below = verify_sample([_row(f"Co{i}", revenue=(0.1, 0.4)) for i in range(6)], atoms,
                              checkable_atoms=checkable)
    assert overlapping.match_rate == 1.0
    assert far_below.match_rate == 0.0


def test_industry_is_only_judged_when_we_actually_searched_a_taxonomy_value():
    """If the search used the free-text keyword fallback, the provider never promised a
    classification -- so scoring its industry label against the partner's wording would invent
    failures. The caller signals this by leaving industry out of checkable_atoms."""
    rows = [_row(f"Co{i}", industry="Law Practice") for i in range(6)]
    atoms = A.decompose_icp(MAJJI)

    judged = verify_sample(rows, atoms,
                           checkable_atoms=[a for a in atoms.by_key(A.INDUSTRY)])
    not_judged = verify_sample(rows, atoms, checkable_atoms=[])

    assert judged.match_rate == 0.0          # "Law Practice" != "Professional Services"
    assert not_judged.checked == 0           # nothing claimed, nothing scored


def test_outcome_policies_route_each_failure_differently():
    assert O.POLICIES[O.UNAVAILABLE].should_retry is False          # never hammer a dead endpoint
    assert O.POLICIES[O.UNAVAILABLE].should_switch_provider is True
    assert O.POLICIES[O.QUALITY_FAIL].should_switch_provider is True
    # A validated empty is a real gap -> try different coverage. An unvalidated empty is our own
    # bad value -> fixing it at a new provider would just repeat the mistake at a new price.
    assert O.POLICIES[O.EMPTY_VALIDATED].should_switch_provider is True
    assert O.POLICIES[O.EMPTY_SUSPECT].should_switch_provider is False
    assert O.POLICIES[O.BUDGET_BLOCKED].should_switch_provider is False


def test_exceptions_classify_onto_the_closed_set():
    from app.deepline_client import DeeplineError, DeeplineSpendBlocked

    assert O.classify_exception(DeeplineSpendBlocked("cap")).outcome == O.BUDGET_BLOCKED
    assert O.classify_exception(DeeplineError("429 too many requests")).outcome == O.RATE_LIMITED
    assert O.classify_exception(DeeplineError("401 unauthorized")).outcome == O.AUTH_ERROR
    assert O.classify_exception(DeeplineError("422 schema validation failed")).outcome == O.SCHEMA_ERROR
    assert O.classify_exception(TimeoutError("timed out")).outcome == O.UNAVAILABLE
    # Anything unrecognised is treated as unavailable, whose policy is switch-and-do-not-retry.
    assert O.classify_exception(RuntimeError("something new")).outcome == O.UNAVAILABLE
