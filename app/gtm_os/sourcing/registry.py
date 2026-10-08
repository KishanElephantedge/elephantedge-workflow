"""What each provider can actually filter on, and exactly how it expresses it.

This is AUTHORED KNOWLEDGE, taken from each provider's own documentation or verified schema, and
it lives in code on purpose: it should be reviewed in a diff, not edited invisibly in a database
row. Runtime state that this file deliberately does NOT hold -- learned taxonomy values,
verification stamps, health, observed cost -- belongs in the database, because it changes without
anyone writing code.

THE RULE THIS FILE EXISTS TO ENFORCE (2026-10-07, after getting it wrong in a design draft):

    "Unsupported" is a registry-wide verdict, never an inference from the provider you happen to
    be looking at.

An earlier draft claimed no provider could filter on department headcount, concluded from the two
providers in front of me. Apollo, LinkedIn Sales Navigator and Crustdata all support it. So
support is a THREE-state fact per (provider, atom) -- never a boolean:

    SUPPORTED   the filter exists AND we know its exact expression
    ABSENT      verified not to exist for this provider
    UNVERIFIED  nobody has checked yet -> triggers research, never a silent skip

A filter seen in a provider's UI is UNVERIFIED here until the API parameter itself is confirmed,
because a product screenshot is not an API contract.

Each capability also carries a `render` function: the atom -> that provider's exact payload shape.
Shapes differ even where the concept is identical (Apollo takes "11,50" strings, Prospeo takes
{min,max} integers, Icypeas takes comparison operators), which is precisely why a filter name or
shape must never be guessed at a call site.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.gtm_os.sourcing import atoms as A

SUPPORTED = "supported"
ABSENT = "absent"
UNVERIFIED = "unverified"

FIXED_TAXONOMY = "fixed_taxonomy"
FREE_TEXT = "free_text"
NUMERIC = "numeric"
GEO = "geo"


@dataclass(frozen=True)
class Capability:
    """How one provider expresses one atom."""

    atom: str
    state: str
    value_space: str | None = None
    # Renders {atom -> provider payload fragment}. None whenever state != SUPPORTED.
    render: Callable[[A.Atom], dict] | None = None
    # Free endpoint NAME that resolves partner wording into this filter's real values, if the
    # provider has one. Prospeo does (/search-suggestions); Icypeas does not. Metadata only --
    # until `resolver_fetch` below, nothing in this codebase ever actually CALLED it.
    resolver: str | None = None
    # The CALLABLE that does it: (query_term, limit) -> list of real values this provider's own
    # free resolver suggests, or []. Added 2026-10-08 after finding `resolver` above had sat as
    # a name nobody dialed since this file's very first version -- resolve_atom() calls this
    # directly rather than re-deriving a provider's own input/output shape from a string.
    resolver_fetch: Callable[[str, int], list[str]] | None = None
    note: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class ProviderEndpoint:
    provider: str
    endpoint: str
    job: str
    capabilities: dict[str, Capability]
    count_endpoint: str | None = None          # free pre-flight, if offered
    cost_unit: str | None = None               # per_result | per_page | per_call
    cost_amount: float | None = None
    billed_on_miss: bool = True
    page_size_max: int | None = None
    identity_fields: tuple[str, ...] = ()
    source: str | None = None
    # Atoms this endpoint cannot filter but whose values it RETURNS, so they can be enforced for
    # free on our side after the fetch instead of costing a second provider call.
    returns_for_residual_check: tuple[str, ...] = ()
    # True when the provider cannot be called with Deepline's own managed credentials at all --
    # a separate blocker from "no adapter yet". Apollo is the case that forced this: Deepline's
    # own catalog marks apollo_company_search `requires_own_credential: true,
    # credentialStatus: "requires_connection"` -- it is fully specified and still cannot be
    # executed until a partner Apollo account exists, which is a business decision, not code.
    requires_own_credential: bool = False

    def capability(self, atom_key: str) -> Capability:
        return self.capabilities.get(atom_key) or Capability(atom=atom_key, state=UNVERIFIED)


# --------------------------------------------------------------------------------------------
# Icypeas -- find-companies. Verified against https://api-doc.icypeas.com/leads-db/find-companies/
# on 2026-10-07, and against real live responses from our own runs.
# --------------------------------------------------------------------------------------------

def _icypeas_headcount(atom: A.Atom) -> dict:
    lo, hi = atom.value
    bounds: dict[str, Any] = {}
    if lo is not None:
        bounds[">="] = lo
    if hi is not None:
        bounds["<="] = hi
    return {"headcount": bounds}


def _icypeas_revenue(atom: A.Atom) -> dict:
    lo, hi = atom.value
    bounds: dict[str, Any] = {}
    if lo is not None:
        bounds[">="] = lo
    if hi is not None:
        bounds["<="] = hi
    return {"revenue": bounds}


def _icypeas_geography(atom: A.Atom) -> dict:
    return {"location": {"include": list(atom.value)}}


def _icypeas_industry(atom: A.Atom) -> dict:
    return {"industry": {"include": list(atom.value)}}


def _icypeas_keyword(atom: A.Atom) -> dict:
    return {"keyword": {"include": list(atom.value)}}


ICYPEAS_FIND_COMPANIES = ProviderEndpoint(
    provider="icypeas",
    endpoint="find-companies",
    job="company_search",
    count_endpoint="icypeas_count_companies",   # free: "We do not charge anything when using this route."
    cost_unit="per_result",
    cost_amount=0.007,
    billed_on_miss=True,                        # billed on REQUESTED page size, not rows returned
    page_size_max=200,
    identity_fields=("linkedin_company_id", "domain", "name"),
    source="https://api-doc.icypeas.com/leads-db/find-companies/",
    returns_for_residual_check=(A.INDUSTRY, A.HEADCOUNT, A.REVENUE, A.COMPANY_TYPE),
    capabilities={
        A.HEADCOUNT: Capability(A.HEADCOUNT, SUPPORTED, NUMERIC, _icypeas_headcount),
        # Documented as a Range filter, but never exercised live by us. Left UNVERIFIED on
        # purpose: promoting it to SUPPORTED would silently add a filter to every live query,
        # and an untested filter that over-excludes (companies with unknown revenue) looks
        # exactly like an empty market. Because Icypeas RETURNS revenue on each row
        # (estimatedRevenuRange), it falls to a residual check instead -- enforced by us, for
        # free, on data we already pay for. Promote after a free count-endpoint comparison.
        A.REVENUE: Capability(A.REVENUE, UNVERIFIED, NUMERIC, None,
                              note="Documented range filter; verify with the free count endpoint "
                                   "before enforcing. Enforced as a residual check meanwhile."),
        A.GEOGRAPHY: Capability(A.GEOGRAPHY, SUPPORTED, GEO, _icypeas_geography),
        A.INDUSTRY: Capability(
            A.INDUSTRY, SUPPORTED, FIXED_TAXONOMY, _icypeas_industry,
            resolver=None,
            note="Fixed taxonomy. The published value list 404s as of 2026-10-05, so values are "
                 "learned from live responses and validated with the free count endpoint. The "
                 "free-text `keyword` filter is the fallback ONLY when a concept has no taxonomy "
                 "value, never as a shortcut past resolving one that does.",
        ),
        # Free-text, documented as an Include/Exclude filter. Registered as its own capability so
        # resolution can fall back to it ONLY when a concept has no structured filter -- never as
        # a shortcut past resolving one that does.
        "keyword": Capability("keyword", SUPPORTED, FREE_TEXT, _icypeas_keyword,
                              note="Free-text Include/Exclude over company text."),
        # Verified absent: the documented filter set is name, lid, urn, companyId, type, industry,
        # location, headcount, headcountGrowth, revenue, keyword, domain. No department headcount.
        A.DEPARTMENT_HEADCOUNT: Capability(
            A.DEPARTMENT_HEADCOUNT, ABSENT,
            note="Not in the documented filter set. Route elsewhere (Apollo) or enforce per row.",
            source="https://api-doc.icypeas.com/leads-db/find-companies/",
        ),
        # Company-level search; person titles are not a filter here.
        A.DECISION_MAKER_TITLE: Capability(
            A.DECISION_MAKER_TITLE, ABSENT,
            note="Company search. Titles are handled by the separate decision-maker stage.",
        ),
    },
)


# --------------------------------------------------------------------------------------------
# Prospeo -- /search-company. Schema from https://prospeo.io/api-docs/filters-documentation
# (2026-10-07). Not yet exercised live by us, so anything not explicitly in those docs stays
# UNVERIFIED rather than being assumed.
# --------------------------------------------------------------------------------------------

def _prospeo_headcount(atom: A.Atom) -> dict:
    lo, hi = atom.value
    bounds: dict[str, Any] = {}
    if lo is not None:
        bounds["min"] = lo
    if hi is not None:
        bounds["max"] = hi
    return {"company_headcount_custom": bounds}


def _prospeo_industry(atom: A.Atom) -> dict:
    return {"company_industry": {"include": list(atom.value)}}


def _prospeo_geography(atom: A.Atom) -> dict:
    return {"company_location_search": {"include": list(atom.value)}}


PROSPEO_SEARCH_COMPANY = ProviderEndpoint(
    provider="prospeo",
    endpoint="/search-company",
    job="company_search",
    count_endpoint=None,                        # not documented; UNVERIFIED, must be checked
    cost_unit="per_page",
    cost_amount=None,
    page_size_max=None,
    identity_fields=("domain", "linkedin_company_id", "name"),
    source="https://prospeo.io/api-docs/filters-documentation",
    capabilities={
        A.HEADCOUNT: Capability(A.HEADCOUNT, SUPPORTED, NUMERIC, _prospeo_headcount,
                                note="company_headcount_custom is mutually exclusive with "
                                     "company_headcount_range."),
        A.INDUSTRY: Capability(
            A.INDUSTRY, SUPPORTED, FIXED_TAXONOMY, _prospeo_industry,
            resolver="/search-suggestions",
            note="256 fixed values, and the resolver maps free text to them WITHOUT consuming "
                 "credits -- the cheapest correct resolution path we have anywhere.",
        ),
        A.GEOGRAPHY: Capability(A.GEOGRAPHY, SUPPORTED, GEO, _prospeo_geography,
                                resolver="/search-suggestions",
                                note="Values must come from Search Suggestions."),
        # Documented as {min,max} strings like "10M" -- a different value space from a raw integer,
        # so it stays UNVERIFIED until the exact encoding is confirmed rather than guessed.
        A.REVENUE: Capability(A.REVENUE, UNVERIFIED, None, None,
                              note="company_revenue takes {min,max} as range STRINGS ('10M'). "
                                   "Confirm the accepted vocabulary before rendering."),
        # Prospeo documents headcount growth BY DEPARTMENT; whether absolute department headcount
        # is filterable on company search is not established. Explicitly unverified.
        A.DEPARTMENT_HEADCOUNT: Capability(A.DEPARTMENT_HEADCOUNT, UNVERIFIED, None, None,
                                           note="Growth-by-department is documented; absolute "
                                                "department headcount is not confirmed."),
    },
)


# --------------------------------------------------------------------------------------------
# Apollo -- organization search. This is the route that can serve Majji's "marketing < 1".
# The product filter is documented ("# of employees by department ... enter a minimum or maximum
# number of employees, or create a range"), but the API PARAMETER NAME is not confirmed, so it is
# UNVERIFIED here by the rule above. Verifying it is the next research task, not a guess.
# --------------------------------------------------------------------------------------------

APOLLO_ORGANIZATION_SEARCH = ProviderEndpoint(
    provider="apollo",
    endpoint="/mixed_companies/search",
    job="company_search",
    count_endpoint=None,
    cost_unit="per_call",
    cost_amount=0.026,                          # Monid public catalog, 2026-10-07
    page_size_max=None,
    identity_fields=("domain", "linkedin_company_id", "name"),
    source="https://knowledge.apollo.io/hc/en-us/articles/4412665755661-Search-Filters-Overview",
    # Verified 2026-10-08 against Deepline's own tool catalog: apollo_company_search shows
    # connected: false, credentialStatus: "requires_connection", requiresOwnCredential: true,
    # with "Deepline cannot provide platform-managed Apollo access." This is a different kind of
    # gap than every other UNVERIFIED entry below -- those need research; this needs a partner
    # Apollo account and API key before any call can be made at all, managed or not.
    requires_own_credential=True,
    capabilities={
        A.HEADCOUNT: Capability(
            A.HEADCOUNT, UNVERIFIED, NUMERIC, None,
            note="organization_num_employees_ranges[] takes 'min,max' STRINGS. Confirm against "
                 "the live schema before rendering.",
        ),
        A.DEPARTMENT_HEADCOUNT: Capability(
            A.DEPARTMENT_HEADCOUNT, UNVERIFIED, NUMERIC, None,
            note="Product filter '# of employees by department' with min/max/range is documented. "
                 "API parameter name NOT confirmed -- a UI filter is not an API contract. This is "
                 "the capability that would let Majji's 'no dedicated marketing hire' be enforced "
                 "at search time instead of guessed by the qualifier.",
        ),
        A.REVENUE: Capability(A.REVENUE, UNVERIFIED, NUMERIC, None),
        A.GEOGRAPHY: Capability(A.GEOGRAPHY, UNVERIFIED, GEO, None),
        A.INDUSTRY: Capability(A.INDUSTRY, UNVERIFIED, FIXED_TAXONOMY, None),
    },
)


# --------------------------------------------------------------------------------------------
# Crustdata v3 -- company/search. Schema pulled whole from Deepline's own `tools describe
# crustdata_v3_company_search` on 2026-10-08 -- the declared contract, not documentation scraped
# separately, so every field below is the real accepted field name and operator set, not a guess.
#
# Every one of our current atom concepts maps onto a real indexed field here, including
# DEPARTMENT_HEADCOUNT via `roles.distribution.<function>` -- the one capability that triggered
# this whole survey (Majji's "no dedicated marketing hire"). The schema was checked for ALL
# atoms, not just that one, per the correction that this file exists to keep making: a provider
# gets ONE registry entry describing everything it can do, not one entry per feature we happen
# to be chasing that week.
#
# Deliberately NOT yet registered as atom capabilities, because no current ICP atom models them
# -- but real, confirmed-present fields on this endpoint for when/if an ICP needs them:
# funding.* (total/last-round amount, type, investors), headcount.growth_percent/absolute by
# period, followers.* (LinkedIn follower count and growth), technographics.technologies.*,
# basic_info.year_founded, basic_info.markets, revenue.acquisition_status,
# taxonomy.professional_network_specialities. Adding an atom for one of these later should start
# here, not with a new provider survey.
# --------------------------------------------------------------------------------------------

def _crustdata_cond(field_name: str, op: str, value: Any) -> dict:
    return {"conditions": [{"field": field_name, "type": op, "value": value}]}


def _crustdata_headcount(atom: A.Atom) -> dict:
    lo, hi = atom.value
    conds = []
    if lo is not None:
        conds.append({"field": "headcount.total", "type": "=>", "value": lo})
    if hi is not None:
        conds.append({"field": "headcount.total", "type": "=<", "value": hi})
    return {"conditions": conds}


def _crustdata_revenue(atom: A.Atom) -> dict:
    lo, hi = atom.value
    conds = []
    if lo is not None:
        conds.append({"field": "revenue.estimated.lower_bound_usd", "type": "=>", "value": lo})
    if hi is not None:
        conds.append({"field": "revenue.estimated.upper_bound_usd", "type": "=<", "value": hi})
    return {"conditions": conds}


def _crustdata_geography(atom: A.Atom) -> dict:
    return _crustdata_cond("locations.country", "in", list(atom.value))


def _crustdata_industry(atom: A.Atom) -> dict:
    return _crustdata_cond("basic_info.industries", "in", list(atom.value))


def _crustdata_resolver(field_name: str) -> Callable[[str, int], list[str]]:
    """Builds a resolver_fetch for one Crustdata field. Lazily imports execute_tool so this
    module stays side-effect-free to IMPORT (no network call happens just from building the
    registry) -- the I/O only happens when resolution.py actually calls the returned function.
    Verified live 2026-10-08: {"suggestions": [{"value": "..."}]}, confirmed free
    (billingSource: free) in Deepline's own catalog."""

    def fetch(query: str, limit: int = 10) -> list[str]:
        from app.deepline_client import DeeplineError, DeeplineSpendBlocked, execute_tool

        try:
            response = execute_tool("crustdata_v3_company_search_autocomplete",
                                    {"field": field_name, "query": query, "limit": limit})
        except (DeeplineSpendBlocked, DeeplineError):
            return []
        raw = (response.get("toolResponse") or {}).get("raw") or {}
        return [s.get("value") for s in (raw.get("suggestions") or []) if s.get("value")]

    return fetch


