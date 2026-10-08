"""Phase 2: partner wording -> a provider's real filter and real values, for free, before spend.

Pins the $0.175 incident of 2026-10-05: "Professional Services" was sent as an Icypeas industry
enum, matched zero companies, and we only found out by paying for the empty page.
"""
import json

import pytest

from app.gtm_os.sourcing import atoms as A
from app.gtm_os.sourcing import registry as R
from app.gtm_os.sourcing import resolution as RES
from app.gtm_os.sourcing.models import IcpTermResolution, ProviderTaxonomyValue

PARTNER = 15
ICYPEAS = R.ICYPEAS_FIND_COMPANIES


@pytest.fixture
def db(db_factory):
    return db_factory([ProviderTaxonomyValue, IcpTermResolution])


def _industry_atom(term="Professional Services"):
    return A.Atom(A.INDUSTRY, A.INCLUDE, [term], partner_term=term)


def test_observed_values_are_learned_from_rows_we_already_paid_for(db):
    # Icypeas' published taxonomy 404s, so live rows are the only source of truth we have.
    RES.record_observed_values(db, "icypeas", A.INDUSTRY, ["Law Practice", "Insurance", None, ""])
    values = {v.value for v in RES.known_values(db, "icypeas", A.INDUSTRY)}
    assert values == {"Law Practice", "Insurance"}


def test_repeat_sightings_build_evidence_rather_than_duplicate_rows(db):
    RES.record_observed_values(db, "icypeas", A.INDUSTRY, ["Law Practice"])
    RES.record_observed_values(db, "icypeas", A.INDUSTRY, ["law practice"])   # same value, different case
    rows = RES.known_values(db, "icypeas", A.INDUSTRY)
    assert len(rows) == 1
    assert rows[0].observed_count == 2


def test_a_partner_term_that_is_a_real_value_resolves_exactly(db):
    RES.record_observed_values(db, "icypeas", A.INDUSTRY, ["Law Practice"])
    resolved = RES.resolve_atom(db, ICYPEAS, _industry_atom("law practice"), tenant_id=PARTNER)
    assert resolved.method == RES.METHOD_EXACT_TAXONOMY
    assert resolved.filter_fragment == {"industry": {"include": ["Law Practice"]}}


def test_unknown_term_falls_back_to_free_text_instead_of_an_enum_that_matches_nothing(db):
    """THE incident. With no confirmed value, the old code sent an industry enum and matched zero.

    Free text is weaker than a taxonomy filter, but it is the honest expression of a concept the
    provider has no value for -- and it costs nothing to find out.
    """
    resolved = RES.resolve_atom(db, ICYPEAS, _industry_atom(), tenant_id=PARTNER, use_llm=False)
    assert resolved.method == RES.METHOD_KEYWORD_FALLBACK
    assert resolved.filter_fragment == {"keyword": {"include": ["Professional Services"]}}
    assert "industry" not in resolved.filter_fragment


def test_multi_industry_partners_get_separate_keywords_not_one_joined_phrase(db):
    """Real bug, found 2026-10-07 by previewing EVERY partner rather than the one in front of me.

    partner_term is the partner's wording kept for provenance, and it is joined for display. Using
    it as the search value sent one comma-joined phrase as a single keyword, which matches nothing.
    The partner we had been testing with has exactly one industry, so his output looked correct
    while every multi-industry partner was broken.
    """
    atom = A.Atom(A.INDUSTRY, A.INCLUDE,
                  ["B2B technology", "SaaS", "cybersecurity"],
                  partner_term="B2B technology, SaaS, cybersecurity")
    resolved = RES.resolve_atom(db, ICYPEAS, atom, tenant_id=PARTNER, use_llm=False)
    assert resolved.filter_fragment == {
        "keyword": {"include": ["B2B technology", "SaaS", "cybersecurity"]}
    }


