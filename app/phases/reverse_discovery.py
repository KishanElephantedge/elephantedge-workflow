"""Mode B from the hot-leads research: reverse discovery. Finds people PUBLICLY discussing or
actively exploring building AI SDR / autonomous sales themselves -- with NO job posting
involved at all, which every other signal in this system (hiring-based, firmographic-based)
completely misses. Directly observed live: the test post used to validate the LinkedIn monitor
("digging into how AI SDR systems are reshaping outbound...") is a real example of exactly this
pattern.

Inverts the normal pipeline order: broad keyword search across LinkedIn -> keyword hit ->
LLM relevance filter (reused from linkedin_monitor.py) -> identify the post's author -> identify
their company -> minimal ICP floor check (US-based AND 11-50 employees ONLY -- confirmed by the
user as the only two gates, nothing stricter) -> if it passes, the post's AUTHOR becomes the
direct outreach target (they've already self-identified as engaged with this exact topic, so
there's no separate decision-maker search the way the rest of the pipeline does).

Uses the same Apify actor already integrated for the Targets/competitor-watch feature
(supreme_coder/linkedin-post) -- its `urls` input accepts a LinkedIn keyword SEARCH URL, not
just profile URLs, confirmed against the actor's own documentation, so no new tool is needed,
just a different kind of input to the same one.

Kept deliberately SEPARATE from linkedin_monitor.py's Targets feature and its DB tables --
Targets watches a fixed list of KNOWN profiles; this searches broadly for UNKNOWN people. Per
explicit user instruction, this gets its own tab/filter in the UI, not folded into Targets or
the main Companies list."""

from datetime import datetime
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from app.apify_client import ApifyError, search_linkedin_posts
from app.apify_client import _get_api_key as _get_apify_api_key
from app.db.models import ReverseDiscoveryCandidate
from app.jobo_client import JoboError, find_company_id_by_name, get_company_profile
from app.jobo_client import _get_api_key as _get_jobo_api_key
from app.phases.linkedin_monitor import classify_relevance, get_keyword_tiers

# Only the sharpest tier -- a broad public search needs tight keywords, unlike the Targets
# watch-list where any tier is fine because the profile is already known-relevant.
SEARCH_TIER = "Tier 1: Direct signals"
POSTS_PER_KEYWORD = 10
DATE_POSTED_FILTER = "past-week"
ICP_TARGET_EMPLOYEE_SIZE = "11-50"


def _build_search_url(keyword: str) -> str:
    return f"https://www.linkedin.com/search/results/content/?keywords={quote(keyword)}&datePosted=%22{DATE_POSTED_FILTER}%22&origin=FACETED_SEARCH"


def _guess_company_name(occupation: str) -> str | None:
    for sep in (" at ", " @ ", "@"):
        if sep in occupation:
            return occupation.split(sep)[-1].strip() or None
    return None


def check_icp_floor(company_name: str, db: Session, tenant_id: int) -> tuple[str, str]:
    """Only the two floors the user confirmed -- US-based AND 11-50 employees, nothing
    stricter. Returns (status, reasoning); status is "qualified"/"rejected"/"unknown".
    "unknown" (not rejected) when Jobo has no data for this company -- Jobo's own index is
    known to cover only a fraction of real companies (~11% per prior findings), so most
    candidates will land here, and that's surfaced for manual review, never silently dropped."""
    try:
        api_key = _get_jobo_api_key(db, tenant_id)
        with httpx.Client() as client:
            company_id = find_company_id_by_name(client, api_key, company_name)
            if not company_id:
                return "unknown", "no Jobo match for this company name"
            profile = get_company_profile(client, company_id)
    except (httpx.HTTPError, JoboError):
        return "unknown", "Jobo lookup failed"
    if not profile:
        return "unknown", "no Jobo profile data"
    if profile.get("country_code") != "US":
        return "rejected", f"not US (country_code={profile.get('country_code')!r})"
    if profile.get("company_size") != ICP_TARGET_EMPLOYEE_SIZE:
        return "rejected", f"employee size {profile.get('company_size')!r}, not {ICP_TARGET_EMPLOYEE_SIZE}"
    return "qualified", "US-based, 11-50 employees"