def _crustdata_company_type(atom: A.Atom) -> dict:
    return _crustdata_cond("basic_info.company_type", "=", atom.value)


def _crustdata_funding_recency(atom: A.Atom) -> dict:
    # atom.value is (None, max_days) -- "a round within the last N days" becomes
    # "last_fundraise_date on or after (today - N days)". A real date comparison, not a guess:
    # funding.last_fundraise_date is confirmed present with format=date in the schema.
    from datetime import datetime, timedelta

    _, max_days = atom.value
    if max_days is None:
        return {}
    threshold = (datetime.utcnow() - timedelta(days=max_days)).date().isoformat()
    return _crustdata_cond("funding.last_fundraise_date", "=>", threshold)


def _crustdata_technographics(atom: A.Atom) -> dict:
    op = "in" if atom.operator == A.INCLUDE else "not_in"
    return _crustdata_cond("technographics.technologies.name", op, list(atom.value))


# Crustdata's own `roles.distribution.<function>` vocabulary (the full set the schema accepts),
# mapped from the department names an ICP actually uses. Kept as its own table rather than
# inline so Dropleads' matching mapping below can be visibly compared against it -- the two
# providers use almost, but not quite, the same function taxonomy.
CRUSTDATA_DEPARTMENT_FIELDS: dict[str, str] = {
    "engineering": "roles.distribution.engineering",
    "sales": "roles.distribution.sales",
    "marketing": "roles.distribution.marketing",
    "operations": "roles.distribution.operations",
    "finance": "roles.distribution.finance",
    "hr": "roles.distribution.human_resources",
    "human_resources": "roles.distribution.human_resources",
    "product": "roles.distribution.product_management",
    "product_management": "roles.distribution.product_management",
    "customer_success": "roles.distribution.customer_success_and_support",
    "legal": "roles.distribution.legal",
    "it": "roles.distribution.information_technology",
    "information_technology": "roles.distribution.information_technology",
    "accounting": "roles.distribution.accounting",
    "administrative": "roles.distribution.administrative",
    "business_development": "roles.distribution.business_development",
    "consulting": "roles.distribution.consulting",
    "design": "roles.distribution.arts_and_design",
    "education": "roles.distribution.education",
    "healthcare": "roles.distribution.healthcare_services",
    "media": "roles.distribution.media_and_communication",
    "real_estate": "roles.distribution.real_estate",
    "research": "roles.distribution.research",
}


