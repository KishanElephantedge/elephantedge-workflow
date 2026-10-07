"""When no single provider covers the ICP: combine what we can buy with what we can check for free.

Two patterns, matched to what we can actually act on today:

    enrich-to-decide   the deciding atom isn't searchable anywhere, so search on what IS
                       searchable, then decide the rest per row from data already in hand
    intersect          two providers each cover different must-haves; run both, keep only
                       companies both found (identity-keyed), most selective first

WHY ENRICH-TO-DECIDE MATTERS RIGHT NOW. Majji's "no dedicated marketing hire" is a
department_headcount atom no registered provider can search (Icypeas: verified absent; Apollo:
documented in its UI but unverified as an API parameter -- see registry.py). It has been sitting in
free-text notes, enforced by nothing, since this session started.

But `_process_icypeas_company` already fetches a company's free Jobo leadership list for decision-
maker resolution -- that data is free and already in hand before this module is even called. Using
it to ALSO decide a department-presence atom costs nothing extra. This is the honest version of
"enforce it somehow": not a new paid integration, a second use of data already paid for (or free).

THE SAME ASYMMETRY AS VERIFICATION applies here, because it is the same underlying risk: Jobo's
leadership list is often empty or incomplete, and an empty list is NOT evidence that a department
doesn't exist -- it may just mean Jobo's index missed it. So this only ever returns a confident
verdict when the evidence is positive (a title WAS found), and returns "cannot tell" otherwise,
exactly like verify_sample()'s unverifiable bucket. A company is never rejected on an absence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.gtm_os.sourcing import atoms as A

# Keyword patterns per department, matched against a real person's title. Deliberately narrow and
# literal rather than a fuzzy/semantic match: a false POSITIVE here (deciding a department exists
# when it doesn't) wrongly rejects a real match, which is the more expensive mistake -- the
# company is gone from this run entirely, not just mis-scored.
_DEPARTMENT_TITLE_PATTERNS: dict[str, re.Pattern] = {
    "marketing": re.compile(r"\b(marketing|brand|demand\s*gen|growth marketing|content marketing)\b", re.IGNORECASE),
    "sales": re.compile(r"\b(sales|account executive|business development|revenue)\b", re.IGNORECASE),
    "engineering": re.compile(r"\b(engineer|engineering|developer|cto)\b", re.IGNORECASE),
    "product": re.compile(r"\b(product manager|head of product|cpo)\b", re.IGNORECASE),
}


@dataclass
class DecideResult:
    satisfied: bool | None     # None = cannot tell from the evidence in hand
    evidence: str | None = None


def department_presence(department: str, people: list[dict]) -> DecideResult:
    """Does any of these real people's titles indicate this department exists at the company?

    `people` is whatever free data is already in hand (Jobo leadership candidates today) --
    this function never fetches anything itself. An empty or title-less list returns "cannot
    tell", never "no" -- the same reasoning as BePresent's stale headcount: absence of evidence
    in a free, partial index is not evidence of absence at the real company.
    """
    pattern = _DEPARTMENT_TITLE_PATTERNS.get(department)
    if pattern is None or not people:
        return DecideResult(satisfied=None)
    for person in people:
        title = (person.get("title") or "").strip()
        if title and pattern.search(title):
            return DecideResult(satisfied=True, evidence=f"{person.get('name') or 'unnamed'}: {title!r}")
    # Titles were present but none matched -- still not proof of absence (Jobo's leadership list
    # is not the whole company), so "cannot tell" rather than a confident "no".
    return DecideResult(satisfied=None)


def enrich_to_decide(atom: A.Atom, people: list[dict]) -> DecideResult:
    """A department_headcount atom -> whether the evidence in hand satisfies it.

    Reads the atom's own bounds rather than assuming "max 0 means forbidden": an atom asking for
    department_headcount >= 1 (a real hire wanted) is satisfied by the OPPOSITE evidence from one
    asking for < 1 (no dedicated hire), and this function serves both without the caller needing
    to know which.
    """
    if atom.key != A.DEPARTMENT_HEADCOUNT or not atom.qualifier:
        return DecideResult(satisfied=None)
    lo, hi = atom.value
    forbids_presence = hi is not None and hi < 1
    found = department_presence(atom.qualifier, people)
    if found.satisfied is None:
        return found
    if forbids_presence:
        return DecideResult(satisfied=not found.satisfied, evidence=found.evidence)
    if lo is not None and lo >= 1:
        return DecideResult(satisfied=found.satisfied, evidence=found.evidence)
    return DecideResult(satisfied=None)


def intersect(rows_a: list[dict], rows_b: list[dict],
             identity_of: "callable[[dict], str | None]") -> list[dict]:
    """Companies both provider results agree exist, keyed by identity rather than name (name
    collisions across unrelated companies are a confirmed real failure mode in this codebase).

    Caller supplies `identity_of` rather than this module assuming a field name, because the two
    providers being intersected will not shape their rows identically -- that mapping belongs with
    whoever called both searches, not guessed here.

    Returns rows from `rows_a`, enriched with whatever `rows_b`'s matching row adds that `rows_a`'s
    does not already have -- the composition point: neither provider alone had every atom, so the
    merged row should carry what both of them separately knew.
    """
    by_id_b = {k: r for r in rows_b if (k := identity_of(r))}
    merged = []
    for row in rows_a:
        key = identity_of(row)
        if key is None or key not in by_id_b:
            continue
        combined = dict(row)
        for field, value in by_id_b[key].items():
            if combined.get(field) in (None, "", 0) and value not in (None, "", 0):
                combined[field] = value
        merged.append(combined)
    return merged