def test_a_broad_concept_expands_to_several_real_values(db, monkeypatch):
    import app.llm_client as llm

    RES.record_observed_values(db, "icypeas", A.INDUSTRY,
                               ["Law Practice", "Accounting", "Construction", "Insurance"])
    monkeypatch.setattr(llm, "generate_json",
                        lambda *a, **k: {"values": ["Law Practice", "Accounting"]})

    resolved = RES.resolve_atom(db, ICYPEAS, _industry_atom(), tenant_id=PARTNER)
    assert resolved.method == RES.METHOD_LLM_EXPANSION
    assert resolved.filter_fragment == {"industry": {"include": ["Law Practice", "Accounting"]}}


def test_the_llm_may_not_invent_a_value_the_provider_does_not_have(db, monkeypatch):
    """The whole point of resolution: an invented value is what matched zero companies."""
    import app.llm_client as llm

    RES.record_observed_values(db, "icypeas", A.INDUSTRY, ["Law Practice"])
    monkeypatch.setattr(llm, "generate_json",
                        lambda *a, **k: {"values": ["Law Practice", "Professional Services"]})

    resolved = RES.resolve_atom(db, ICYPEAS, _industry_atom(), tenant_id=PARTNER)
    # "Professional Services" is not in the provider's real value space, so it is discarded.
    assert resolved.values == ["Law Practice"]


def test_an_llm_failure_degrades_to_the_keyword_fallback_rather_than_crashing_a_paid_run(db, monkeypatch):
    import app.llm_client as llm

    RES.record_observed_values(db, "icypeas", A.INDUSTRY, ["Law Practice"])

    def boom(*a, **k):
        raise RuntimeError("quota exhausted")

    monkeypatch.setattr(llm, "generate_json", boom)
    resolved = RES.resolve_atom(db, ICYPEAS, _industry_atom(), tenant_id=PARTNER)
    assert resolved.method == RES.METHOD_KEYWORD_FALLBACK


def test_resolution_is_recorded_so_a_partner_can_see_how_their_words_were_read(db):
    RES.resolve_atom(db, ICYPEAS, _industry_atom(), tenant_id=PARTNER, use_llm=False)
    row = db.query(IcpTermResolution).one()
    assert (row.tenant_id, row.provider, row.partner_term) == (PARTNER, "icypeas", "Professional Services")
    assert row.method == RES.METHOD_KEYWORD_FALLBACK
    assert json.loads(row.resolved_values) == ["Professional Services"]
    assert row.target_filter == "keyword"


def test_a_numeric_filter_needs_no_vocabulary_mapping(db):
    resolved = RES.resolve_atom(db, ICYPEAS, A.Atom(A.HEADCOUNT, A.RANGE, (11, 50)), tenant_id=PARTNER)
    assert resolved.method == RES.METHOD_VERBATIM
    assert resolved.filter_fragment == {"headcount": {">=": 11, "<=": 50}}


def test_an_atom_the_provider_cannot_express_is_unresolved_not_keyword_stuffed(db):
    """Level 1 before level 2: department headcount is a real filter elsewhere, so it must route
    to another provider -- not get smuggled into Icypeas' keyword field as loose text."""
    atom = A.Atom(A.DEPARTMENT_HEADCOUNT, A.RANGE, (None, 0), qualifier="marketing",
                  partner_term="no dedicated marketing hire")
    resolved = RES.resolve_atom(db, ICYPEAS, atom, tenant_id=PARTNER)
    assert resolved.method == RES.METHOD_UNRESOLVED
    assert resolved.filter_fragment == {}


# -------------------------------------------------------------------------------------------
# resolver_fetch, 2026-10-08. Found onboarding Nora: this module's own docstring described
# calling the provider's free resolver as the FIRST, cheapest step, but no code ever actually
# did it for any provider -- Crustdata's industry filter came back empty because the real
# taxonomy ("Medical Device") doesn't match the partner's own wording ("medical devices"), and
# nothing had a chance to learn that for free before falling back to free text or an LLM.
# -------------------------------------------------------------------------------------------