def _crustdata_department_headcount(atom: A.Atom) -> dict:
    field_name = CRUSTDATA_DEPARTMENT_FIELDS.get((atom.qualifier or "").lower())
    if field_name is None:
        return {}
    lo, hi = atom.value
    conds = []
    if lo is not None:
        conds.append({"field": field_name, "type": "=>", "value": lo})
    if hi is not None:
        conds.append({"field": field_name, "type": "=<", "value": hi})
    return {"conditions": conds}


CRUSTDATA_V3_COMPANY_SEARCH = ProviderEndpoint(
    provider="crustdata-v3",
    endpoint="crustdata_v3_company_search",
    job="company_search",
    count_endpoint=None,                        # none dedicated; provider's own pricing note:
                                                 # "Empty result pages are free" -- a limit=1 probe
                                                 # with no matches costs nothing, the closest thing
                                                 # to a free pre-check this endpoint has.
    cost_unit="per_result",
    cost_amount=0.002,                          # Deepline catalog, 2026-10-08: $0.002/returned row
    billed_on_miss=False,                       # priced on RETURNED rows, unlike Icypeas
    page_size_max=1000,
    identity_fields=("professional_network_url", "primary_domain", "name"),
    source="deepline tools describe crustdata_v3_company_search (2026-10-08)",
    capabilities={
        A.HEADCOUNT: Capability(A.HEADCOUNT, SUPPORTED, NUMERIC, _crustdata_headcount,
                                note="headcount.total, exact integer, full comparison operators."),
        A.REVENUE: Capability(A.REVENUE, SUPPORTED, NUMERIC, _crustdata_revenue,
                              note="revenue.estimated.{lower,upper}_bound_usd, exact USD integers."),
        A.GEOGRAPHY: Capability(A.GEOGRAPHY, SUPPORTED, GEO, _crustdata_geography,
                                note="locations.country via `in`. locations.headquarters/"
                                     "street_address are also filterable for finer geo if an "
                                     "atom ever needs city/state precision."),
        A.INDUSTRY: Capability(
            A.INDUSTRY, SUPPORTED, FIXED_TAXONOMY, _crustdata_industry,
            resolver="crustdata_v3_company_search_autocomplete",
            resolver_fetch=_crustdata_resolver("basic_info.industries"),
            note="basic_info.industries via `in`. Verified LIVE 2026-10-08 (not just the "
                 "provider's guidance text): the autocomplete tool does real fuzzy/substring "
                 "matching against Crustdata's actual taxonomy, not the partner's own wording --"
                 " 'medical devices' -> 'Medical Device', 'diagnostics' -> 'Medical and "
                 "Diagnostic Laboratories', but broad category phrases like 'life science', "
                 "'clinical', 'regulatory', 'lab technology' matched NOTHING. A partner's broad "
                 "qualitative description and a provider's crisp taxonomy are genuinely "
                 "different things -- resolving correctly does not guarantee a wide match.",
        ),
        A.COMPANY_TYPE: Capability(A.COMPANY_TYPE, SUPPORTED, FIXED_TAXONOMY,
                                   _crustdata_company_type,
                                   note="basic_info.company_type, exact match."),
        A.DEPARTMENT_HEADCOUNT: Capability(
            A.DEPARTMENT_HEADCOUNT, SUPPORTED, NUMERIC, _crustdata_department_headcount,
            note="roles.distribution.<function> -- per-function absolute headcount, full "
                 "comparison operators. This is the field that makes Majji's "
                 "'marketing headcount 0-0' enforceable at search time instead of only by the "
                 "free Jobo-leadership-list proxy. Schema-verified via Deepline's declared "
                 "contract on 2026-10-08; not yet exercised against a live response, so treat "
                 "the first real call as the confirmation step, same as any other first use.",
        ),
        # Company-level search; person titles are not a filter here, same as Icypeas.
        A.DECISION_MAKER_TITLE: Capability(
            A.DECISION_MAKER_TITLE, ABSENT,
            note="Company search. Titles are handled by the separate decision-maker stage.",
        ),
        # Added 2026-10-08, onboarding Nora. Both fields are confirmed present in the schema --
        # the difference between them is whether the VALUE SPACE is also confirmed.
        A.FUNDING_RECENCY: Capability(
            A.FUNDING_RECENCY, SUPPORTED, NUMERIC, _crustdata_funding_recency,
            note="funding.last_fundraise_date, format=date -- a plain date comparison, no "
                 "vocabulary ambiguity, so SUPPORTED despite being newly added.",
        ),
        A.FUNDING_STAGE: Capability(
            A.FUNDING_STAGE, UNVERIFIED, FIXED_TAXONOMY, None,
            note="funding.last_round_type exists in the schema, but its accepted value strings "
                 "(e.g. 'Series B' vs 'series_b') are not confirmed anywhere -- exactly the "
                 "'Professional Services' trap. Confirm via a live response before rendering.",
        ),
        A.TECHNOGRAPHICS: Capability(
            A.TECHNOGRAPHICS, SUPPORTED, FREE_TEXT, _crustdata_technographics,
            note="technographics.technologies.name via `in`/`not_in` -- both directions "
                 "confirmed in the operator set, so include AND exclude both render.",
        ),
        A.LEADERSHIP_CHANGE: Capability(
            A.LEADERSHIP_CHANGE, ABSENT,
            note="Company-level search; no person/role data of any kind in this schema.",
        ),
    },
)


