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


# -------------------------------------------------------------------------------------------
# 2026-10-08 survey: a provider gets ONE registry entry for its WHOLE schema, not one entry per
# capability we happened to be chasing. These pin the full Crustdata/Dropleads/PredictLeads
# coverage found that day, not just the department-headcount field that started the search.
# -------------------------------------------------------------------------------------------

def test_crustdata_expands_europe_into_real_countries_not_a_literal_value():
    # Found live 2026-10-08 (Nora): "Europe" is not a real locations.country value in Crustdata
    # (confirmed via its own autocomplete -- zero suggestions), so sending it literally silently
    # zeroed out her entire search. Region names must expand; real country names must not.
    coverage = R.coverage_for(R.CRUSTDATA_V3_COMPANY_SEARCH,
                              A.decompose_icp({"geographies": ["United States", "United Kingdom", "Europe"]}))
    countries = next(c for c in coverage.filters["conditions"]
                     if c["field"] == "locations.country")["value"]
    assert "Europe" not in countries
    assert "United States" in countries and "United Kingdom" in countries
    assert "Germany" in countries and "France" in countries


def test_crustdata_geography_expansion_never_duplicates_an_explicitly_listed_country():
    coverage = R.coverage_for(R.CRUSTDATA_V3_COMPANY_SEARCH,
                              A.decompose_icp({"geographies": ["Germany", "Europe"]}))
    countries = next(c for c in coverage.filters["conditions"]
                     if c["field"] == "locations.country")["value"]
    assert countries.count("Germany") == 1


def test_crustdata_enforces_every_majji_must_have_including_department_headcount():
    coverage = R.coverage_for(R.CRUSTDATA_V3_COMPANY_SEARCH, A.decompose_icp(MAJJI))
    assert coverage.must_have_gap == []
    enforced_names = {a.name for a in coverage.enforced}
    assert enforced_names == {
        "headcount", "revenue", "geography", "industry", "department_headcount(marketing)",
    }


def test_crustdata_department_headcount_renders_the_real_indexed_field():
    icp = A.decompose_icp({"department_headcount": {"marketing": {"min": 0, "max": 0}}})
    coverage = R.coverage_for(R.CRUSTDATA_V3_COMPANY_SEARCH, icp)
    assert {"field": "roles.distribution.marketing", "type": "=>", "value": 0} in \
        coverage.filters["conditions"]
    assert {"field": "roles.distribution.marketing", "type": "=<", "value": 0} in \
        coverage.filters["conditions"]


def test_crustdata_conditions_from_different_atoms_accumulate_not_clobber():
    # _merge's list-extend path: headcount and department_headcount each contribute conditions:
    # [...] fragments, and both sets of conditions must survive in the merged filter.
    icp = A.decompose_icp({"employee_min": 11, "employee_max": 50,
                           "department_headcount": {"marketing": {"max": 0}}})
    coverage = R.coverage_for(R.CRUSTDATA_V3_COMPANY_SEARCH, icp)
    fields = {c["field"] for c in coverage.filters["conditions"]}
    assert "headcount.total" in fields
    assert "roles.distribution.marketing" in fields


def test_dropleads_is_registered_but_not_in_the_company_search_waterfall():
    # It's a person search, not a company search -- real, verified, and useful to compose.py's
    # free department-presence check, but must never be ranked alongside real company-search
    # endpoints by planner.rank().
    assert R.DROPLEADS_SEARCH_PEOPLE.job != "company_search"
    assert "dropleads" not in {e.provider for e in R.endpoints_for_job("company_search")}


def test_dropleads_department_presence_is_a_probe_not_a_range_filter():
    icp = A.decompose_icp({"department_headcount": {"marketing": {"max": 0}}})
    coverage = R.coverage_for(R.DROPLEADS_SEARCH_PEOPLE, icp)
    assert coverage.filters["departments"] == ["Marketing"]


def test_predictleads_is_registered_honestly_as_thin_not_skipped():
    # The real schema has exactly two filters. Confirmed ABSENT, not left UNVERIFIED, because the
    # whole field list was read directly off the live schema -- there is nothing left to check.
    icp = A.decompose_icp(MAJJI)
    coverage = R.coverage_for(R.PREDICTLEADS_DISCOVER_COMPANIES, icp)
    assert {a.name for a in coverage.enforced} == {"headcount", "geography"}
    assert {a.name for a in coverage.unsupported} >= {"industry", "revenue",
                                                       "department_headcount(marketing)"}
    assert R.PREDICTLEADS_DISCOVER_COMPANIES.capability(A.INDUSTRY).state == R.ABSENT


