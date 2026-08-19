"""Shared derivation of real, business-grounded short terms from this tenant's own ICP/offering/
business-context configuration -- extracted from app/gtm_os/intelligence/linkedin_search_config.py
(Step: LinkedIn Posts search sensing) so this exact, already-reviewed derivation logic can be
reused by a second consumer (app/gtm_os/content/topics.py's default topic derivation) instead of
being re-implemented. Both consumers need the same thing: real, atomic, lead-authored phrases
describing who has the problem and what's being sold -- never an invented keyword list.

Pure functions only -- no DB access, no Parameter reads/writes. Callers pass in already-fetched
config dicts (get_icp_config(), get_offering_config(), get_business_context())."""


def icp_topic_terms(icp_config: list[dict]) -> list[str]:
    """Real, lead-authored language only -- an ICP's own `name` (e.g. "Stuck in Sales") IS the
    lead's own short label for that ICP's pain point. Deliberately does NOT use
    `trigger_description` as a term -- those are full explanatory sentences (e.g. "No active GTM
    engineer or Head-of-Sales hiring detected -- consistent with a team that has plateaued..."),
    real and useful for humans reading the config, but not a phrase anyone would type into a
    search box or that would appear verbatim in an article -- using it as-is would match against
    the sentence itself, not the concept it describes."""
    return [icp["name"] for icp in icp_config if icp.get("name")]


def offering_topic_terms(offering_config: list[dict]) -> list[str]:
    """Real offering names only -- e.g. "Consulting", "Execution" -- prefixed with "sales" for
    topical grounding (these names alone are too generic to search/match on; every real offering
    in this tenant's config is sales-GTM-related, confirmed by business_context.py's own seed
    data, so this single, explicit prefix is a grounding fact, not an invented business rule).
    Skips the prefix when the name already starts with "sales" (e.g. "Sales OS") to avoid
    "sales sales os"."""
    terms = []
    for o in offering_config:
        name = o.get("name")
        if not name:
            continue
        terms.append(name.lower() if name.lower().startswith("sales") else f"sales {name.lower()}")
    return terms


def business_context_topic_terms(business_context: dict) -> list[str]:
    """Real, lead-authored icp_bands `profile` strings (e.g. "Companies stuck in sales",
    "Looking for FDEs in Sales/GTM") -- the lead's own short description of who each band is,
    reused verbatim, never rewritten."""
    terms = []
    for band in business_context.get("icp_bands") or []:
        if band.get("profile"):
            terms.append(band["profile"])
    return terms


def all_business_topic_terms(icp_config: list[dict], offering_config: list[dict], business_context: dict) -> list[str]:
    """Combines all three real sources, deduped (case-insensitive, first-seen order preserved) --
    config can genuinely repeat a term (e.g. an offering name also appearing in an ICP
    description)."""
    terms = (
        icp_topic_terms(icp_config)
        + offering_topic_terms(offering_config)
        + business_context_topic_terms(business_context)
    )
    seen = set()
    deduped = []
    for term in terms:
        key = term.strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(term.strip())
    return deduped