# --------------------------------------------------------------------------------------------
# Dropleads -- search_people. NOT a company-search endpoint (job stays distinct from
# "company_search" on purpose so it is never picked up by planner.rank()'s main waterfall) --
# it is a free PERSON search whose filters happen to include company-level facts. Registered in
# full anyway because it is a real, verified, zero-cost source for exactly the check
# `compose.py`'s `department_presence()` already does by other means: "does this company have
# anyone in department X" is answerable here for $0, without the Jobo-leadership-list proxy.
# Schema pulled whole from Deepline's `tools describe dropleads_search_people` on 2026-10-08.
# --------------------------------------------------------------------------------------------

DROPLEADS_DEPARTMENT_FIELDS: dict[str, str] = {
    "engineering": "Engineering", "sales": "Sales", "marketing": "Marketing",
    "operations": "Operations", "finance": "Finance", "hr": "HR", "human_resources": "HR",
    "product": "Product", "product_management": "Product",
    "customer_success": "Customer Success", "legal": "Legal", "it": "IT",
    "information_technology": "IT",
}


def _dropleads_department_presence(atom: A.Atom) -> dict:
    """Not a headcount RANGE filter -- Dropleads has no numeric department-size filter. This
    renders a presence probe: `departments: [X]` + the known company domain returns >=1 row iff
    the company has anyone in that department. Composed with a real page_size=1 count, this
    answers the boolean 'marketing team exists' question for free -- enough for Majji's
    `0-0` bound, but NOT a substitute for Crustdata's real numeric filter if a bound like `0-5`
    is ever needed instead of `0-0`."""
    dept = DROPLEADS_DEPARTMENT_FIELDS.get((atom.qualifier or "").lower())
    return {"departments": [dept]} if dept else {}


