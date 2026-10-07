"""An ICP decomposed into atoms -- one testable requirement each, independent of any provider.

WHY THIS EXISTS (2026-10-07). Account sourcing was hardcoded to one provider with one filter
builder, and that produced four real failures this month:

  1. The partner's own `industries` was never read, so a Professional Services ICP returned
     hospitals, law firms and construction companies.
  2. "Professional Services" matched zero companies because it is not a value in Icypeas'
     taxonomy -- discovered only after paying for an empty page.
  3. Government bodies and school districts passed a filter that was supposed to exclude them.
  4. "Marketing headcount < 1" was stored on the ICP and silently never enforced anywhere.

All four share one root cause: a requirement could disappear between the partner stating it and
the search running, with nothing in the code that had to account for it. An atom cannot
disappear. Every atom ends a run in exactly one state -- ENFORCED by the provider, RESIDUAL
(checked by us after fetch), or UNSUPPORTED (surfaced to a human) -- and anything else is a bug
that shows up as an atom with no disposition.

The partner's ORIGINAL wording rides along on every atom (`partner_term`). It is never discarded,
because the same words resolve differently against each provider's value space, so resolution has
to be re-runnable per provider rather than done once and baked in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Atom keys -- the canonical vocabulary. A provider's own field name never appears here; that
# mapping lives in the registry, per provider.
HEADCOUNT = "headcount"
REVENUE = "revenue"
GEOGRAPHY = "geography"
INDUSTRY = "industry"
DEPARTMENT_HEADCOUNT = "department_headcount"
DECISION_MAKER_TITLE = "decision_maker_title"
COMPANY_TYPE = "company_type"

RANGE = "range"
INCLUDE = "include"
EXCLUDE = "exclude"

MUST_HAVE = "must_have"
SHOULD_HAVE = "should_have"


@dataclass(frozen=True)
class Atom:
    """One testable requirement.

    `qualifier` scopes an atom that needs a sub-target -- department_headcount is meaningless
    without knowing WHICH department, so Majji's "no dedicated marketing hire" is
    Atom(DEPARTMENT_HEADCOUNT, RANGE, (None, 0), qualifier="marketing").
    """

    key: str
    operator: str
    value: Any
    necessity: str = MUST_HAVE
    qualifier: str | None = None
    partner_term: str | None = None

    @property
    def name(self) -> str:
        return f"{self.key}({self.qualifier})" if self.qualifier else self.key


@dataclass
class IcpAtoms:
    atoms: list[Atom] = field(default_factory=list)
    # Free text the partner wrote that is NOT expressible as an atom. Deliberately kept separate:
    # it is real information and still feeds the LLM qualifier, but it must never be mistaken for
    # an enforced filter -- that confusion is exactly how "marketing < 1" ended up enforced by
    # nothing while looking like it was configured.
    unstructured_notes: str | None = None

    def must_haves(self) -> list[Atom]:
        return [a for a in self.atoms if a.necessity == MUST_HAVE]

    def by_key(self, key: str) -> list[Atom]:
        return [a for a in self.atoms if a.key == key]


def _range(lo: Any, hi: Any) -> tuple[Any, Any] | None:
    return (lo, hi) if lo is not None or hi is not None else None


def decompose_icp(icp: dict) -> IcpAtoms:
    """A stored partner ICP -> atoms. Only what the partner actually stated becomes an atom; an
    absent field is NOT a requirement, so it produces no atom rather than a silent default.

    The one exception is geography, which defaults to United States -- the pre-existing behaviour
    of every search in this codebase. It is recorded as an atom (not a hidden default inside a
    filter builder) so it is visible and overridable like anything else.
    """
    atoms: list[Atom] = []

    headcount = _range(icp.get("employee_min"), icp.get("employee_max"))
    if headcount:
        atoms.append(Atom(HEADCOUNT, RANGE, headcount))

    revenue = _range(icp.get("revenue_min_usd"), icp.get("revenue_max_usd"))
    if revenue:
        atoms.append(Atom(REVENUE, RANGE, revenue))

    geographies = icp.get("geographies") or ["United States"]
    atoms.append(Atom(GEOGRAPHY, INCLUDE, list(geographies)))

    industries = icp.get("industries") or []
    if industries:
        # partner_term keeps the raw wording ("Professional Services (broad)") next to the value,
        # so resolution against each provider's real taxonomy can be attempted and re-attempted.
        atoms.append(Atom(INDUSTRY, INCLUDE, list(industries), partner_term=", ".join(industries)))

    # Department headcount, 2026-10-07. Previously only sales_team_size_min/max existed, which
    # could not express "marketing < 1" at all -- so Majji's primary qualifying signal lived in
    # free-text notes and was enforced by nothing. Both the legacy sales fields and the new
    # per-department map decompose into the same atom shape.
    sales_team = _range(icp.get("sales_team_size_min"), icp.get("sales_team_size_max"))
    if sales_team:
        atoms.append(Atom(DEPARTMENT_HEADCOUNT, RANGE, sales_team, qualifier="sales"))

    for department, bounds in (icp.get("department_headcount") or {}).items():
        bound_range = _range((bounds or {}).get("min"), (bounds or {}).get("max"))
        if bound_range:
            atoms.append(Atom(DEPARTMENT_HEADCOUNT, RANGE, bound_range, qualifier=department))

    titles = icp.get("decision_maker_titles") or []
    if titles:
        # should_have: a route that cannot filter titles at search time is still viable, because
        # decision-maker resolution is a separate, later stage that targets these titles anyway.
        atoms.append(Atom(DECISION_MAKER_TITLE, INCLUDE, list(titles), necessity=SHOULD_HAVE))

    return IcpAtoms(atoms=atoms, unstructured_notes=icp.get("notes"))


def fingerprint(icp: dict) -> str:
    """A stable identifier for one ICP's SHAPE, independent of which provider it is sent to.

    Used to group route_attempts and scorecards by "this same requirement set", so drift
    detection can ask "did this exact filter shape used to work and now doesn't" rather than
    only the coarser "is this provider healthy overall". Deliberately hashes the atoms, not the
    raw icp dict or the rendered provider filters: two differently-worded ICPs that decompose to
    the same atoms (same bounds, same necessity) are the same shape for this purpose, and the
    same atoms rendered differently by two providers must still be recognized as one shape.
    """
    import hashlib
    import json

    decomposed = decompose_icp(icp)
    canonical = sorted(
        (a.key, a.operator, a.necessity, a.qualifier, json.dumps(a.value, sort_keys=True, default=str))
        for a in decomposed.atoms
    )
    return hashlib.sha1(json.dumps(canonical).encode()).hexdigest()[:12]
