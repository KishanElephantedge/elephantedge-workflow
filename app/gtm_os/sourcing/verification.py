"""Check what the provider actually returned against what we asked for.

WHY THIS EXISTS. A search can return 200 OK, with rows, and be completely wrong. On 2026-10-03 a
Professional Services ICP came back as 21 hospitals, law firms, construction and manufacturing
companies, plus a fire department and a school district -- and nothing noticed, because the only
signal we checked was "did the call succeed". A filter we believe is applied is not evidence; the
rows are.

So after the first page of any route, verify a sample against the requirements the route claimed
to satisfy. Below threshold, the route is wrong and the run stops and re-routes instead of buying
24 more pages of the same mistake.

THE ONE ASYMMETRY THAT MATTERS: missing data is never a violation. This repeats a lesson already
paid for -- BePresent's public page declared "2-10 employees" while its real headcount was 31, so
a check that treated an absent or stale value as a failure would have discarded a genuine match.
A row only counts against a requirement when the provider gave us a value AND that value clearly
breaks it. Everything else is `unverifiable`, reported honestly rather than scored either way.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.gtm_os.sourcing import atoms as A

# A route has to be clearly wrong, not marginally imperfect, before we throw away a working
# provider: real data is messy and a provider is allowed to be imprecise at the edges.
DEFAULT_MIN_MATCH_RATE = 0.7
# Never judge a route on one or two rows -- that is noise, not evidence.
DEFAULT_MIN_SAMPLE = 5


@dataclass
class SampleVerification:
    checked: int = 0
    matched: int = 0
    violations: dict[str, int] = field(default_factory=dict)
    unverifiable: dict[str, int] = field(default_factory=dict)
    conclusive: bool = False          # False when the sample was too small to judge

    @property
    def match_rate(self) -> float | None:
        return (self.matched / self.checked) if self.checked else None

    def passed(self, threshold: float = DEFAULT_MIN_MATCH_RATE) -> bool:
        """Not conclusive -> passes. Refusing to judge is not the same as failing, and a tiny
        first page must not be able to disable a working provider."""
        if not self.conclusive or self.match_rate is None:
            return True
        return self.match_rate >= threshold

    def summary(self) -> dict:
        return {
            "checked": self.checked,
            "matched": self.matched,
            "match_rate": round(self.match_rate, 3) if self.match_rate is not None else None,
            "conclusive": self.conclusive,
            "violations": self.violations,
            "unverifiable": self.unverifiable,
        }


def _headcount_of(row: dict) -> int | None:
    value = row.get("numberOfEmployees")
    return int(value) if isinstance(value, (int, float)) else None


def _revenue_of(row: dict) -> tuple[float | None, float | None]:
    revenue = row.get("estimatedRevenuRange") or {}
    low, high = revenue.get("estimatedMinRevenue") or {}, revenue.get("estimatedMaxRevenue") or {}
    unit = 1_000_000 if low.get("unit") == "MILLION" else 1
    lo = low.get("amount") * unit if isinstance(low.get("amount"), (int, float)) else None
    hi = high.get("amount") * unit if isinstance(high.get("amount"), (int, float)) else None
    return lo, hi


def _check_atom(atom: A.Atom, row: dict) -> bool | None:
    """True = satisfied, False = clearly violated, None = cannot tell from this row."""
    if atom.key == A.HEADCOUNT:
        actual = _headcount_of(row)
        if actual is None:
            return None
        lo, hi = atom.value
        if lo is not None and actual < lo:
            return False
        if hi is not None and actual > hi:
            return False
        return True

    if atom.key == A.REVENUE:
        lo_actual, hi_actual = _revenue_of(row)
        if lo_actual is None and hi_actual is None:
            return None
        lo, hi = atom.value
        # Ranges only have to OVERLAP. A company estimated at $1M-$5M genuinely can be inside a
        # $2.5M-$5M band, and rejecting it would discard a real match on an estimate's width.
        if hi is not None and lo_actual is not None and lo_actual > hi:
            return False
        if lo is not None and hi_actual is not None and hi_actual < lo:
            return False
        return True

    if atom.key == A.INDUSTRY:
        actual = (row.get("industry") or "").strip()
        if not actual:
            return None
        from app.gtm_os.sourcing.resolution import normalize

        wanted = {normalize(v) for v in (atom.value or [])}
        # Only conclusive when we searched by a real taxonomy value. If the search used the
        # free-text keyword fallback, the provider never promised a classification, so judging
        # its industry label against the partner's wording would manufacture false failures.
        return normalize(actual) in wanted if wanted else None

    return None


def verify_sample(rows: list[dict], icp_atoms: A.IcpAtoms, *,
                  checkable_atoms: list[A.Atom] | None = None,
                  min_sample: int = DEFAULT_MIN_SAMPLE) -> SampleVerification:
    """Score a page of provider rows against the ICP.

    `checkable_atoms` lets the caller restrict verification to what the route actually claimed --
    an atom the provider never filtered on (and that we did not resolve to real values) is not
    something the rows can be judged against.
    """
    result = SampleVerification()
    atoms = checkable_atoms if checkable_atoms is not None else icp_atoms.must_haves()
    if not rows or not atoms:
        return result

    for row in rows:
        verdicts = []
        for atom in atoms:
            verdict = _check_atom(atom, row)
            if verdict is None:
                result.unverifiable[atom.name] = result.unverifiable.get(atom.name, 0) + 1
            else:
                verdicts.append((atom, verdict))

        if not verdicts:
            continue                       # nothing checkable on this row; it scores neither way
        result.checked += 1
        if all(v for _, v in verdicts):
            result.matched += 1
        else:
            for atom, verdict in verdicts:
                if not verdict:
                    result.violations[atom.name] = result.violations.get(atom.name, 0) + 1

    result.conclusive = result.checked >= min_sample
    return result