FAKE_ENDPOINT_WITH_RESOLVER = R.ProviderEndpoint(
    provider="fake", endpoint="fake-search", job="company_search",
    capabilities={
        A.INDUSTRY: R.Capability(
            A.INDUSTRY, R.SUPPORTED, R.FIXED_TAXONOMY,
            render=lambda atom: {"industry": {"include": list(atom.value)}},
            # "medical device" (exact, singular) echoes back unchanged -- the simple case where
            # the partner's own term already matches a real value. "medical devices" (plural)
            # resolves to a DIFFERENT real string, which is the partial/LLM-expansion case
            # below, matching what was actually observed live against Crustdata.
            resolver_fetch=lambda query, limit: {"medical device": ["Medical Device"],
                                                  "medical devices": ["Medical Device"],
                                                  "life science": []}.get(query.lower(), []),
        ),
    },
)


def test_resolver_fetch_is_called_and_learns_a_new_value_for_free(db):
    # The term itself already normalizes to the learned value -- no LLM step needed, the
    # simple "resolver confirms the partner's own wording" case.
    atom = _industry_atom("medical device")
    resolved = RES.resolve_atom(db, FAKE_ENDPOINT_WITH_RESOLVER, atom, tenant_id=PARTNER)
    assert resolved.method == RES.METHOD_EXACT_TAXONOMY
    assert resolved.values == ["Medical Device"]
    learned = RES.known_values(db, "fake", A.INDUSTRY)
    assert len(learned) == 1
    assert learned[0].source == RES.SOURCE_RESOLVER


def test_resolver_fetch_learns_a_value_that_still_needs_llm_expansion_to_match(db, monkeypatch):
    # The real case observed live: "medical devices" (plural, the partner's actual wording)
    # resolves to "Medical Device" (singular) -- not a literal string match, so the resolver
    # alone cannot close it; the LLM step (already covered by existing tests, mocked here) is
    # what finishes the match using ONLY the value the resolver just taught it.
    import app.llm_client as llm

    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {"values": ["Medical Device"]})
    resolved = RES.resolve_atom(db, FAKE_ENDPOINT_WITH_RESOLVER, _industry_atom("medical devices"),
                                tenant_id=PARTNER)
    assert resolved.method == RES.METHOD_LLM_EXPANSION
    assert resolved.values == ["Medical Device"]
    assert RES.known_values(db, "fake", A.INDUSTRY)[0].source == RES.SOURCE_RESOLVER


def test_resolver_fetch_returning_nothing_falls_through_to_unresolved(db):
    # "life science" is deliberately wired to return [] in the fake resolver above -- the exact
    # real result observed live against Crustdata for this exact term.
    resolved = RES.resolve_atom(db, FAKE_ENDPOINT_WITH_RESOLVER, _industry_atom("life science"),
                                tenant_id=PARTNER, use_llm=False)
    assert resolved.method == RES.METHOD_UNRESOLVED
    assert resolved.filter_fragment == {}


def test_a_failing_resolver_never_blocks_resolution_it_only_skips_the_shortcut(db, monkeypatch):
    def broken(query, limit):
        raise RuntimeError("provider is down")

    broken_endpoint = R.ProviderEndpoint(
        provider="fake-broken", endpoint="x", job="company_search",
        capabilities={A.INDUSTRY: R.Capability(
            A.INDUSTRY, R.SUPPORTED, R.FIXED_TAXONOMY,
            render=lambda atom: {"industry": {"include": list(atom.value)}},
            resolver_fetch=broken)},
    )
    resolved = RES.resolve_atom(db, broken_endpoint, _industry_atom("anything"),
                                tenant_id=PARTNER, use_llm=False)
    assert resolved.method == RES.METHOD_UNRESOLVED  # degraded, not raised