def run_reverse_discovery_sweep(db: Session, tenant_id: int) -> dict:
    keywords = get_keyword_tiers(db, tenant_id).get(SEARCH_TIER, [])
    if not keywords:
        return {"keywords_searched": 0, "candidates_found": 0}

    api_key = _get_apify_api_key(db, tenant_id)
    found = 0
    for keyword in keywords:
        try:
            posts = search_linkedin_posts(api_key, [_build_search_url(keyword)], limit_per_source=POSTS_PER_KEYWORD)
        except ApifyError:
            continue

        for post in posts:
            urn = post.get("urn")
            if not urn:
                continue
            already_seen = (
                db.query(ReverseDiscoveryCandidate)
                .filter(ReverseDiscoveryCandidate.tenant_id == tenant_id)
                .filter(ReverseDiscoveryCandidate.post_urn == urn)
                .first()
            )
            if already_seen:
                continue

            classification = classify_relevance(post.get("text") or "", [keyword], SEARCH_TIER, db, tenant_id)
            if classification.get("recommended_action") == "ignore":
                # Unlike the Targets watch-list (small, known-relevant, worth keeping every
                # match for audit), a broad public search surfaces far more noise -- storing
                # every "ignore" here would make this table mostly garbage. Real matches are
                # never dropped, only ones the classifier is confident are off-topic.
                continue

            author_profile_url = post.get("authorProfileUrl") or ""
            if "/company/" in author_profile_url:
                # Confirmed live (2026-08-14): roughly 40% of keyword-search hits are vendor/
                # marketing brand pages (e.g. AI-SDR-tool companies promoting their own
                # product), not individuals organically discussing the topic -- not the
                # self-declared-interest signal this mode exists to find. Real noise, not a
                # borderline case, so this one IS dropped rather than stored as "unknown".
                continue

            author = post.get("author") or {}
            author_name = post.get("authorName") or " ".join(filter(None, [author.get("firstName"), author.get("lastName")])).strip()
            occupation = author.get("occupation") or ""

            # Confirmed live: `occupation` is empty on search-RESULT posts (this actor only
            # populates it when scraping a profile's own activity, not a search-results page)
            # -- so a second, targeted call directly against the author's profile URL is
            # needed to get real occupation/company text. Small, bounded cost: only for
            # candidates that already passed the relevance filter above, never the raw search
            # volume.
            if not occupation and author_profile_url:
                try:
                    own_posts = search_linkedin_posts(api_key, [author_profile_url], limit_per_source=1)
                    if own_posts:
                        occupation = ((own_posts[0].get("author") or {}).get("occupation")) or ""
                except ApifyError:
                    pass

            company_name = _guess_company_name(occupation)
            if company_name:
                icp_status, icp_reasoning = check_icp_floor(company_name, db, tenant_id)
            else:
                icp_status, icp_reasoning = "unknown", "no company name parsable from profile occupation text"

            posted_at = None
            if post.get("postedAtISO"):
                try:
                    posted_at = datetime.fromisoformat(post["postedAtISO"].replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    pass

            db.add(ReverseDiscoveryCandidate(
                tenant_id=tenant_id, post_urn=urn, post_url=post.get("url"), post_text=post.get("text"),
                matched_keyword=keyword, author_name=author_name or None,
                author_profile_url=post.get("authorProfileUrl"), author_occupation=occupation or None,
                guessed_company_name=company_name, relevance_score=classification.get("relevance_score"),
                recommended_action=classification.get("recommended_action"), classifier_reason=classification.get("reason"),
                icp_status=icp_status, icp_reasoning=icp_reasoning, posted_at=posted_at,
            ))
            db.commit()
            found += 1

    return {"keywords_searched": len(keywords), "candidates_found": found}
