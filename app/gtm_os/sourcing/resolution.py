"""Partner wording -> a provider's real filter and real values. Free, and before any spend.

TWO LEVELS, IN THIS ORDER. Collapsing them is what produced the $0.175 empty page on 2026-10-05.

  Level 1 -- WHICH FILTER in this tool expresses the partner's concept?
            Answered by registry.py. "no dedicated marketing hire" is a department-headcount
            filter on Apollo and nothing at all on Icypeas. It is NOT an Icypeas keyword search
            just because Icypeas happens to have a keyword field.

  Level 2 -- WHAT VALUE does that filter accept?
            Answered here, cheapest-and-most-authoritative first:
              1. the provider's own resolver endpoint      (free, authoritative)
              2. values we have already confirmed it uses  (provider_taxonomy_values)
              3. an LLM mapping, allowed to CHOOSE ONLY from those confirmed values
              4. the provider's free-text field -- and ONLY when the concept has no structured
                 filter, never as a shortcut past resolving one that does
              5. unresolved -> this provider cannot serve the atom; the planner looks elsewhere

Free-text being step 4 rather than step 1 is deliberate. A keyword search is weaker than a real
taxonomy filter: it matches description text rather than a classification, so it drifts. It is the right
answer only when the structured filter genuinely does not exist.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.gtm_os.sourcing import atoms as A
from app.gtm_os.sourcing import registry as R
from app.gtm_os.sourcing.models import IcpTermResolution, ProviderTaxonomyValue

# How a value set was arrived at. Recorded on every resolution so a surprising search result can
# be traced back to the decision that caused it.
METHOD_VERBATIM = "verbatim"              # free-text/numeric filter: the value needs no mapping
METHOD_EXACT_TAXONOMY = "exact_taxonomy"  # the partner's word IS a real value
METHOD_LLM_EXPANSION = "llm_expansion"    # mapped onto confirmed values by an LLM
METHOD_KEYWORD_FALLBACK = "keyword_fallback"
METHOD_UNRESOLVED = "unresolved"

SOURCE_OBSERVED = "observed_in_response"
SOURCE_RESOLVER = "resolver"
SOURCE_PUBLISHED = "published_list"


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


@dataclass
class Resolution:
    atom: A.Atom
    method: str
    values: list[str] = field(default_factory=list)
    target_filter: str | None = None      # which provider filter the values go into
    filter_fragment: dict = field(default_factory=dict)
    note: str | None = None

    @property
    def resolved(self) -> bool:
        return self.method != METHOD_UNRESOLVED


def record_observed_values(db: Session, provider: str, atom: str, values: list[str],
                           source: str = SOURCE_OBSERVED) -> int:
    """Learn a provider's real taxonomy -- from rows it actually returned (SOURCE_OBSERVED,
    the only reason we know Icypeas uses "Law Practice", "Facilities Settings" and "Insurance";
    its published list 404s) or from its own free resolver endpoint (SOURCE_RESOLVER, see
    resolve_atom() below). Either way, cheap: no LLM, and the resolver case is the only network
    call, which is free. Every call teaches us something about the value space, so the next
    resolution is better than the last.
    """
    learned = 0
    for raw in values:
        if not raw or not str(raw).strip():
            continue
        value = str(raw).strip()
        key = normalize(value)
        row = (db.query(ProviderTaxonomyValue)
               .filter(ProviderTaxonomyValue.provider == provider,
                       ProviderTaxonomyValue.atom == atom,
                       ProviderTaxonomyValue.normalized_value == key).first())
        if row is None:
            db.add(ProviderTaxonomyValue(provider=provider, atom=atom, value=value,
                                         normalized_value=key, source=source,
                                         observed_count=1))
            learned += 1
        else:
            row.observed_count = (row.observed_count or 0) + 1
            row.last_seen_at = datetime.utcnow()
    db.commit()
    return learned


def known_values(db: Session, provider: str, atom: str) -> list[ProviderTaxonomyValue]:
    return (db.query(ProviderTaxonomyValue)
            .filter(ProviderTaxonomyValue.provider == provider, ProviderTaxonomyValue.atom == atom)
            .order_by(ProviderTaxonomyValue.observed_count.desc()).all())


def resolve_atom(db: Session, endpoint: R.ProviderEndpoint, atom: A.Atom,
                 tenant_id: int | None = None, use_llm: bool = True) -> Resolution:
    """Resolve one atom against one provider endpoint. Never spends, never calls the search API."""
    cap = endpoint.capability(atom.key)

    # Level 1: does this provider express the concept at all?
    if cap.state != R.SUPPORTED or cap.render is None:
        keyword = _keyword_capability(endpoint)
        if keyword is not None and cap.state == R.ABSENT and atom.key == A.INDUSTRY:
            # The concept has no structured filter here, so free text is now the RIGHT answer
            # rather than a shortcut -- step 4, reached only because step 1 failed.
            return _keyword_resolution(db, endpoint, atom, keyword, tenant_id)
        return Resolution(atom=atom, method=METHOD_UNRESOLVED,
                          note=f"{endpoint.provider} has no usable filter for {atom.name} "
                               f"(state={cap.state})")

    # Level 2: a filter that takes no vocabulary needs no mapping.
    if cap.value_space in (R.NUMERIC, R.GEO) or cap.value_space == R.FREE_TEXT:
        return Resolution(atom=atom, method=METHOD_VERBATIM, values=_as_list(atom.value),
                          target_filter=_filter_name(cap.render(atom)),
                          filter_fragment=cap.render(atom))

    # Fixed taxonomy: the partner's words must become real values.
    terms = _as_list(atom.value)
    confirmed = known_values(db, endpoint.provider, atom.key)
    by_norm = {v.normalized_value: v.value for v in confirmed}

    exact = [by_norm[normalize(t)] for t in terms if normalize(t) in by_norm]
    if exact and len(exact) == len(terms):
        return _store(db, tenant_id, endpoint, atom,
                      Resolution(atom=atom, method=METHOD_EXACT_TAXONOMY, values=exact,
                                 target_filter=_filter_name(cap.render(atom)),
                                 filter_fragment=cap.render(_with_value(atom, exact))))

    # Level 2, step 1 -- THIS MODULE'S OWN DOCSTRING described this as the first, cheapest,
    # most authoritative step from day one, and it was never actually called for any provider
    # until now (found 2026-10-08, onboarding Nora: Crustdata's industry filter came back
    # empty, and tracing why showed `cap.resolver` was only ever metadata -- a name nobody
    # dialed). Call the provider's own free resolver for whatever terms aren't already
    # confirmed, so a second search for the SAME wording never pays the LLM-expansion cost (or
    # worse, matches nothing) when the provider could have just said "that's not a real value"
    # for free up front.
    if cap.resolver_fetch is not None:
        for term in terms:
            if normalize(term) in by_norm:
                continue
            try:
                suggestions = cap.resolver_fetch(term, 10)
            except Exception as e:  # noqa: BLE001 -- the resolver is an optimization, never a dependency
                logger.warning("resolver fetch skipped for %s/%s %r: %s: %s",
                               endpoint.provider, atom.key, term, type(e).__name__, e)
                continue
            if suggestions:
                record_observed_values(db, endpoint.provider, atom.key, suggestions,
                                       source=SOURCE_RESOLVER)
        confirmed = known_values(db, endpoint.provider, atom.key)
        by_norm = {v.normalized_value: v.value for v in confirmed}
        exact = [by_norm[normalize(t)] for t in terms if normalize(t) in by_norm]
        if exact and len(exact) == len(terms):
            return _store(db, tenant_id, endpoint, atom,
                          Resolution(atom=atom, method=METHOD_EXACT_TAXONOMY, values=exact,
                                     target_filter=_filter_name(cap.render(atom)),
                                     filter_fragment=cap.render(_with_value(atom, exact)),
                                     note="resolved via the provider's own free autocomplete"))

    if confirmed and use_llm:
        expanded = _llm_expand(db, atom, terms, [v.value for v in confirmed], endpoint.provider)
        if expanded:
            return _store(db, tenant_id, endpoint, atom,
                          Resolution(atom=atom, method=METHOD_LLM_EXPANSION, values=expanded,
                                     target_filter=_filter_name(cap.render(atom)),
                                     filter_fragment=cap.render(_with_value(atom, expanded)),
                                     note=f"mapped onto {len(confirmed)} confirmed values"))

    # We cannot name a real value for this taxonomy yet. Free text is better than a filter that
    # matches nothing -- but it is recorded as a fallback, not as a resolution.
    keyword = _keyword_capability(endpoint)
    if keyword is not None:
        return _keyword_resolution(db, endpoint, atom, keyword, tenant_id,
                                   note="no confirmed taxonomy value yet; the taxonomy is learned "
                                        "from live rows, so this improves as runs accumulate")

    return _store(db, tenant_id, endpoint, atom,
                  Resolution(atom=atom, method=METHOD_UNRESOLVED,
                             note="no confirmed taxonomy value and no free-text filter"))


def _keyword_capability(endpoint: R.ProviderEndpoint) -> R.Capability | None:
    cap = endpoint.capabilities.get("keyword")
    return cap if cap is not None and cap.state == R.SUPPORTED and cap.render is not None else None


def _keyword_resolution(db: Session, endpoint: R.ProviderEndpoint, atom: A.Atom,
                        keyword: R.Capability, tenant_id: int | None,
                        note: str | None = None) -> Resolution:
    # The atom's VALUE list, never partner_term. partner_term is the partner's wording kept for
    # provenance and is joined for display ("B2B technology, SaaS, software, ..."), so using it as
    # the search value sent one comma-joined phrase instead of nine separate keywords -- which
    # matches nothing. Found 2026-10-07 by running the preview across every partner tenant:
    # Majji has a single industry so his looked correct, and every multi-industry partner was
    # broken. A partner-specific test would never have caught it.
    terms = _as_list(atom.value)
    fragment = keyword.render(_with_value(atom, terms))
    return _store(db, tenant_id, endpoint, atom,
                  Resolution(atom=atom, method=METHOD_KEYWORD_FALLBACK, values=terms,
                             target_filter=_filter_name(fragment), filter_fragment=fragment,
                             note=note))


def _llm_expand(db: Session, atom: A.Atom, terms: list[str], confirmed: list[str],
                provider: str) -> list[str]:
    """Map a broad concept onto values the provider really has.

    The LLM may ONLY pick from `confirmed`. It cannot invent a value, because an invented value is
    exactly what matched zero companies. A broad concept expands to a SET -- "Professional
    Services" is not one category in any real taxonomy, it is several.
    """
    from app.llm_client import generate_json

    prompt = (
        f"A B2B sales partner described the companies they target as: {', '.join(terms)}\n\n"
        f"Below is the COMPLETE list of {atom.key} values that the data provider actually has. "
        f"Choose every value that genuinely belongs to what the partner described. Choose nothing "
        f"else. If none genuinely fit, return an empty list.\n\n"
        f"Available values:\n{json.dumps(confirmed)}\n\n"
        f'Return ONLY: {{"values": ["..."]}}'
    )
    try:
        verdict = generate_json(prompt, db, 2, max_tokens=800)
    except Exception:  # noqa: BLE001 -- resolution must degrade to the keyword fallback, not crash
        return []
    allowed = {normalize(v): v for v in confirmed}
    return [allowed[normalize(v)] for v in (verdict.get("values") or []) if normalize(v) in allowed]


def _store(db: Session, tenant_id: int | None, endpoint: R.ProviderEndpoint, atom: A.Atom,
           resolution: Resolution) -> Resolution:
    """Persist how this wording was read, so a partner can see and correct it."""
    if tenant_id is None or not atom.partner_term:
        return resolution
    row = (db.query(IcpTermResolution)
           .filter(IcpTermResolution.tenant_id == tenant_id,
                   IcpTermResolution.provider == endpoint.provider,
                   IcpTermResolution.atom == atom.key,
                   IcpTermResolution.partner_term == atom.partner_term).first())
    if row is None:
        row = IcpTermResolution(tenant_id=tenant_id, provider=endpoint.provider, atom=atom.key,
                                partner_term=atom.partner_term)
        db.add(row)
    row.resolved_values = json.dumps(resolution.values)
    row.target_filter = resolution.target_filter
    row.method = resolution.method
    row.resolved_at = datetime.utcnow()
    db.commit()
    return resolution


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]


def _with_value(atom: A.Atom, value: Any) -> A.Atom:
    return A.Atom(key=atom.key, operator=atom.operator, value=value, necessity=atom.necessity,
                  qualifier=atom.qualifier, partner_term=atom.partner_term)


def _filter_name(fragment: dict) -> str | None:
    return next(iter(fragment), None)