def test_resolver_fetch_already_confirmed_terms_are_not_refetched(db):
    # If a term is ALREADY confirmed (from a prior observed row), the resolver must not be
    # called for it again -- it should only be dialed for terms still missing.
    calls = []

    def tracking(query, limit):
        calls.append(query)
        return []

    RES.record_observed_values(db, "fake-tracked", A.INDUSTRY, ["Medical Device"])
    endpoint = R.ProviderEndpoint(
        provider="fake-tracked", endpoint="x", job="company_search",
        capabilities={A.INDUSTRY: R.Capability(
            A.INDUSTRY, R.SUPPORTED, R.FIXED_TAXONOMY,
            render=lambda atom: {"industry": {"include": list(atom.value)}},
            resolver_fetch=tracking)},
    )
    RES.resolve_atom(db, endpoint, _industry_atom("medical device"), tenant_id=PARTNER)
    assert calls == []  # already confirmed -- the exact-match path returns before the resolver


FAKE_ENDPOINT_WITH_RESOLVER_AND_KEYWORD = R.ProviderEndpoint(
    provider="fake-mixed", endpoint="x", job="company_search",
    capabilities={
        A.INDUSTRY: R.Capability(
            A.INDUSTRY, R.SUPPORTED, R.FIXED_TAXONOMY,
            render=lambda atom: {"industry": {"include": list(atom.value)}},
            resolver_fetch=lambda query, limit: {"medical device": ["Medical Device"]}.get(query.lower(), []),
        ),
        "keyword": R.Capability("keyword", R.SUPPORTED, R.FREE_TEXT,
                                render=lambda atom: {"keyword": {"include": list(atom.value)}}),
    },
)


def test_mixed_resolution_combines_real_taxonomy_values_with_a_keyword_fallback(db):
    # One term the resolver confirms ("medical device"), one it confirms is absent ("life
    # science") -- real-world instruction, 2026-10-08: send the confirmed one as a structured
    # filter and the rejected one as free text, in the SAME call, rather than all-or-nothing.
    atom = A.Atom(A.INDUSTRY, A.INCLUDE, ["medical device", "life science"],
                 partner_term="medical device, life science")
    resolved = RES.resolve_atom(db, FAKE_ENDPOINT_WITH_RESOLVER_AND_KEYWORD, atom,
                                tenant_id=PARTNER, use_llm=False)
    assert resolved.method == RES.METHOD_MIXED
    assert resolved.filter_fragment["industry"] == {"include": ["Medical Device"]}
    assert resolved.filter_fragment["keyword"] == {"include": ["life science"]}


def test_mixed_resolution_ors_structured_and_keyword_never_ands_them(db):
    """THE real bug, found live 2026-10-08 against Crustdata: a naive merge concatenated the
    resolved taxonomy condition and the keyword fallback into the SAME top-level AND group,
    requiring a company to match BOTH its real industry classification AND independently match
    a keyword tag. It correctly resolved "medical devices"/"diagnostics" to real values and the
    live query still matched zero companies, because of this. Uses Crustdata's REAL registry
    entry (not a fake endpoint with separate dict keys, which doesn't exercise this) since the
    bug is specific to a provider whose structured and keyword filters share one `conditions`
    list -- combining them must produce ONE OR-group, never two ANDed conditions."""

    def fake_resolver(query, limit):
        return {"medical device": ["Medical Device"]}.get(query.lower(), [])

    crustdata_with_resolver = R.CRUSTDATA_V3_COMPANY_SEARCH
    # Capability is frozen -- build a throwaway endpoint reusing Crustdata's real render/keyword
    # functions instead of mutating the shared registry object.
    rigged = R.ProviderEndpoint(
        provider="crustdata-v3", endpoint="x", job="company_search",
        capabilities={
            A.INDUSTRY: R.Capability(A.INDUSTRY, R.SUPPORTED, R.FIXED_TAXONOMY,
                                     render=crustdata_with_resolver.capabilities[A.INDUSTRY].render,
                                     resolver_fetch=fake_resolver),
            "keyword": crustdata_with_resolver.capabilities["keyword"],
        },
    )
    atom = A.Atom(A.INDUSTRY, A.INCLUDE, ["medical device", "life science"],
                 partner_term="medical device, life science")
    resolved = RES.resolve_atom(db, rigged, atom, tenant_id=PARTNER, use_llm=False)
    assert resolved.method == RES.METHOD_MIXED

    conditions = resolved.filter_fragment["conditions"]
    assert len(conditions) == 1, "must be ONE combined OR-group, not two separate AND'd conditions"
    group = conditions[0]
    assert group["op"] == "or"

    def flatten(conds):
        for c in conds:
            if "field" in c:
                yield c
            elif "conditions" in c:
                yield from flatten(c["conditions"])

    leaves = list(flatten(group["conditions"]))
    assert {"basic_info.industries"} <= {c["field"] for c in leaves}
    assert any(c["field"] in ("taxonomy.categories", "taxonomy.professional_network_specialities")
              for c in leaves)


