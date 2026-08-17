"""Small, shared LinkedIn-URL helpers with no framework/route dependencies -- safe to import
from both app/routes/api.py and app/gtm_os/intelligence/ (the intelligence layer must never
import from app/routes/*, so this lives at the app-level instead, alongside apify_client.py/
website_scraper.py-style standalone modules)."""


def normalize_linkedin_url(url: str | None) -> str | None:
    """LinkedIn URLs come in inconsistently from every source we deal with (our own decision-
    maker search, SalesRobot's prospect records) -- with/without https://, with/without www.,
    with/without a trailing slash, mixed case. Matching on raw strings silently drops real
    matches. Normalizes to the bare "linkedin.com/in/handle" form for reliable comparison."""
    if not url:
        return None
    normalized = url.strip().lower()
    for prefix in ("https://", "http://"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    if normalized.startswith("www."):
        normalized = normalized[4:]
    return normalized.rstrip("/")
