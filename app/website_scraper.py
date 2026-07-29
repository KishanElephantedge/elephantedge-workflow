"""Plain HTTP scraper for a company's own website -- genuinely free (no third-party API),
since a public marketing site doesn't need a specialized scraping provider the way LinkedIn
does. Extracts visible text only, not JS-rendered content -- sufficient for marketing copy
(product description, value props), which is almost always server-rendered HTML."""
import re

import httpx


class ScrapeError(Exception):
    pass


def _strip_html(html: str) -> str:
    # Drop script/style blocks entirely -- their text isn't visible content.
    html = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def scrape_page(url: str, max_chars: int = 8000) -> str:
    """Fetches one page and returns its visible text, truncated to max_chars (keeps the
    Claude prompt this feeds into a predictable, bounded size)."""
    try:
        response = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (compatible; ElephantEdgeResearchBot/1.0)"})
    except httpx.HTTPError as e:
        raise ScrapeError(f"Failed to fetch {url}: {e}") from e
    if response.status_code != 200:
        raise ScrapeError(f"{url} returned status {response.status_code}")
    return _strip_html(response.text)[:max_chars]


def scrape_company_website(domain: str) -> str:
    """Tries the homepage plus a couple of common "about/product" paths, concatenating
    whatever loads successfully -- a single page is often too thin (just a hero + nav) for a
    real product/value-prop summary."""
    base = domain if domain.startswith("http") else f"https://{domain}"
    paths_to_try = ["", "/product", "/about", "/solutions"]
    combined = []
    for path in paths_to_try:
        try:
            text = scrape_page(base.rstrip("/") + path, max_chars=4000)
            if text:
                combined.append(text)
        except ScrapeError:
            continue
    if not combined:
        raise ScrapeError(f"Could not fetch any page for {domain}")
    return "\n\n---\n\n".join(combined)[:12000]
