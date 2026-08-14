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

from app.apify_client import ApifyError, search_google_ai_overview, search_linkedin_posts
from app.apify_client import _get_api_key as _get_apify_api_key
from app.db.models import ReverseDiscoveryCandidate
from app.jobo_client import JoboError, find_company_id_by_name, get_company_profile
from app.jobo_client import _get_api_key as _get_jobo_api_key
from app.llm_client import generate_json
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


GOOGLE_ICP_FLOOR_PROMPT = """The following is a Google AI Overview answering a search about \
the company "{company_name}".

AI Overview text:
{text}

Based ONLY on what this text actually states or clearly implies (never guess or use outside \
knowledge), determine:
1. Is this company headquartered in the United States?
2. Approximately how many employees does it have?

If the text is clearly about a different company that just shares a similar name, or doesn't \
give enough information to answer confidently, say so rather than guessing.

Return JSON exactly:
{{"confident_same_company": true/false, "is_us_based": true/false/null, "employee_count_estimate": <integer or null>, "reasoning": "one sentence"}}"""


def _check_icp_floor_via_google(company_name: str, db: Session, tenant_id: int) -> tuple[str, str]:
    """Fallback for when Jobo has no data (the common case -- Jobo's index covers only a
    fraction of real companies). Real added cost per call (~$0.0085 Google search + a tiny LLM
    call), only ever run on candidates Jobo already couldn't answer, never on the raw search
    volume."""
    try:
        api_key = _get_apify_api_key(db, tenant_id)
        content = search_google_ai_overview(api_key, f"{company_name} company headquarters location number of employees")
    except ApifyError:
        return "unknown", "Google search failed"
    if not content:
        return "unknown", "no Google AI Overview for this company"
    try:
        extracted = generate_json(GOOGLE_ICP_FLOOR_PROMPT.format(company_name=company_name, text=content), db, tenant_id, max_tokens=200)
    except Exception as e:
        return "unknown", f"classifier failed: {e}"
    if not extracted.get("confident_same_company"):
        return "unknown", "Google result not confidently about this exact company"

    is_us = extracted.get("is_us_based")
    count = extracted.get("employee_count_estimate")
    reasoning = extracted.get("reasoning") or ""
    if is_us is False:
        return "rejected", f"not US-based per Google ({reasoning})"
    if count is not None and not (8 <= count <= 55):
        return "rejected", f"~{count} employees per Google, outside 11-50 range ({reasoning})"
    if is_us and count is not None:
        return "qualified", f"US-based, ~{count} employees per Google ({reasoning})"
    return "unknown", f"Google didn't confirm both floors ({reasoning})"


def check_icp_floor(company_name: str, db: Session, tenant_id: int) -> tuple[str, str]:
    """Only the two floors the user confirmed -- US-based AND 11-50 employees, nothing
    stricter. Returns (status, reasoning); status is "qualified"/"rejected"/"unknown".
    Tries Jobo first (free); if Jobo has no data (the common case -- its index covers only a
    fraction of real companies, ~11% per prior findings), falls back to a Google AI Overview
    lookup before giving up. "unknown" only when neither source can answer -- never silently
    dropped, always surfaced for manual review."""
    try:
        api_key = _get_jobo_api_key(db, tenant_id)
        with httpx.Client() as client:
            company_id = find_company_id_by_name(client, api_key, company_name)
            profile = get_company_profile(client, company_id) if company_id else None
    except (httpx.HTTPError, JoboError):
        profile = None

    if profile:
        if profile.get("country_code") != "US":
            return "rejected", f"not US (country_code={profile.get('country_code')!r})"
        if profile.get("company_size") != ICP_TARGET_EMPLOYEE_SIZE:
            return "rejected", f"employee size {profile.get('company_size')!r}, not {ICP_TARGET_EMPLOYEE_SIZE}"
        return "qualified", "US-based, 11-50 employees (Jobo)"

    return _check_icp_floor_via_google(company_name, db, tenant_id)


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
