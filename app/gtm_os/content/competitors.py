"""Content Intelligence competitor configuration (2026-08-28) -- the "40%" leg of the content
mix: real, named competitors whose own content (blog posts, articles) gets searched under the
same keywords as topics.py's trend leg, so a strong competitor angle can inform what Elephant
Edge writes about next.

Deliberately SEPARATE from topics.py/feeds.py, same reasoning as feeds.py's own separation from
topics.py: a competitor is WHO we watch, independent of WHAT we care about (topics) or WHERE a
generic feed lives (feeds.py). Same Parameter storage pattern, same get/set/validate discipline,
no new mechanism invented.

DEFAULT_COMPETITORS is seeded with the 9 real, already-documented competitors from Elephant
Edge's own "us vs. them" comparison pages (company-understanding.md Step 6b), with real domains
confirmed via a live web search (2026-08-28) -- not guessed. Editable from here on; this is only
the first-run default, same convention as topics.py's own ICP-derived default."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

COMPETITORS_PARAMETER_KEY = "content_intelligence_competitors"

# Matches content_business_context.py's own constant -- duplicated rather than imported, same
# reasoning (content/ -> routes/ import direction stays one-way; this module has no reason to
# import that one either, so it's just restated here).
ELEPHANT_EDGE_TENANT_ID = 2

# REAL BUG, FOUND AND FIXED 2026-10-12: get_content_competitors() used to fall back to this list
# for ANY tenant with no override, not just Elephant Edge -- confirmed live, only 2 of the then-
# enabled partner tenants had ever actually overridden it, so every other partner's
# "competitor-reactive" content track was silently searching Elephant Edge's own 9 competitors'
# sites (Aexus, Skaled, Chief Outsiders, ...), never a competitor relevant to that partner's own
# business. This list is now gated to tenant_id == ELEPHANT_EDGE_TENANT_ID specifically, same
# "never fabricate for a business we don't know anything about" discipline
# content_business_context.py already applies to business_name/positioning/audience -- any other
# tenant with no override now gets a real empty list, not someone else's competitors.
DEFAULT_COMPETITORS: list[dict] = [
    {"name": "Aexus", "domain": "aexus.com", "enabled": True},
    {"name": "Activated Scale", "domain": "activatedscale.com", "enabled": True},
    {"name": "Skaled", "domain": "skaled.com", "enabled": True},
    {"name": "Go-to-Market Alliance", "domain": "gotomarketalliance.com", "enabled": True},
    # Elephant Edge's own "GTM OS Certified" credential comes from this org -- possibly more a
    # certifying body/community than a direct competitor. Kept enabled by default (still real,
    # relevant GTM content), flagged here rather than silently excluded or silently included.
    {"name": "GTM Partners", "domain": "gtmpartners.com", "enabled": True},
    {"name": "Chief Outsiders", "domain": "chiefoutsiders.com", "enabled": True},
    # A solo newsletter/Substack (Alexander Estner), not a company offering fractional sales --
    # a different kind of source than the other 8, but still real, relevant GTM content under the
    # same keywords. Kept enabled by default, flagged here rather than silently excluded.
    {"name": "MRR Unlocked", "domain": "mrrunlocked.com", "enabled": True},
    {"name": "The Sales Collective", "domain": "thesalescollective.com", "enabled": True},
    {"name": "Sales Xceleration", "domain": "salesxceleration.com", "enabled": True},
]


class CompetitorConfigError(ValueError):
    """Raised when competitor configuration fails validation -- never silently coerced or dropped."""


def _validate_competitors(competitors: list[dict]) -> None:
    if not isinstance(competitors, list):
        raise CompetitorConfigError("competitors must be a list")

    seen_domains: set[str] = set()
    for i, competitor in enumerate(competitors):
        if not isinstance(competitor, dict):
            raise CompetitorConfigError(f"competitor at index {i} must be an object")

        domain = competitor.get("domain")
        if not isinstance(domain, str) or not domain.strip():
            raise CompetitorConfigError(f"competitor at index {i} has an empty or missing domain")
        normalized_domain = domain.strip().lower()
        if normalized_domain in seen_domains:
            raise CompetitorConfigError(f"duplicate competitor domain: {domain!r}")
        seen_domains.add(normalized_domain)

        name = competitor.get("name")
        if not isinstance(name, str) or not name.strip():
            raise CompetitorConfigError(f"competitor {domain!r} has an empty or missing name")

        enabled = competitor.get("enabled", True)
        if not isinstance(enabled, bool):
            raise CompetitorConfigError(f"competitor {domain!r} has a malformed 'enabled' value -- must be true/false")

        # Optional -- a competitor's own LinkedIn company page, used by
        # sense_competitor_linkedin_posts (sensing.py) as a second real competitor-content
        # channel alongside the site: Google search above (a competitor's LinkedIn posts often
        # aren't indexed well enough for that search to catch). Absent is a real, valid state
        # (that channel simply has nothing to do for this competitor), never defaulted/guessed.
        linkedin_url = competitor.get("linkedin_url")
        if linkedin_url is not None and (not isinstance(linkedin_url, str) or not linkedin_url.strip()):
            raise CompetitorConfigError(f"competitor {domain!r} has a malformed 'linkedin_url' value -- must be a non-empty string or omitted")


def _normalize_competitor(competitor: dict) -> dict:
    normalized = {
        "name": competitor["name"].strip(),
        "domain": competitor["domain"].strip().lower(),
        "enabled": competitor.get("enabled", True),
    }
    linkedin_url = competitor.get("linkedin_url")
    if linkedin_url:
        normalized["linkedin_url"] = linkedin_url.strip()
    return normalized


def get_content_competitors(db: Session, tenant_id: int) -> list[dict]:
    """REAL FIX (2026-10-14): a partner tenant used to just get [] forever -- the competitor
    angle was the one content origin in this platform with no self-discovery at all, unlike
    topics.py's own derive_topics_from_content_business_context (the exact architectural gap a
    partner-side question surfaced live: "why should competitors be something a human has to
    type in, shouldn't the system find its own competitors from the partner's own context?").
    Now: no override -> derive from the partner's own real content_business_context (same
    "compute fresh from live config, cache once a human/LLM derivation has run" pattern topics.py
    already uses), SAVED so this never re-runs (re-deriving is a real Google CSE query + LLM
    call, not free). A tenant with no business_context configured yet still gets a real empty
    list -- never Elephant Edge's own competitors, never an invented one."""
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == COMPETITORS_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, list) and param.value:
        return param.value
    if tenant_id == ELEPHANT_EDGE_TENANT_ID:
        return DEFAULT_COMPETITORS

    from app.gtm_os.content.content_business_context import get_content_business_context

    business_context = get_content_business_context(db, tenant_id)
    if not business_context.get("business_name"):
        return []

    derived = derive_competitors_from_content_business_context(db, tenant_id, business_context)
    if derived:
        set_content_competitors(db, tenant_id, derived)
    return derived


