"""Tests for the new sales+marketing team-size dimension on a partner ICP.

Built for the "majji" partner tenant's real ICP: companies with a small, under-resourced
sales+marketing function (2-3 people) but enough headcount to afford fixing it (30-100
employees). employee_min/max already covers company size via headcount_band_for_partner_icp;
this is the missing DEPARTMENT-size check -- no partner ICP has ever had one before.

The measured-vs-proxy split mirrors the revenue check's own discipline exactly: a KNOWN
violation (real sales_headcount_percent/marketing_headcount_percent on file) drops the company;
an estimated mismatch (no measured percentage, only the tenant-wide proxy) goes to needs_review,
never straight to deletion -- "absence of a figure is not evidence of a bad fit."
"""
import pytest

from app.db.models import Batch, Company, Contact, Parameter, Score, Signal
from app.gtm_os.icp.icp_matching import estimated_sales_and_marketing_team_size_range
from app.phases.partner_pipeline import enforce_icp_on_companies

TENANT = 15  # majji


@pytest.fixture
def db(db_factory):
    # Parameter: the drop path calls add_rejected_domain(), a Parameter-backed exclusion list.
    # Signal/Score/Contact: Company's cascade="all, delete-orphan" relationships -- db.delete(c)
    # touches all three regardless of whether any row exists.
    return db_factory([Company, Batch, Parameter, Signal, Score, Contact])


def _company(db, batch, **kw):
    defaults = dict(batch_id=batch.id, name="Co", domain="co.com", employee_count=50)
    defaults.update(kw)
    c = Company(**defaults)
    db.add(c)
    db.commit()
    return c


def test_measured_percentages_give_an_exact_estimate():
    class C:
        employee_count = 40
        sales_headcount_percent = 5.0
        marketing_headcount_percent = 2.5
    low, high, is_proxy, evidence = estimated_sales_and_marketing_team_size_range(C())
    assert low == high == pytest.approx(3.0)  # 40 * 7.5%
    assert is_proxy is False
    assert "measured" in evidence


def test_only_sales_measured_treats_marketing_as_zero_not_unknown():
    """A real 0% marketing function is information, not a missing one."""
    class C:
        employee_count = 40
        sales_headcount_percent = 5.0
        marketing_headcount_percent = None
    low, high, is_proxy, _ = estimated_sales_and_marketing_team_size_range(C())
    assert low == high == pytest.approx(2.0)  # 40 * 5%, marketing treated as 0
    assert is_proxy is False


def test_neither_measured_falls_back_to_a_labeled_sales_only_proxy():
    class C:
        employee_count = 30
        sales_headcount_percent = None
        marketing_headcount_percent = None
    low, high, is_proxy, evidence = estimated_sales_and_marketing_team_size_range(C())
    assert low == pytest.approx(30 * 11.87 / 100)
    assert high == pytest.approx(30 * 28.13 / 100)
    assert is_proxy is True
    assert "SALES ONLY" in evidence


def test_no_employee_count_returns_unknown():
    class C:
        employee_count = None
        sales_headcount_percent = 5.0
        marketing_headcount_percent = 2.0
    assert estimated_sales_and_marketing_team_size_range(C()) == (None, None, False, None)


# --- enforce_icp_on_companies integration -----------------------------------------------------

def test_unconfigured_team_size_is_a_no_op(db):
    """None/None must not change behavior for every partner ICP that predates this field."""
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    c = _company(db, batch, employee_count=50, sales_headcount_percent=20, marketing_headcount_percent=20)

    result = enforce_icp_on_companies(db, TENANT, [c], icp={}, enrich_revenue=False)
    assert result["kept"] == [c]
    assert result["dropped"] == []
    assert result["needs_review"] == []


