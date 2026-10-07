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
