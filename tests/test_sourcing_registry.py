"""Phase 1 of the provider router: ICP -> atoms -> per-provider coverage.

These tests pin the three failures that motivated the design (see provider-router-design.md):
a requirement silently dropped, a filter name guessed at a call site, and "unsupported" concluded
from whichever provider happened to be in front of us.
"""
from app.gtm_os.sourcing import atoms as A
from app.gtm_os.sourcing import registry as R

MAJJI = {
    "industries": ["Professional Services"],
    "geographies": ["United States"],
    "revenue_min_usd": 2_500_000,
    "revenue_max_usd": 5_000_000,
    "employee_min": 11,
    "employee_max": 50,
    "decision_maker_titles": ["Owner", "Founder", "CEO", "Co-Founder"],
    "department_headcount": {"marketing": {"max": 0}},
    "notes": "No dedicated marketing hire.",
}


def test_every_stated_requirement_becomes_an_atom():
    names = {a.name for a in A.decompose_icp(MAJJI).atoms}
    assert names == {
        "headcount", "revenue", "geography", "industry",
        "department_headcount(marketing)", "decision_maker_title",
    }


def test_an_absent_field_is_not_a_requirement():
    # Only geography defaults (the pre-existing behaviour of every search here); nothing else is
    # invented, so a partner who never stated a revenue band does not silently acquire one.
    names = {a.name for a in A.decompose_icp({"employee_min": 11}).atoms}
    assert names == {"headcount", "geography"}


def test_partner_wording_is_never_discarded():
    industry = A.decompose_icp({"industries": ["Professional Services (broad)"]}).by_key(A.INDUSTRY)[0]
    assert industry.partner_term == "Professional Services (broad)"


def test_free_text_notes_are_kept_separate_from_enforced_atoms():
    # The exact confusion behind incident #4: notes are real information, but they are not a
    # filter, and must never be countable as one.
    decomposed = A.decompose_icp(MAJJI)
    assert decomposed.unstructured_notes == "No dedicated marketing hire."
    assert all(a.key != "notes" for a in decomposed.atoms)


def test_legacy_sales_team_size_and_new_department_map_produce_the_same_atom_shape():
    legacy = A.decompose_icp({"sales_team_size_min": 2, "sales_team_size_max": 3})
    modern = A.decompose_icp({"department_headcount": {"sales": {"min": 2, "max": 3}}})
    assert [(a.key, a.qualifier, a.value) for a in legacy.by_key(A.DEPARTMENT_HEADCOUNT)] == \
           [(a.key, a.qualifier, a.value) for a in modern.by_key(A.DEPARTMENT_HEADCOUNT)]


def test_icypeas_renders_its_own_documented_filter_shapes():
    coverage = R.coverage_for(R.ICYPEAS_FIND_COMPANIES, A.decompose_icp(MAJJI))
    assert coverage.filters["headcount"] == {">=": 11, "<=": 50}
    assert coverage.filters["location"] == {"include": ["United States"]}
    assert coverage.filters["industry"] == {"include": ["Professional Services"]}


def test_providers_express_the_same_concept_in_different_shapes():
    # Why a call site may never guess a field name: headcount is three different payloads.
    icp = A.decompose_icp({"employee_min": 11, "employee_max": 50})
    icypeas = R.coverage_for(R.ICYPEAS_FIND_COMPANIES, icp).filters
    prospeo = R.coverage_for(R.PROSPEO_SEARCH_COMPANY, icp).filters
    assert icypeas["headcount"] == {">=": 11, "<=": 50}
    assert prospeo["company_headcount_custom"] == {"min": 11, "max": 50}


def test_unsupported_is_three_state_not_boolean():
    """The design-draft error: concluding a market-wide absence from one provider.

    Icypeas genuinely has no department-headcount filter (verified against its documented filter
    set), but Apollo's is merely unverified by us -- which must trigger research, not a shrug.
    """
    icp = A.decompose_icp(MAJJI)
    icypeas = R.coverage_for(R.ICYPEAS_FIND_COMPANIES, icp)
    apollo = R.coverage_for(R.APOLLO_ORGANIZATION_SEARCH, icp)

    assert "department_headcount(marketing)" in [a.name for a in icypeas.unsupported]
    assert "department_headcount(marketing)" in [a.name for a in apollo.unverified]
    assert R.ICYPEAS_FIND_COMPANIES.capability(A.DEPARTMENT_HEADCOUNT).state == R.ABSENT
    assert R.APOLLO_ORGANIZATION_SEARCH.capability(A.DEPARTMENT_HEADCOUNT).state == R.UNVERIFIED


def test_a_ui_filter_is_not_an_api_contract():
    # Apollo's department headcount is documented in its product UI but its API parameter name is
    # unconfirmed, so it must not be renderable yet.
    cap = R.APOLLO_ORGANIZATION_SEARCH.capability(A.DEPARTMENT_HEADCOUNT)
    assert cap.render is None
    assert "not confirmed" in (cap.note or "").lower()


def test_an_unenforceable_must_have_is_reported_as_a_gap():
    gap = R.coverage_for(R.ICYPEAS_FIND_COMPANIES, A.decompose_icp(MAJJI)).must_have_gap
    assert [a.name for a in gap] == ["department_headcount(marketing)"]


def test_a_should_have_is_not_a_gap():
    # Titles are handled by the later decision-maker stage, so not filtering them at search time
    # must not disqualify an otherwise good route.
    coverage = R.coverage_for(R.ICYPEAS_FIND_COMPANIES, A.decompose_icp(MAJJI))
    assert "decision_maker_title" in [a.name for a in coverage.unsupported]
    assert "decision_maker_title" not in [a.name for a in coverage.must_have_gap]


def test_an_atom_the_provider_returns_is_checked_after_fetch_not_dropped():
    # Icypeas documents a revenue filter but we have never exercised it, so it stays unverified
    # AND falls to a free residual check, because Icypeas returns revenue on every row.
    coverage = R.coverage_for(R.ICYPEAS_FIND_COMPANIES, A.decompose_icp(MAJJI))
    assert "revenue" in [a.name for a in coverage.residual]
    assert "revenue" not in coverage.filters


def test_no_atom_is_ever_silently_dropped():
    """The invariant the whole phase exists for: every atom lands in exactly one bucket."""
    icp = A.decompose_icp(MAJJI)
    for endpoint in R.endpoints_for_job("company_search"):
        c = R.coverage_for(endpoint, icp)
        classified = c.enforced + c.residual + c.unverified + c.unsupported
        assert len(classified) == len(icp.atoms), f"{endpoint.provider} lost an atom"
        assert {a.name for a in classified} == {a.name for a in icp.atoms}