def test_measured_team_size_outside_band_is_dropped_and_removed(db):
    """A KNOWN violation -- Majji's actual criterion, 2-3 people, with a company measured well
    above it (e.g. an already-well-staffed sales+marketing function)."""
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    # 50 employees * (12% sales + 8% marketing) = 10.0 estimated combined -- well above 3.
    c = _company(db, batch, employee_count=50, sales_headcount_percent=12.0, marketing_headcount_percent=8.0)

    result = enforce_icp_on_companies(
        db, TENANT, [c], icp={"sales_team_size_min": 2, "sales_team_size_max": 3}, enrich_revenue=False,
    )
    assert result["kept"] == []
    assert len(result["dropped"]) == 1
    assert "outside configured 2-3" in result["dropped"][0][1]
    assert db.query(Company).filter(Company.id == c.id).first() is None, "a known violation must be deleted, matching the revenue check's own discipline"


def test_measured_team_size_inside_band_is_kept(db):
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    # 40 employees * (4% sales + 3% marketing) = 2.8 -- inside 2-3.
    c = _company(db, batch, employee_count=40, sales_headcount_percent=4.0, marketing_headcount_percent=3.0)

    result = enforce_icp_on_companies(
        db, TENANT, [c], icp={"sales_team_size_min": 2, "sales_team_size_max": 3}, enrich_revenue=False,
    )
    assert result["kept"] == [c]
    assert result["dropped"] == []


def test_unmeasured_team_size_mismatch_goes_to_needs_review_not_dropped(db):
    """THE SAFETY CASE. No calibrated marketing-headcount percentile exists for any tenant, so a
    proxy-only mismatch must go to a human, never straight to deletion -- same 'absence of a
    figure is not evidence of a bad fit' rule the revenue check already applies."""
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    # No measured percentages -> proxy range 30*11.87%-30*28.13% = 3.6-8.4, entirely above max=3.
    c = _company(db, batch, employee_count=30, sales_headcount_percent=None, marketing_headcount_percent=None)

    result = enforce_icp_on_companies(
        db, TENANT, [c], icp={"sales_team_size_min": 2, "sales_team_size_max": 3}, enrich_revenue=False,
    )
    assert result["kept"] == []
    assert result["dropped"] == [], "an unmeasured (proxy) mismatch must never be deleted outright"
    assert len(result["needs_review"]) == 1
    assert db.query(Company).filter(Company.id == c.id).first() is not None, "needs_review companies are never deleted"


def test_a_range_that_overlaps_the_band_is_kept_even_as_a_proxy(db):
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    # employee_count small enough that even the proxy range overlaps 2-3.
    # 15 * 11.87%-28.13% = 1.78-4.22, overlaps [2,3].
    c = _company(db, batch, employee_count=15, sales_headcount_percent=None, marketing_headcount_percent=None)

    result = enforce_icp_on_companies(
        db, TENANT, [c], icp={"sales_team_size_min": 2, "sales_team_size_max": 3}, enrich_revenue=False,
    )
    assert result["kept"] == [c]


def test_no_employee_count_is_never_rejected_on_team_size(db):
    """Cannot estimate at all -> must not be treated as a violation."""
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    c = _company(db, batch, employee_count=None)

    result = enforce_icp_on_companies(
        db, TENANT, [c], icp={"sales_team_size_min": 2, "sales_team_size_max": 3}, enrich_revenue=False,
    )
    assert result["kept"] == [c]


def test_one_sided_band_is_respected(db):
    """sales_team_size_min alone (no max) -- reject only companies estimated clearly too small."""
    batch = Batch(tenant_id=TENANT, name="b")
    db.add(batch)
    db.commit()
    # 50 * 1% = 0.5, measured exact, below min=2.
    c = _company(db, batch, employee_count=50, sales_headcount_percent=1.0, marketing_headcount_percent=0.0)

    result = enforce_icp_on_companies(
        db, TENANT, [c], icp={"sales_team_size_min": 2}, enrich_revenue=False,
    )
    assert result["kept"] == []
    assert len(result["dropped"]) == 1
