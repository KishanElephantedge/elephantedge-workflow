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


def _normalize_competitor(competitor: dict) -> dict:
    return {
        "name": competitor["name"].strip(),
        "domain": competitor["domain"].strip().lower(),
        "enabled": competitor.get("enabled", True),
    }


def get_content_competitors(db: Session, tenant_id: int) -> list[dict]:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == COMPETITORS_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, list) and param.value:
        return param.value
    return DEFAULT_COMPETITORS


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
