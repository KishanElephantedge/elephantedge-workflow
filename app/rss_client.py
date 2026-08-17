"""RSS/Atom feed fetching -- Step 16D.

Access-method decision (per the Step 16D spec's explicit instruction to evaluate options rather
than default to what's already integrated): three real options were considered --

1. Apify's RSS Feed Aggregator (Step 16B's own finding -- same provider relationship this
   codebase already has, no new vendor onboarding). REJECTED for this task specifically: it adds
   per-run cost and external-call latency/failure modes for something that is structurally just
   "fetch a URL, parse XML" -- a task this system's own existing httpx client already does most
   of. Paying a third party for that isn't the simplest reliable architecture, it's the most
   expensive one for no real benefit here.
2. Hand-rolled parsing via Python's stdlib xml.etree.ElementTree (zero new dependency).
   REJECTED: real-world RSS/Atom feeds are a known minefield of near-standard variants (RSS
   1.0/2.0 vs Atom, inconsistent date formats, encoding quirks, occasionally malformed XML) --
   this is exactly the reliability problem the spec asks to solve for, and hand-rolling it means
   silently mis-parsing or crashing on real feeds in ways that are hard to predict in advance.
3. **feedparser** (chosen) -- a mature, pure-Python, dependency-light library purpose-built for
   exactly this problem: it normalizes RSS/Atom variants into one consistent entry shape, tolerates
   malformed feeds (exposing a `bozo` flag rather than raising), and normalizes dates into
   `time.struct_time` regardless of the wildly inconsistent ways feeds actually format them. One
   new dependency (added to requirements.txt), genuinely justified by reliability, not adopted
   for its own sake.

Fetching itself still goes through httpx (already this project's standard outbound-HTTP client,
same timeout/header conventions as website_scraper.py) rather than feedparser's own built-in
URL-fetching, keeping this consistent with how every other source in this codebase makes HTTP
calls."""

import httpx
import feedparser


class RssFeedError(Exception):
    pass


def fetch_feed_entries(feed_url: str, timeout: int = 15) -> list[dict]:
    """Returns a list of normalized entry dicts -- title/link/guid/summary/content/author/
    published/published_parsed/updated/updated_parsed/tags. Any field the feed doesn't provide
    is None, never invented. Raises RssFeedError only for a real fetch failure (network/HTTP
    status) -- a fetchable-but-malformed feed is feedparser's own problem to tolerate, not an
    error this function raises (real-world feeds are often slightly non-conformant; feedparser's
    own `bozo`/`bozo_exception` attributes mark this without preventing parsing of whatever
    entries it CAN recover)."""
    try:
        response = httpx.get(
            feed_url, timeout=timeout, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ElephantEdgeContentBot/1.0)"},
        )
    except httpx.HTTPError as e:
        raise RssFeedError(f"Failed to fetch {feed_url}: {e}") from e
    if response.status_code != 200:
        raise RssFeedError(f"{feed_url} returned status {response.status_code}")

    parsed = feedparser.parse(response.content)
    entries = []
    for entry in parsed.entries:
        content = None
        if entry.get("content"):
            content = entry["content"][0].get("value")
        tags = [t.get("term") for t in entry.get("tags", []) if t.get("term")] or None
        entries.append({
            "title": entry.get("title"),
            "link": entry.get("link"),
            "guid": entry.get("id") or entry.get("guid"),
            "summary": entry.get("summary"),
            "content": content,
            "author": entry.get("author"),
            "published": entry.get("published"),
            "published_parsed": entry.get("published_parsed"),
            "updated": entry.get("updated"),
            "updated_parsed": entry.get("updated_parsed"),
            "tags": tags,
        })
    return entries