def _dropleads_headcount(atom: A.Atom) -> dict:
    lo, hi = atom.value
    bounds: dict[str, Any] = {}
    if lo is not None:
        bounds["min"] = lo
    if hi is not None:
        bounds["max"] = hi
    return {"customEmployeeRange": bounds}


def _dropleads_revenue(atom: A.Atom) -> dict:
    lo, hi = atom.value
    bounds: dict[str, Any] = {}
    if lo is not None:
        bounds["min"] = lo
    if hi is not None:
        bounds["max"] = hi
    return {"revenueRange": bounds}


def _dropleads_geography(atom: A.Atom) -> dict:
    return {"organizationCountries": {"include": list(atom.value)}}


def _dropleads_industry(atom: A.Atom) -> dict:
    return {"industries": list(atom.value)}


def _dropleads_decision_maker_title(atom: A.Atom) -> dict:
    return {"jobTitles": list(atom.value)}


DROPLEADS_SEARCH_PEOPLE = ProviderEndpoint(
    provider="dropleads",
    endpoint="dropleads_search_people",
    job="department_presence_check",            # deliberately not "company_search"; see docstring
    count_endpoint="dropleads_search_people",    # page=1, limit=1 per the provider's own sizing tip
    cost_unit="per_call",
    cost_amount=0.0,                             # Deepline catalog: "Free"
    page_size_max=50,
    identity_fields=("companyDomain", "companyName"),
    source="deepline tools describe dropleads_search_people (2026-10-08)",
    capabilities={
        A.DEPARTMENT_HEADCOUNT: Capability(
            A.DEPARTMENT_HEADCOUNT, SUPPORTED, FIXED_TAXONOMY, _dropleads_department_presence,
            note="Presence only (>=1 person in department), not a numeric range -- see render "
                 "docstring. Fixed vocabulary: Engineering, Sales, Marketing, Operations, "
                 "Finance, HR, Product, Customer Success, Legal, IT.",
        ),
        A.HEADCOUNT: Capability(A.HEADCOUNT, SUPPORTED, NUMERIC, _dropleads_headcount,
                                note="customEmployeeRange {min,max}; employeeRanges (fixed "
                                     "buckets) also available if exact bounds aren't needed."),
        A.REVENUE: Capability(A.REVENUE, SUPPORTED, NUMERIC, _dropleads_revenue,
                              note="revenueRange {min,max}, raw USD numbers."),
        A.GEOGRAPHY: Capability(A.GEOGRAPHY, SUPPORTED, GEO, _dropleads_geography,
                                note="organizationCountries/States/Cities include/exclude -- "
                                     "company HQ location, distinct from the contact's own "
                                     "personalCountries/States/Cities (not modeled as an atom)."),
        A.INDUSTRY: Capability(
            A.INDUSTRY, SUPPORTED, FREE_TEXT, _dropleads_industry,
            note="Free-text against Dropleads' own taxonomy, not a confirmed fixed value list. "
                 "Provider's own guidance: 'keep values broad... if 0 results, broaden first and "
                 "iterate' -- closer to Icypeas' keyword fallback than to a resolved taxonomy.",
        ),
        A.DECISION_MAKER_TITLE: Capability(
            A.DECISION_MAKER_TITLE, SUPPORTED, FREE_TEXT, _dropleads_decision_maker_title,
            note="Substring OR match, e.g. 'Sales' matches 'VP of Sales'. jobTitlesExclude also "
                 "available. Multi-word values must be split into single words per the "
                 "provider's own documented keywords caveat -- the same shape as the "
                 "multi-industry keyword-join bug fixed elsewhere in this router.",
        ),
        A.COMPANY_TYPE: Capability(A.COMPANY_TYPE, ABSENT,
                                   note="No company-type field in the documented filter set."),
    },
)


# --------------------------------------------------------------------------------------------
# PredictLeads -- discover_companies. Schema pulled whole from Deepline on 2026-10-08. Genuinely
# thin: exactly two filters exist, full stop -- registered honestly rather than left out, so the
# planner can see it was checked and ruled out by evidence, not forgotten.
# --------------------------------------------------------------------------------------------

_PREDICTLEADS_SIZE_BUCKETS = ["1", "2-10", "11-50", "51-200", "201-500", "501-1000",
                              "1001-5000", "5001-10000", "10001+"]


def _predictleads_bucket_for(lo: Any, hi: Any) -> list[str]:
    """PredictLeads takes an enum of fixed size buckets, not a numeric range -- any bucket whose
    range overlaps [lo, hi] is included, since the provider has no finer granularity."""
    bounds = [(1, 1), (2, 10), (11, 50), (51, 200), (201, 500), (501, 1000),
              (1001, 5000), (5001, 10000), (10001, float("inf"))]
    lo = lo if lo is not None else 0
    hi = hi if hi is not None else float("inf")
    return [label for (blo, bhi), label in zip(bounds, _PREDICTLEADS_SIZE_BUCKETS)
            if blo <= hi and bhi >= lo]