def test_predictleads_geography_takes_one_string_not_a_list():
    coverage = R.coverage_for(R.PREDICTLEADS_DISCOVER_COMPANIES,
                              A.decompose_icp({"geographies": ["United States", "Canada"]}))
    assert coverage.filters["location"] == "United States"


def test_predictleads_headcount_maps_onto_its_fixed_buckets():
    coverage = R.coverage_for(R.PREDICTLEADS_DISCOVER_COMPANIES,
                              A.decompose_icp({"employee_min": 11, "employee_max": 50}))
    assert coverage.filters["sizes"] == ["11-50"]


def test_peopledatalabs_stays_unverified_everywhere_rather_than_guessing_field_names():
    # Deepline's own schema for this tool never disclosed PDL's real field vocabulary (just a
    # generic query/sql envelope) -- so nothing here may be promoted to SUPPORTED without reading
    # PDL's own docs first, the same rule that governs every other UNVERIFIED entry.
    coverage = R.coverage_for(R.PEOPLEDATALABS_COMPANY_SEARCH, A.decompose_icp(MAJJI))
    assert coverage.enforced == []
    assert coverage.filters == {}


def test_apollo_requires_its_own_credential_a_different_blocker_than_unverified():
    # Verified 2026-10-08 against Deepline's own catalog: apollo_company_search is fully outside
    # Deepline's managed credentials, unlike every other provider surveyed. This is a business
    # decision (get an Apollo account), not a research task -- a distinct flag from UNVERIFIED.
    assert R.APOLLO_ORGANIZATION_SEARCH.requires_own_credential is True
    assert R.CRUSTDATA_V3_COMPANY_SEARCH.requires_own_credential is False
    assert R.ICYPEAS_FIND_COMPANIES.requires_own_credential is False


# -------------------------------------------------------------------------------------------
# 2026-10-08, onboarding Nora (life-science positioning consultancy). Her ICP introduced four
# genuinely new atom concepts -- funding stage, funding recency, leadership-change, and
# technographics -- because her qualifying criteria are about company STATE AT A POINT IN TIME,
# not static firmographics. These pin both the new decomposition and the real coverage found
# researching them, including the one case where a tool's description promised something its
# actual schema does not confirm.
# -------------------------------------------------------------------------------------------

NORA = {
    "revenue_min_usd": 10_000_000, "revenue_max_usd": 100_000_000,
    "industries": ["life-science technology"],
    "geographies": ["United States", "United Kingdom"],
    "department_headcount": {"marketing": {"min": 4}},
    "funding_stages": ["Series B", "Series C"],
    "funding_recency_max_days": 180,
    "leadership_change": {"titles": ["CMO", "VP Marketing", "CRO"], "max_age_days": 180},
    "technologies": {"include": ["HubSpot", "Salesforce"], "exclude": ["none"]},
    "notes": "Buyers don't yet believe the problem can be solved.",
}


def test_nora_new_fields_all_decompose_into_atoms():
    names = {a.name for a in A.decompose_icp(NORA).atoms}
    assert "funding_stage" in names
    assert "funding_recency" in names
    assert "leadership_change" in names
    assert "technographics" in names
    # marketing >= 4 uses the SAME department_headcount atom Majji's marketing <= 0 uses --
    # a minimum and a maximum are both just bounds on one generic range, no special-casing.
    assert "department_headcount(marketing)" in names


def test_funding_recency_defaults_to_an_upper_bound_only():
    atom = A.decompose_icp({"funding_recency_max_days": 180}).by_key(A.FUNDING_RECENCY)[0]
    assert atom.value == (None, 180)
    assert atom.necessity == A.SHOULD_HAVE


def test_technographics_include_and_exclude_are_separate_atoms():
    atoms = A.decompose_icp(NORA).by_key(A.TECHNOGRAPHICS)
    ops = {a.operator: list(a.value) for a in atoms}
    assert ops[A.INCLUDE] == ["HubSpot", "Salesforce"]
    assert ops[A.EXCLUDE] == ["none"]


def test_forager_enforces_revenue_funding_stage_and_recency():
    # NORA's worksheet qualifies by revenue, not headcount -- no employee_min/max is set, so no
    # headcount atom exists to enforce; asserting it here would test a field Nora never stated.
    coverage = R.coverage_for(R.FORAGER_PERSON_ROLE_SEARCH, A.decompose_icp(NORA))
    enforced = {a.name for a in coverage.enforced}
    assert enforced >= {"revenue", "funding_stage", "funding_recency"}


