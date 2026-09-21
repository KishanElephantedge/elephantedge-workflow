"""Tests for respecting a partner's explicitly-stated buyer persona in decision-maker search.

Real gap found for the "majji" partner tenant: CEO_FIRST_MAX_EMPLOYEES=50 is an
Elephant-Edge-calibrated heuristic ("CEO-first made sense for icp_1's 11-50 employee band but
not icp_2/icp_3's 125-300 band"), applied globally regardless of tenant. Majji's real, stated
ICP is Owner/Founder/CEO/Co-Founder across his whole 30-100 employee band -- roughly half that
band (50-100) would otherwise try the sales-leader tier FIRST, contradicting his explicit
instruction. CEO_TITLE_KEYWORDS already covers "ceo"/"founder"/"owner" -- this is purely about
tier ORDER, not a new title vocabulary.
"""
import pytest

from app.db.models import Parameter
from app.phases.decision_maker import (
    BROADER_LEADERSHIP_FILTER, CEO_FILTER, SALES_LEADER_FILTER, _partner_wants_ceo_tier_first,
    _size_ordered_tiers,
)

TENANT = 15  # majji


@pytest.fixture
def db(db_factory):
    return db_factory([Parameter])


class _Company:
    def __init__(self, employee_count):
        self.employee_count = employee_count


TIERS = [
    (CEO_FILTER, ["ceo", "founder", "owner"], True, "founder_ceo"),
    (SALES_LEADER_FILTER, ["vp sales"], False, "sales_leader"),
    (BROADER_LEADERSHIP_FILTER, ["cto"], False, "other_leadership"),
]


def test_partner_wants_ceo_first_when_persona_matches(db):
    db.add(Parameter(tenant_id=TENANT, key="partner_icp", value={
        "decision_maker_titles": ["Owner", "Founder", "CEO", "Co-Founder"],
    }))
    db.commit()
    assert _partner_wants_ceo_tier_first(db, TENANT) is True


def test_a_different_stated_persona_does_not_force_ceo_first(db):
    """Only overrides when the stated persona is actually founder/CEO/owner-shaped -- a partner
    who named 'VP Sales' as their buyer should keep the existing size-based order, since that
    persona lives in a different tier entirely."""
    db.add(Parameter(tenant_id=TENANT, key="partner_icp", value={
        "decision_maker_titles": ["VP Sales", "Head of Sales"],
    }))
    db.commit()
    assert _partner_wants_ceo_tier_first(db, TENANT) is False


def test_no_partner_icp_at_all_defaults_to_unchanged_behavior(db):
    assert _partner_wants_ceo_tier_first(db, TENANT) is False


def test_no_decision_maker_titles_field_defaults_to_unchanged_behavior(db):
    db.add(Parameter(tenant_id=TENANT, key="partner_icp", value={"employee_min": 30}))
    db.commit()
    assert _partner_wants_ceo_tier_first(db, TENANT) is False


def test_elephant_edges_own_tenant_is_unaffected(db):
    """Elephant Edge has no partner_icp row at all -- must remain on the existing,
    proven-by-real-experience size-based ordering."""
    assert _partner_wants_ceo_tier_first(db, 2) is False


# --- _size_ordered_tiers with force_ceo_first ---------------------------------------------------

def test_force_ceo_first_keeps_ceo_tier_first_above_the_size_threshold():
    """THE FIX. A 75-employee company (above CEO_FIRST_MAX_EMPLOYEES=50) would normally try
    sales-leader first -- force_ceo_first must keep the CEO/Founder/Owner tier first regardless."""
    company = _Company(employee_count=75)
    ordered = _size_ordered_tiers(company, TIERS, force_ceo_first=True)
    assert ordered[0][3] == "founder_ceo"


def test_without_the_override_the_existing_size_behavior_is_unchanged():
    """Regression guard: Elephant Edge's own proven behavior must not change."""
    company = _Company(employee_count=75)
    ordered = _size_ordered_tiers(company, TIERS, force_ceo_first=False)
    assert ordered[0][3] == "sales_leader"

    small_company = _Company(employee_count=30)
    ordered_small = _size_ordered_tiers(small_company, TIERS, force_ceo_first=False)
    assert ordered_small[0][3] == "founder_ceo"


def test_force_ceo_first_is_a_no_op_for_a_small_company_anyway():
    company = _Company(employee_count=30)
    ordered = _size_ordered_tiers(company, TIERS, force_ceo_first=True)
    assert ordered[0][3] == "founder_ceo"