def _predictleads_headcount(atom: A.Atom) -> dict:
    lo, hi = atom.value
    return {"sizes": _predictleads_bucket_for(lo, hi)}


def _predictleads_geography(atom: A.Atom) -> dict:
    # `location` is a single STRING, not an array -- only the first geography atom value is
    # usable per call. Multiple geographies need multiple calls, not a joined string.
    values = list(atom.value)
    return {"location": values[0]} if values else {}


PREDICTLEADS_DISCOVER_COMPANIES = ProviderEndpoint(
    provider="predictleads",
    endpoint="predictleads_discover_companies",
    job="company_search",
    count_endpoint=None,                        # `page` param returns a `meta.count`, but that's
                                                 # a priced call, not a dedicated free count route.
    cost_unit="per_result",
    cost_amount=None,                           # "Pricing unavailable" in Deepline's own catalog
    page_size_max=1000,
    identity_fields=("domain",),
    source="deepline tools describe predictleads_discover_companies (2026-10-08)",
    capabilities={
        A.HEADCOUNT: Capability(A.HEADCOUNT, SUPPORTED, FIXED_TAXONOMY, _predictleads_headcount,
                                note="Fixed size-bucket enum, not a numeric filter -- see bucket "
                                     "mapping. `sizes` is REQUIRED; this endpoint cannot be "
                                     "called with no headcount atom at all."),
        A.GEOGRAPHY: Capability(A.GEOGRAPHY, SUPPORTED, GEO, _predictleads_geography,
                                note="Single string, country or US-state name/abbreviation. "
                                     "REQUIRED. No city-level or multi-value geography."),
        A.INDUSTRY: Capability(A.INDUSTRY, ABSENT,
                               note="Confirmed: only `location` and `sizes` exist on this "
                                    "endpoint's schema. No industry filter at all."),
        A.REVENUE: Capability(A.REVENUE, ABSENT, note="Not in the schema."),
        A.DEPARTMENT_HEADCOUNT: Capability(A.DEPARTMENT_HEADCOUNT, ABSENT,
                                           note="Not in the schema."),
        A.COMPANY_TYPE: Capability(A.COMPANY_TYPE, ABSENT, note="Not in the schema."),
        A.DECISION_MAKER_TITLE: Capability(A.DECISION_MAKER_TITLE, ABSENT,
                                           note="Company search; no person fields at all."),
    },
)


# --------------------------------------------------------------------------------------------
# PeopleDataLabs -- company_search. Deliberately left almost entirely UNVERIFIED. Deepline's own
# `tools describe` does NOT enumerate PDL's field names the way it does for every provider
# above -- the input schema is just a generic `query` (Elasticsearch-style object) or `sql`
# string, with the field vocabulary living in PDL's OWN docs, not in anything Deepline disclosed.
# Registering a Capability as SUPPORTED here would mean guessing a PDL column name (e.g.
# `job_title_levels`, `summary.headcount`) from memory -- exactly the mistake this file's header
# rule exists to prevent. Correct next step if this provider is ever pursued: pull PDL's own
# company-schema reference, not infer one from this generic envelope. $0.10/result also makes it
# 50x Crustdata's cost, so it is not worth that research unless Crustdata's coverage has a gap.
# --------------------------------------------------------------------------------------------

PEOPLEDATALABS_COMPANY_SEARCH = ProviderEndpoint(
    provider="peopledatalabs",
    endpoint="peopledatalabs_company_search",
    job="company_search",
    count_endpoint=None,
    cost_unit="per_result",
    cost_amount=0.10,                           # Deepline catalog, 2026-10-08
    page_size_max=100,
    identity_fields=("website",),
    source="deepline tools describe peopledatalabs_company_search (2026-10-08) -- field "
           "vocabulary NOT disclosed by this schema; see module docstring above.",
    capabilities={
        A.HEADCOUNT: Capability(A.HEADCOUNT, UNVERIFIED,
                                note="PDL's own docs almost certainly have an employee-count "
                                     "field; name unconfirmed here."),
        A.REVENUE: Capability(A.REVENUE, UNVERIFIED),
        A.GEOGRAPHY: Capability(A.GEOGRAPHY, UNVERIFIED),
        A.INDUSTRY: Capability(A.INDUSTRY, UNVERIFIED),
        A.DEPARTMENT_HEADCOUNT: Capability(A.DEPARTMENT_HEADCOUNT, UNVERIFIED),
        A.COMPANY_TYPE: Capability(A.COMPANY_TYPE, UNVERIFIED),
        A.DECISION_MAKER_TITLE: Capability(A.DECISION_MAKER_TITLE, UNVERIFIED),
    },
)


# --------------------------------------------------------------------------------------------
# Forager -- person_role_search. Found researching Nora's "just raised money" / "just hired a
# CMO" readiness signals (2026-10-08). Schema pulled from Deepline's full jsonSchema, not just
# the input-field names -- which mattered: organization_employees_start/end and
# funding_event_date_featured_start/end are genuine range pairs, but role_position_start_date
# has NO matching _start/_end pair, so "started within the last N days" is NOT confirmed
# renderable despite the tool's description explicitly promising "time periods". A plausible
# guess (pass a {gte: date} object) was deliberately not coded -- that is the exact shape of
# mistake this file exists to prevent.
#
# NOT registered as job="company_search": it returns people (with linked org data), identity-
# keyed on a person the same way Dropleads is, so it must never be ranked by planner.rank()
# alongside true company-search endpoints.
# --------------------------------------------------------------------------------------------

# funding_types is a CONFIRMED fixed enum (read from the live jsonSchema, not guessed). Partner
# wording -> Forager's own vocabulary; extend as new partner phrasing is seen, never invent a
# value not in this list.
FORAGER_FUNDING_STAGE_MAP: dict[str, str] = {
    "pre-seed": "pre_seed", "seed": "seed",
    "series a": "series_a", "series b": "series_b", "series c": "series_c",
    "series d": "series_d", "series e": "series_e", "series f": "series_f",
    "series g": "series_g", "series h": "series_h", "series i": "series_i",
    "series j": "series_j",
    "pe-backed": "private_equity", "pe backed": "private_equity",
    "private equity": "private_equity",
    "post-ipo equity": "post_ipo_equity", "post-ipo debt": "post_ipo_debt",
    "corporate round": "corporate_round", "angel": "angel", "grant": "grant",
    "undisclosed": "undisclosed",
}