def test_mixed_resolution_never_fires_for_a_provider_with_no_resolver(db):
    # Icypeas has no resolver at all -- "not checked" must stay on the existing all-or-nothing
    # keyword path, never silently treated as "confirmed absent".
    resolved = RES.resolve_atom(db, ICYPEAS, _industry_atom("Professional Services"),
                                tenant_id=PARTNER, use_llm=False)
    assert resolved.method == RES.METHOD_KEYWORD_FALLBACK
    assert "industry" not in resolved.filter_fragment


def test_crustdata_resolver_fetch_parses_the_real_suggestions_shape(monkeypatch):
    """Pins the exact live response shape confirmed 2026-10-08:
    {"suggestions": [{"value": "Medical Device"}]}, via Deepline's own execute_tool wrapper."""
    import app.deepline_client as dc

    captured = {}

    def fake_execute_tool(tool_id, payload):
        captured["tool_id"] = tool_id
        captured["payload"] = payload
        return {"toolResponse": {"raw": {"suggestions": [{"value": "Medical Device"}]}}}

    monkeypatch.setattr(dc, "execute_tool", fake_execute_tool)
    fetch = R.CRUSTDATA_V3_COMPANY_SEARCH.capability(A.INDUSTRY).resolver_fetch
    result = fetch("medical devices", 10)
    assert result == ["Medical Device"]
    assert captured["tool_id"] == "crustdata_v3_company_search_autocomplete"
    assert captured["payload"] == {"field": "basic_info.industries", "query": "medical devices", "limit": 10}


def test_crustdata_resolver_fetch_degrades_to_empty_on_a_deepline_error(monkeypatch):
    import app.deepline_client as dc

    def fake_execute_tool(tool_id, payload):
        raise dc.DeeplineError("provider unavailable")

    monkeypatch.setattr(dc, "execute_tool", fake_execute_tool)
    fetch = R.CRUSTDATA_V3_COMPANY_SEARCH.capability(A.INDUSTRY).resolver_fetch
    assert fetch("anything", 10) == []


def test_live_filter_builder_uses_resolution_when_given_a_session(db):
    from app.gtm_os.plays.icp_filters import icypeas_filters_for_icp

    icp = {"employee_min": 11, "employee_max": 50, "industries": ["Professional Services"],
           "geographies": ["United States"]}

    without_db = icypeas_filters_for_icp(icp)
    assert without_db["industry"]["include"] == ["Professional Services"]   # unchanged legacy path

    with_db = icypeas_filters_for_icp(icp, db=db, tenant_id=PARTNER)
    assert with_db["keyword"] == {"include": ["Professional Services"]}
    assert "include" not in with_db["industry"]          # the enum that matched nothing is gone
    assert with_db["industry"]["exclude"]                # our own vendor policy still applies