COMPETITOR_EXTRACTION_PROMPT = """A business has described its own real positioning and audience \
below. Below that is a REAL, LIVE set of Google search results for companies that compete with or \
offer a similar service to this business -- not invented, actually returned by a real search.

Business name: {business_name}
Real positioning (their own words): {positioning}
Real audience (their own words): {audience}

Real search results:
{results_block}

Your job: from ONLY the real companies that actually appear in the search results above, identify \
which ones are genuine competitors or close alternatives to this business -- a real company \
offering a similar service to a similar audience, not a generic article, directory, or unrelated \
result.

CRITICAL RULES:
- Only name a company that is ACTUALLY PRESENT in the search results above, by its real name and \
real domain as shown in the result URL. Never invent a company, name, or domain not present above.
- Skip results that are articles, directories, listicles, or not actually a competing company's \
own site.
- If genuinely none of the results are a real competitor, return an empty list -- never pad with \
an invented one.

Return strict JSON: {{"competitors": [{{"name": "<real company name>", "domain": "<real domain from \
the result URL>"}}, ...]}}, at most 8."""


def derive_competitors_from_content_business_context(db: Session, tenant_id: int, business_context: dict) -> list[dict]:
    """One real, grounded search + LLM extraction -- only reached when nothing is cached yet (see
    get_content_competitors() above, which saves the result so this never re-runs for the same
    tenant). Unlike topics.py's derivation, this can't ground purely in the partner's own
    positioning text -- a positioning paragraph essentially never names real competitor
    companies, so an LLM asked to extract from text alone would have to invent names, which this
    codebase's own standing discipline (see this module's own file docstring, topics.py's
    derive_default_topics) explicitly forbids. Instead: one real Google search for competitors/
    alternatives to this business, then the LLM may only name a company that's ACTUALLY PRESENT
    in those real results -- grounded in real search evidence, same discipline
    sense_competitor_content/sense_web_search_trends already apply to signals."""
    from urllib.parse import urlparse

    from app.gtm_os.intelligence.sensing import _search_google_with_fallback
    from app.llm_client import generate_json
    from app.phases.partner_icp import get_partner_icp

    business_name = business_context["business_name"]
    results = _search_google_with_fallback(db, tenant_id, f'"{business_name}" competitors alternatives', num=10)
    # REAL GAP, found live 2026-10-15 (Nora/Majji/Angela, all solo fractional consultants): a
    # name-based search returns nothing for a small, not-widely-written-about business -- nobody
    # publishes "X vs Y alternatives" content about a solo consultant. Fall back to searching by
    # the partner's own stated industry (partner_icp's real `industries` list, never invented --
    # same real-data-only discipline as the name search) to find real peer firms instead.
    if not results:
        icp = get_partner_icp(db, tenant_id) or {}
        industries = icp.get("industries") or []
        if industries:
            results = _search_google_with_fallback(db, tenant_id, f"best {industries[0]} consulting firms advisors", num=10)
    if not results:
        return []

    results_block = "\n".join(
        f"- {r.get('title', '')} ({r.get('url', '')}): {r.get('description', '')}" for r in results
    )

    try:
        result = generate_json(
            COMPETITOR_EXTRACTION_PROMPT.format(
                business_name=business_name,
                positioning=business_context.get("positioning") or "(not stated)",
                audience=business_context.get("audience") or "(not stated)",
                results_block=results_block,
            ),
            db, tenant_id, max_tokens=500,
        )
    except Exception:
        return []

    real_domains = {urlparse(r["url"]).netloc.lower().removeprefix("www.") for r in results if r.get("url")}

    competitors = []
    seen_domains: set[str] = set()
    for c in (result.get("competitors") or []):
        if not isinstance(c, dict):
            continue
        name, domain = c.get("name"), c.get("domain")
        if not name or not domain:
            continue
        domain = domain.strip().lower().removeprefix("www.")
        # Grounding check: only keep a competitor whose domain actually appeared in the real
        # search results above -- an LLM drifting off-prompt and naming something not in the
        # evidence is caught here, not trusted.
        if domain not in real_domains or domain in seen_domains:
            continue
        seen_domains.add(domain)
        competitors.append({"name": name.strip(), "domain": domain, "enabled": True})
    return competitors


def get_enabled_content_competitors(db: Session, tenant_id: int) -> list[dict]:
    return [c for c in get_content_competitors(db, tenant_id) if c.get("enabled", True)]


def set_content_competitors(db: Session, tenant_id: int, competitors: list[dict]) -> None:
    _validate_competitors(competitors)
    normalized = [_normalize_competitor(c) for c in competitors]

    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == COMPETITORS_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = normalized
    else:
        param = Parameter(
            tenant_id=tenant_id,
            key=COMPETITORS_PARAMETER_KEY,
            value=normalized,
            description="Content Intelligence named-competitor domains, searched under the same keywords as topics.py -- see competitors.py",
        )
        db.add(param)
    db.commit()