def _forager_headcount(atom: A.Atom) -> dict:
    lo, hi = atom.value
    out: dict[str, Any] = {}
    if lo is not None:
        out["organization_employees_start"] = lo
    if hi is not None:
        out["organization_employees_end"] = hi
    return out


def _forager_revenue(atom: A.Atom) -> dict:
    lo, hi = atom.value
    out: dict[str, Any] = {}
    if lo is not None:
        out["organization_revenue_start"] = lo
    if hi is not None:
        out["organization_revenue_end"] = hi
    return out


def _forager_funding_stage(atom: A.Atom) -> dict:
    mapped = [FORAGER_FUNDING_STAGE_MAP[v.lower()] for v in atom.value if v.lower() in FORAGER_FUNDING_STAGE_MAP]
    return {"funding_types": mapped} if mapped else {}


def _forager_funding_recency(atom: A.Atom) -> dict:
    from datetime import datetime, timedelta

    _, max_days = atom.value
    if max_days is None:
        return {}
    threshold = (datetime.utcnow() - timedelta(days=max_days)).date().isoformat()
    return {"funding_event_date_featured_start": threshold}


FORAGER_PERSON_ROLE_SEARCH = ProviderEndpoint(
    provider="forager",
    endpoint="forager_person_role_search",
    job="leadership_signal_search",             # deliberately not "company_search"; see docstring
    count_endpoint=None,
    cost_unit="per_page",
    cost_amount=0.032,                          # Deepline catalog, 2026-10-08
    page_size_max=None,
    identity_fields=("person_linkedin_public_identifiers", "organization_domains"),
    source="deepline tools describe forager_person_role_search (2026-10-08), full jsonSchema",
    capabilities={
        A.HEADCOUNT: Capability(A.HEADCOUNT, SUPPORTED, NUMERIC, _forager_headcount,
                                note="organization_employees_start/end, plain integers."),
        A.REVENUE: Capability(A.REVENUE, SUPPORTED, NUMERIC, _forager_revenue,
                              note="organization_revenue_start/end, plain integers."),
        A.FUNDING_STAGE: Capability(
            A.FUNDING_STAGE, SUPPORTED, FIXED_TAXONOMY, _forager_funding_stage,
            note="funding_types -- confirmed fixed enum read directly from the live jsonSchema "
                 "(seed, series_a..series_j, private_equity, etc). No resolver needed; the "
                 "vocabulary is closed and already known.",
        ),
        A.FUNDING_RECENCY: Capability(
            A.FUNDING_RECENCY, SUPPORTED, NUMERIC, _forager_funding_recency,
            note="funding_event_date_featured_start/end -- a genuine range pair, confirmed in "
                 "the jsonSchema (both format=date). This is the field that makes 'raised money "
                 "in the last 2 quarters' renderable, not just detectable after the fact.",
        ),
        A.LEADERSHIP_CHANGE: Capability(
            A.LEADERSHIP_CHANGE, UNVERIFIED, None, None,
            note="role_title (boolean text query) + role_is_current=true CAN find current "
                 "title-holders. But role_position_start_date has NO _start/_end range pair in "
                 "the schema -- unlike every other date field here, it is a single exact-match "
                 "date. There is no confirmed way to express 'started within the last N days' "
                 "without guessing an undocumented range-object format on that field. Needs a "
                 "live test call before promoting -- this is Nora's single most important "
                 "signal and it must not be rendered on a guess.",
        ),
        # Both require integer IDs from a separate lookup endpoint ("Industries lookup",
        # "Web technologies lookup", "Locations lookup") that Deepline's catalog mentions but
        # that we have not confirmed exists as a callable tool. Free-text values would 400.
        A.INDUSTRY: Capability(A.INDUSTRY, UNVERIFIED, None, None,
                               note="organization_industries wants integer IDs; lookup tool "
                                    "existence unconfirmed."),
        A.GEOGRAPHY: Capability(A.GEOGRAPHY, UNVERIFIED, None, None,
                                note="organization_locations wants integer IDs; lookup tool "
                                     "existence unconfirmed."),
        A.TECHNOGRAPHICS: Capability(A.TECHNOGRAPHICS, UNVERIFIED, None, None,
                                     note="organization_web_technologies wants integer IDs; "
                                          "lookup tool existence unconfirmed. Crustdata's "
                                          "free-text technographics field is the usable route "
                                          "for this atom today."),
        A.COMPANY_TYPE: Capability(A.COMPANY_TYPE, ABSENT, note="Not in the schema."),
        A.DEPARTMENT_HEADCOUNT: Capability(A.DEPARTMENT_HEADCOUNT, ABSENT,
                                           note="organization_employees_* sizes the whole "
                                                "company; no per-department breakdown field."),
    },
)


# --------------------------------------------------------------------------------------------
# PredictLeads -- bulk signal discovery. Two more endpoints from the same provider as
# predictleads_discover_companies, found researching Nora's funding/news signals. Both are
# DISCOVERY mode (bulk, not "give me this one company's events") but neither exposes a date
# filter at the discovery level, despite each being sorted "by updated date" -- the per-company
# lookup variants (predictleads_company_financing_events / _news_events) DO take
# first_seen_at_from/until, but that is a different endpoint with a different input schema; the
# discovery endpoints below genuinely lack it. Registered honestly as ABSENT on recency rather
# than assumed to inherit the per-company endpoint's date filter.
# --------------------------------------------------------------------------------------------

def _predictleads_signal_geography(atom: A.Atom) -> dict:
    values = list(atom.value)
    return {"company_location": values[0]} if values else {}


