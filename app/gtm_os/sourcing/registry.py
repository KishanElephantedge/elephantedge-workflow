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
    # Free endpoint that resolves partner wording into this filter's real values, if the provider
    # has one. Prospeo does (/search-suggestions); Icypeas does not.
    resolver: str | None = None
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


REGISTRY: tuple[ProviderEndpoint, ...] = (
    ICYPEAS_FIND_COMPANIES,
    PROSPEO_SEARCH_COMPANY,
    APOLLO_ORGANIZATION_SEARCH,
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
    `industry.include` and `industry.exclude` must be able to coexist."""
    for key, value in fragment.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            target[key].update(value)
        else:
            target[key] = value
