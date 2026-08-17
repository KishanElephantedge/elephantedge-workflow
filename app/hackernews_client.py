"""Official Hacker News API -- public, free, no authentication, no documented rate limit
(confirmed against github.com/HackerNews/API, Step 16B research). Base URL and endpoints below
are exactly the documented ones; nothing here is guessed or assumed.

Used endpoints (of the full documented set -- only what this first adapter needs):
- GET /v0/topstories.json  -> up to 500 story ids, ranked
- GET /v0/item/<id>.json   -> one item (story/comment/job/poll), or null if it doesn't exist

Not used in this first version (named, not implemented): /v0/newstories.json, /v0/beststories.json,
/v0/askstories.json, /v0/showstories.json, /v0/jobstories.json, /v0/user/<username>.json,
/v0/updates.json -- all real, documented, simply not needed for a first "top stories" adapter."""

import httpx

BASE_URL = "https://hacker-news.firebaseio.com/v0"


class HackerNewsError(Exception):
    pass


def get_top_story_ids(limit: int = 100) -> list[int]:
    """Returns up to `limit` of the current top story ids (the endpoint itself returns up to
    500; this just slices client-side, no server-side limit param exists)."""
    response = httpx.get(f"{BASE_URL}/topstories.json", timeout=15)
    if response.status_code != 200:
        raise HackerNewsError(f"topstories.json failed ({response.status_code}): {response.text[:300]}")
    ids = response.json() or []
    return ids[:limit]


def get_item(item_id: int) -> dict | None:
    """Returns one item's full object, or None if the id doesn't exist (the API itself returns
    a bare `null` body for that case, per the official docs -- not an error)."""
    response = httpx.get(f"{BASE_URL}/item/{item_id}.json", timeout=15)
    if response.status_code != 200:
        raise HackerNewsError(f"item/{item_id}.json failed ({response.status_code}): {response.text[:300]}")
    return response.json()
