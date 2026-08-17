"""Content Intelligence feed/source configuration -- Step 16D.

Deliberately SEPARATE from topics.py's topic/alias configuration, per the Step 16D spec's own
explicit instruction: no `rss_url`/`reddit_url`/`linkedin_url` field belongs inside a topic
definition. A topic is WHAT we care about, independent of WHICH sources we watch for it; a feed
is WHERE we look, independent of what we're looking for -- keeping them separate is what lets a
future Reddit/News/GDELT source reuse the exact same topic configuration without topics.py ever
needing to know sources exist at all (topics.py already has zero source-specific fields, and this
file keeps it that way).

Same underlying `Parameter` storage pattern as topics.py (and linkedin_monitor.py before it) --
own key, own shape, same get/set/validate discipline, not a new mechanism invented."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

FEEDS_PARAMETER_KEY = "content_intelligence_feeds"

# Deliberately empty -- no real starter feed list has been provided (same reasoning as
# topics.py's own DEFAULT_TOPICS).
DEFAULT_FEEDS: list[dict] = []


class FeedConfigError(ValueError):
    """Raised when feed configuration fails validation -- never silently coerced or dropped."""


def _validate_feeds(feeds: list[dict]) -> None:
    if not isinstance(feeds, list):
        raise FeedConfigError("feeds must be a list")

    seen_urls: set[str] = set()
    for i, feed in enumerate(feeds):
        if not isinstance(feed, dict):
            raise FeedConfigError(f"feed at index {i} must be an object")

        url = feed.get("url")
        if not isinstance(url, str) or not url.strip():
            raise FeedConfigError(f"feed at index {i} has an empty or missing url")
        normalized_url = url.strip().lower()
        if normalized_url in seen_urls:
            raise FeedConfigError(f"duplicate feed url: {url!r}")
        seen_urls.add(normalized_url)

        name = feed.get("name")
        if name is not None and not isinstance(name, str):
            raise FeedConfigError(f"feed {url!r} has a malformed name -- must be a string or null")

        enabled = feed.get("enabled", True)
        if not isinstance(enabled, bool):
            raise FeedConfigError(f"feed {url!r} has a malformed 'enabled' value -- must be true/false")


def _normalize_feed(feed: dict) -> dict:
    return {
        "url": feed["url"].strip(),
        "name": feed.get("name"),
        "enabled": feed.get("enabled", True),
    }


def get_content_feeds(db: Session, tenant_id: int) -> list[dict]:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == FEEDS_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, list):
        return param.value
    return DEFAULT_FEEDS


def get_enabled_content_feeds(db: Session, tenant_id: int) -> list[dict]:
    return [f for f in get_content_feeds(db, tenant_id) if f.get("enabled", True)]


def set_content_feeds(db: Session, tenant_id: int, feeds: list[dict]) -> None:
    _validate_feeds(feeds)
    normalized = [_normalize_feed(f) for f in feeds]

    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == FEEDS_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = normalized
    else:
        param = Parameter(
            tenant_id=tenant_id,
            key=FEEDS_PARAMETER_KEY,
            value=normalized,
            description="Content Intelligence feed/source configuration (RSS/Atom feed URLs) -- separate from topic config, see content/topics.py",
        )
        db.add(param)
    db.commit()