def test_forager_headcount_renders_when_an_icp_actually_states_it():
    coverage = R.coverage_for(R.FORAGER_PERSON_ROLE_SEARCH,
                              A.decompose_icp({"employee_min": 50, "employee_max": 500}))
    assert coverage.filters["organization_employees_start"] == 50
    assert coverage.filters["organization_employees_end"] == 500


def test_forager_funding_stage_maps_partner_wording_to_the_confirmed_enum():
    coverage = R.coverage_for(R.FORAGER_PERSON_ROLE_SEARCH,
                              A.decompose_icp({"funding_stages": ["Series B", "Series C"]}))
    assert coverage.filters["funding_types"] == ["series_b", "series_c"]


def test_forager_leadership_change_stays_unverified_despite_the_tool_description():
    # The one real finding worth protecting: Forager's own description promises "time periods",
    # but role_position_start_date has no _start/_end range pair in the actual jsonSchema, unlike
    # every other date field on this endpoint. A description is not a schema, same rule as a UI
    # screenshot not being an API contract.
    cap = R.FORAGER_PERSON_ROLE_SEARCH.capability(A.LEADERSHIP_CHANGE)
    assert cap.state == R.UNVERIFIED
    assert cap.render is None
    assert "no _start/_end range pair" in (cap.note or "") or "exact-match" in (cap.note or "")


def test_forager_is_not_in_the_company_search_waterfall():
    assert R.FORAGER_PERSON_ROLE_SEARCH.job != "company_search"
    assert "forager" not in {e.provider for e in R.endpoints_for_job("company_search")}


def test_crustdata_now_also_covers_fundings_recency_and_technographics():
    # Fields that existed in Crustdata's schema all along (seen in the original survey) but had
    # no atom to map onto until Nora's ICP needed one -- confirming the earlier correction stuck:
    # register the WHOLE schema, use pieces of it as real atoms arrive, never re-survey per atom.
    coverage = R.coverage_for(R.CRUSTDATA_V3_COMPANY_SEARCH, A.decompose_icp(NORA))
    enforced = {a.name for a in coverage.enforced}
    assert "funding_recency" in enforced
    assert "technographics" in enforced
    assert R.CRUSTDATA_V3_COMPANY_SEARCH.capability(A.FUNDING_STAGE).state == R.UNVERIFIED
    assert R.CRUSTDATA_V3_COMPANY_SEARCH.capability(A.LEADERSHIP_CHANGE).state == R.ABSENT


def test_crustdata_technographics_uses_token_match_not_array_equality():
    # REAL FIX, 2026-10-08: `in` against an array-valued field (a company's tech stack) checks
    # whether the WHOLE field equals one of our values, which it never does -- confirmed by
    # comparing against Crustdata's own dashboard query, which uses `[.]` (token match) OR'd per
    # technology instead. This was the live bug behind a confirmed-empty search for a real ICP.
    coverage = R.coverage_for(R.CRUSTDATA_V3_COMPANY_SEARCH,
                              A.decompose_icp({"technologies": {"include": ["HubSpot", "Marketo"]}}))
    group = next(c for c in coverage.filters["conditions"] if "conditions" in c)
    assert group["op"] == "or"
    assert {c["type"] for c in group["conditions"]} == {"[.]"}
    assert {c["value"] for c in group["conditions"]} == {"HubSpot", "Marketo"}
    assert all(c["field"] == "technographics.technologies.name" for c in group["conditions"])


def test_predictleads_financing_discovery_confirms_recency_absent_not_inherited():
    # The per-company predictleads endpoint (not registered here) takes a date filter; the bulk
    # discovery endpoint does not, despite sharing a provider. One must never be assumed to carry
    # the other's capability.
    cap = R.PREDICTLEADS_DISCOVER_FINANCING_EVENTS.capability(A.FUNDING_RECENCY)
    assert cap.state == R.ABSENT


def test_no_atom_is_ever_silently_dropped_for_nora_either():
    icp = A.decompose_icp(NORA)
    for endpoint in R.endpoints_for_job("company_search"):
        c = R.coverage_for(endpoint, icp)
        classified = c.enforced + c.residual + c.unverified + c.unsupported
        assert len(classified) == len(icp.atoms), f"{endpoint.provider} lost an atom"