PREDICTLEADS_DISCOVER_FINANCING_EVENTS = ProviderEndpoint(
    provider="predictleads",
    endpoint="predictleads_discover_financing_events",
    job="funding_signal_discovery",
    count_endpoint=None,
    cost_unit="per_result",
    cost_amount=None,                           # "Pricing unavailable" in Deepline's own catalog
    page_size_max=None,
    identity_fields=("domain",),
    source="deepline tools describe predictleads_discover_financing_events (2026-10-08)",
    capabilities={
        A.GEOGRAPHY: Capability(A.GEOGRAPHY, SUPPORTED, GEO, _predictleads_signal_geography,
                                note="company_location, single string -- same one-value "
                                     "limitation as predictleads_discover_companies."),
        A.FUNDING_STAGE: Capability(
            A.FUNDING_STAGE, UNVERIFIED, FIXED_TAXONOMY, None,
            note="financing_types_normalized exists, but unlike Forager's funding_types, its "
                 "accepted enum values were not disclosed by Deepline's schema -- the "
                 "description only says 'Comma-separated normalized financing types', no list. "
                 "Use Forager's confirmed enum instead until this one is separately confirmed.",
        ),
        A.FUNDING_RECENCY: Capability(
            A.FUNDING_RECENCY, ABSENT,
            note="Confirmed absent at the DISCOVERY level: the only fields are "
                 "financing_types_normalized, company_location, page, limit -- sorted by "
                 "updated date, but not filterable by it. Forager's "
                 "funding_event_date_featured_start/end is the renderable route for this atom.",
        ),
    },
)

PREDICTLEADS_DISCOVER_NEWS_EVENTS = ProviderEndpoint(
    provider="predictleads",
    endpoint="predictleads_discover_news_events",
    job="news_signal_discovery",
    count_endpoint=None,
    cost_unit="per_result",
    cost_amount=None,
    page_size_max=None,
    identity_fields=("domain",),
    source="deepline tools describe predictleads_discover_news_events (2026-10-08)",
    capabilities={
        A.GEOGRAPHY: Capability(A.GEOGRAPHY, SUPPORTED, GEO, _predictleads_signal_geography,
                                note="company_location, single string, same caveat as above."),
        # `categories` (launches, hires, expansions, partnerships) is real and confirmed, but no
        # atom concept exists yet for "recent product launch" or "recent expansion" -- Nora's
        # "launched a new product but messaging hasn't caught up" is only HALF externally
        # checkable (the launch itself is; whether the messaging caught up is not, it requires
        # reading the company's own site and judging it, an LLM task not a filter). Deliberately
        # not inventing a PRODUCT_LAUNCH atom for the checkable half alone until a partner ICP
        # actually needs it -- same "build for a real requirement, not speculatively" rule as
        # everywhere else in this file. This endpoint is registered so the field is visible and
        # not re-discovered from scratch next time, not because it is wired to anything yet.
    },
)


# Firmable is deliberately NOT registered here. Its only real endpoints surveyed
# (firmable_company_lookup, and firmable_people_search/firmable_person_lookup, not yet probed)
# require an already-known identifier (domain, LinkedIn slug, ABN...) -- enrichment, not
# discovery. There is no filter-driven search endpoint to register a Capability set against.

REGISTRY: tuple[ProviderEndpoint, ...] = (
    ICYPEAS_FIND_COMPANIES,
    PROSPEO_SEARCH_COMPANY,
    APOLLO_ORGANIZATION_SEARCH,
    CRUSTDATA_V3_COMPANY_SEARCH,
    DROPLEADS_SEARCH_PEOPLE,
    PREDICTLEADS_DISCOVER_COMPANIES,
    PEOPLEDATALABS_COMPANY_SEARCH,
    FORAGER_PERSON_ROLE_SEARCH,
    PREDICTLEADS_DISCOVER_FINANCING_EVENTS,
    PREDICTLEADS_DISCOVER_NEWS_EVENTS,
)


def endpoints_for_job(job: str = "company_search") -> list[ProviderEndpoint]:
    return [e for e in REGISTRY if e.job == job]


def get_endpoint(provider: str, endpoint: str | None = None) -> ProviderEndpoint | None:
    for e in REGISTRY:
        if e.provider == provider and (endpoint is None or e.endpoint == endpoint):
            return e
    return None


@dataclass
class Coverage:
    """How one endpoint handles one ICP. Every atom lands in exactly one bucket."""

    provider: str
    endpoint: str
    enforced: list[A.Atom] = field(default_factory=list)      # provider filters it natively
    residual: list[A.Atom] = field(default_factory=list)      # we check it after fetch, for free
    unverified: list[A.Atom] = field(default_factory=list)    # needs research before use
    unsupported: list[A.Atom] = field(default_factory=list)   # verified absent here
    filters: dict = field(default_factory=dict)               # the rendered provider payload

    @property
    def must_have_gap(self) -> list[A.Atom]:
        """must_have atoms this endpoint can neither enforce nor let us check for free. A route
        with a non-empty gap is not viable alone -- it needs composition with another provider."""
        return [a for a in self.unverified + self.unsupported if a.necessity == A.MUST_HAVE]


def coverage_for(endpoint: ProviderEndpoint, icp_atoms: A.IcpAtoms) -> Coverage:
    """Classify every atom against one endpoint and render what it can enforce.

    Nothing is silently dropped: an atom the provider cannot filter is either RESIDUAL (the
    provider returns the value, so we enforce it ourselves for free after the fetch) or it is
    reported in `unverified` / `unsupported` for a human or the planner to act on.
    """
    result = Coverage(provider=endpoint.provider, endpoint=endpoint.endpoint)

    for atom in icp_atoms.atoms:
        cap = endpoint.capability(atom.key)
        if cap.state == SUPPORTED and cap.render is not None:
            result.enforced.append(atom)
            _merge(result.filters, cap.render(atom))
        elif atom.key in endpoint.returns_for_residual_check:
            result.residual.append(atom)
        elif cap.state == ABSENT:
            result.unsupported.append(atom)
        else:
            result.unverified.append(atom)

    return result


def _merge(target: dict, fragment: dict) -> None:
    """Merge a rendered fragment, combining sub-keys of the same filter rather than clobbering --
    `industry.include` and `industry.exclude` must be able to coexist. Crustdata's render
    functions each contribute one or more SearchConditions under the same `conditions` key, so
    list fragments are concatenated rather than overwritten -- the same non-clobbering rule,
    applied to a list-shaped filter instead of a dict-shaped one."""
    for key, value in fragment.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            target[key].update(value)
        elif key in target and isinstance(target[key], list) and isinstance(value, list):
            target[key].extend(value)
        else:
            target[key] = value
