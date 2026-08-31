"""Sensing layer -- Step 3 of the GTM OS build. Five adapters, one per source identified in
sensing-sources-analysis.md, each calling an existing CLIENT-level function directly (never
app/phases/* orchestration, so this has zero coupling to and zero risk for the production
pipeline) and writing the result as raw GtmSignal rows.

Each adapter only OBSERVES and RECORDS -- no adapter classifies demand/opportunity, scores
offering-fit, or decides what to do with a signal. That is later, not-yet-built intelligence-
layer work that will read from gtm_signals, not something these functions do.

Duplicate observations (the same source_ref sensed again on a later sweep) are not blocked or
overwritten -- every call is still inserted, with its dedup_key set, so a later step can decide
how to treat repeats without any evidence having been silently dropped.

Step 11A's sense_linkedin_replies() is a deliberate, documented EXCEPTION to that "always insert"
rule -- see its own docstring for why (SalesRobot's syncedMessages endpoint returns full thread
history on every call, unlike every other source's small-incremental-results shape).

sense_linkedin_posts() (2026-08-24) is a second, equally deliberate exception: it is called
repeatedly over time against the same profile URLs, and the actor legitimately returns the same
recent posts again on a later call -- an "already sensed by source_ref" guard is applied before
insert, same pattern as linkedin_reply/hackernews_story/rss_article below.

sense_linkedin_jobs() (2026-08-24) is a third such exception, same reasoning and same guard --
confirmed live: the exact same Codeable job posting (same linkedin_id/url) was stored twice
across overlapping search calls before this guard existed."""

import hashlib
from datetime import datetime
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.apify_client import _get_api_key as _get_apify_api_key
from app.apify_client import ApifyError, search_google_ai_overview, search_google_organic_results, search_linkedin_jobs, search_linkedin_posts
from app.deepline_client import execute_tool
from app.gtm_os.intelligence.signal import GtmSignal
from app.phases.discovery import EMPLOYEE_COUNT_MIN, REVENUE_MIN_USD
from app.phases.hiring_signal import JOB_POSTING_MAX_AGE_DAYS
from app.phases.jd_first_discovery import (
    JD_FIRST_EMPLOYEE_COUNT_MAX,
    JD_FIRST_MAX_FUNDING_USD,
    JD_FIRST_MAX_REVENUE_USD,
    JOB_TITLE_PATTERN_OR,
)
from app.phases.reverse_discovery import _guess_company_name
from app.website_scraper import scrape_company_website
from app.db.models import Contact
from app.linkedin_utils import normalize_linkedin_url
from app.salesrobot_client import SalesRobotError, get_campaign_prospects, get_synced_messages
from app.hackernews_client import get_item, get_top_story_ids
from app.gtm_os.content.topics import get_enabled_content_topics, match_topic
from app.gtm_os.content.feeds import get_enabled_content_feeds
from app.gtm_os.content.competitors import get_enabled_content_competitors
from app.rss_client import fetch_feed_entries


# Bounded so a large inbox can never turn one sensing run into an unbounded crawl. 100 threads
# per page; 10 pages is far more history than a daily incremental sweep needs.
# The endpoint returns 20 per page regardless of what is asked for, so this is stated rather
# than requested. 60 pages = 1200 threads, comfortably past the current 822, and the loop exits
# early anyway as soon as every replying prospect has been located.
SYNCED_INBOX_PAGE_SIZE = 20
SYNCED_INBOX_MAX_PAGES = 60


def _synced_total_pages(raw_response: dict) -> int | None:
    """totalPages off the real (second) pagination wrapper -- see _extract_thread_pages for why
    data[0] is the wrong one."""
    for wrapper in (raw_response.get("data") or []):
        if isinstance(wrapper, dict) and wrapper.get("data") is not None:
            pages = wrapper.get("totalPages")
            if isinstance(pages, int):
                return pages
    return None



def _parse_tag_list(value) -> list[str]:
    """SalesRobot returns tagList as a JSON-encoded STRING (e.g. '["Interested"]'), not a list --
    iterating it raw yields individual characters. Confirmed live 2026-08-31."""
    import json as _json
    if isinstance(value, list):
        return [str(t) for t in value]
    if isinstance(value, str) and value.strip():
        try:
            parsed = _json.loads(value)
            return [str(t) for t in parsed] if isinstance(parsed, list) else [value]
        except ValueError:
            return [value]
    return []


def _dedup_key(source: str, source_ref: str) -> str:
    return hashlib.sha256(f"{source}:{source_ref}".encode()).hexdigest()


def _parse_dt(value) -> datetime | None:
    """Best-effort parse of whatever date shape a raw source item hands back -- these vary
    (Apify actors return ISO strings, sometimes with a Z suffix; some items omit it entirely).
    Returns None rather than raising, since this is only ever used for the informational
    observed_at field, never for filtering or dedup."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def sense_linkedin_jobs(
    db: Session,
    tenant_id: int,
    title_search: list[str],
    location_search: list[str],
    organization_employees_gte: int,
    organization_employees_lte: int,
    industry_filter: list[str],
    time_range: str = "24h",
    limit: int = 100,
) -> list[GtmSignal]:
    api_key = _get_apify_api_key(db, tenant_id)
    items = search_linkedin_jobs(
        api_key,
        title_search=title_search,
        location_search=location_search,
        organization_employees_gte=organization_employees_gte,
        organization_employees_lte=organization_employees_lte,
        industry_filter=industry_filter,
        time_range=time_range,
        limit=limit,
    )
    signals = []
    for item in items:
        source_ref = str(item.get("id") or item.get("jobUrl") or item.get("url") or "")
        if not source_ref:
            continue

        # Real dedup bug fix (2026-08-24, confirmed live: the exact same Codeable BDR posting,
        # same linkedin_id/url, stored 2x). Same missing "already sensed" guard already applied
        # to sense_linkedin_posts() earlier today -- this adapter is also called repeatedly over
        # time/across overlapping search queries, and the same real job can legitimately be
        # returned again on a later call.
        already_sensed = (
            db.query(GtmSignal)
            .filter(GtmSignal.tenant_id == tenant_id, GtmSignal.source == "linkedin_job", GtmSignal.source_ref == source_ref)
            .first()
        )
        if already_sensed:
            continue

        # Real bug fix (2026-08-24, confirmed live): the raw actor response uses "organization"
        # (and "org_linkedin_website"/"org_linkedin_headcount"/"date_posted"), never
        # "organizationName"/"companyName"/"postedAt" -- those never matched anything real,
        # meaning company_name_raw was silently None on every linkedin_job signal this source
        # ever produced. org_linkedin_website/org_linkedin_headcount are real, already-present
        # fields captured here for the first time -- free byproducts of the same billed call,
        # not a new lookup -- kept in extracted_info for a future company-enrichment pass to use.
        #
        # 2026-08-24 addition: description_text (the REAL, full job description body) and several
        # other AI-extracted/firmographic fields were confirmed present in the raw actor response
        # but never captured anywhere -- also free byproducts of the same billed call. Captured
        # here so a future JD-content classifier (distinguishing a genuine first-sales-hire/
        # building-from-scratch gap from an ordinary scaling hire -- exactly the missing context
        # problem_detection.py's own docstring names as the reason hiring_activity stays
        # contextual-tier-only) has real text to work with instead of just a bare job title.
        signal = GtmSignal(
            tenant_id=tenant_id,
            source="linkedin_job",
            source_ref=source_ref,
            signal_type="job_posting",
            observed_at=_parse_dt(item.get("date_posted") or item.get("postedAt") or item.get("datePosted")),
            company_name_raw=item.get("organization") or item.get("organizationName") or item.get("companyName"),
            raw_evidence=item,
            extracted_info={
                "title": item.get("title"),
                "location": item.get("location"),
                "organization_domain": item.get("org_linkedin_website"),
                "organization_headcount": item.get("org_linkedin_headcount"),
                "description_text": item.get("description_text"),
                "seniority": item.get("seniority"),
                "ai_experience_level": item.get("ai_experience_level"),
                "ai_core_responsibilities": item.get("ai_core_responsibilities"),
                "ai_requirements_summary": item.get("ai_requirements_summary"),
                "organization_industry": item.get("org_linkedin_industry"),
                "organization_founded_date": item.get("org_linkedin_founded_date"),
                "organization_description": item.get("org_linkedin_description"),
            },
            dedup_key=_dedup_key("linkedin_job", source_ref),
        )
        db.add(signal)
        signals.append(signal)
    db.commit()
    return signals


def sense_linkedin_posts(
    db: Session,
    tenant_id: int,
    profile_urls: list[str],
    scrape_until: str | None = None,
    limit_per_source: int = 5,
) -> list[GtmSignal]:
    """Step 6A fix: the real actor payload uses `postedAtISO` (not `postedAt`/`date`) and has no
    `authorCompany`/`company` field at all -- those two original field names never matched
    anything real, so observed_at was always None and company_name_raw was always None too. Fixed
    by reading the real fields:
      - observed_at <- postedAtISO (falls back to the never-seen postedAt/date names harmlessly,
        in case a different actor version ever does use them).
      - company_name_raw <- parsed from the author's real headline text via the SAME
        deterministic _guess_company_name() parser reverse_discovery.py already uses for this
        exact purpose.

    REAL-PAYLOAD FIX (2026-08-22, production-sample audit): `author.occupation` was NEVER a real
    field in this actor's response -- confirmed empty across every real production signal
    inspected. The real field is `author.headline` (falls back to the top-level `authorHeadline`,
    seen present even when the nested `author` object itself is thin/missing) -- e.g. a real
    production headline: "Founder and Learning Alchemist @ Wren Learning Consultancy | ...". This
    silently meant company_name_raw was empty for every single linkedin_post signal this source
    ever produced, regardless of how good the interpretation/company-resolution logic downstream
    was. Also now preserves authorProfileId/author.publicId/authorUrn/authorType in
    extracted_info -- real, already-present identity fields nothing previously captured, kept for
    a future entity-resolution layer even though none of them alone resolve a company (no
    structured company/organization field exists anywhere in this payload; the headline free-text
    is the only company-bearing field at all)."""
    api_key = _get_apify_api_key(db, tenant_id)
    items = search_linkedin_posts(api_key, profile_urls, scrape_until=scrape_until, limit_per_source=limit_per_source)
    signals = []
    for item in items:
        source_ref = str(item.get("urn") or item.get("postUrl") or item.get("url") or "")
        if not source_ref:
            continue

        # Real dedup bug fix (2026-08-24, confirmed live: the same post stored 3x for one
        # person across separate objective attempts). This adapter is called repeatedly over
        # time against the same profile URLs, and the actor legitimately returns the same
        # recent posts again on a later call -- without this guard every repeat retrieval
        # silently inserted another GtmSignal row for the identical post. Same pre-insert
        # "already sensed" pattern already used below for linkedin_reply/hackernews_story/
        # rss_article -- see this module's docstring for why those are exceptions to the
        # default "always insert" rule; linkedin_post is now a second, equally deliberate one.
        already_sensed = (
            db.query(GtmSignal)
            .filter(GtmSignal.tenant_id == tenant_id, GtmSignal.source == "linkedin_post", GtmSignal.source_ref == source_ref)
            .first()
        )
        if already_sensed:
            continue

        author = item.get("author") or {}
        headline = author.get("headline") or item.get("authorHeadline") or ""
        signal = GtmSignal(
            tenant_id=tenant_id,
            source="linkedin_post",
            source_ref=source_ref,
            signal_type="post",
            observed_at=_parse_dt(item.get("postedAtISO") or item.get("postedAt") or item.get("date")),
            # A Company-authored post IS the company -- its authorName is the real org name, not
            # a person's headline (2026-08-31). Parsing it through _guess_company_name, which is
            # built to pull an employer out of a PERSON's "Title at Company" headline, threw that
            # away and left these posts unresolvable like every other one. Person-authored posts
            # keep the headline parser exactly as before.
            person_name_raw=None if item.get("authorType") == "Company" else (item.get("authorName") or item.get("author")),
            company_name_raw=(
                item.get("authorName") if item.get("authorType") == "Company"
                else _guess_company_name(headline)
            ),
            raw_evidence=item,
            extracted_info={
                "text": item.get("text") or item.get("content"),
                "headline": headline or None,
                "author_profile_url": item.get("authorProfileUrl") or item.get("inputUrl"),
                "author_profile_id": item.get("authorProfileId") or author.get("id"),
                "author_public_id": author.get("publicId"),
                "author_urn": item.get("authorUrn"),
                "author_type": item.get("authorType"),
                # For a Company-authored post this is the company page URL, which is a far
                # stronger identity key than a name -- kept so resolution can use it directly.
                "author_company_url": item.get("authorProfileUrl") if item.get("authorType") == "Company" else None,
            },
            dedup_key=_dedup_key("linkedin_post", source_ref),
        )
        db.add(signal)
        signals.append(signal)
    db.commit()
    return signals


def sense_linkedin_post_search(db: Session, tenant_id: int) -> list[GtmSignal]:
    """GTM-OS end-to-end wiring -- the one gap that was actually blocking ProblemHypothesis/
    DemandHypothesis from ever opening (see app/gtm_os/intelligence/problem_detection.py's own
    tier map: only linkedin_post evidence can OPEN a new hypothesis, every other currently-sensed
    source is contextual-only). Reuses sense_linkedin_posts() (this same file, unmodified) with
    real LinkedIn keyword-SEARCH URLs instead of a fixed profile-URL list -- the exact same
    `supreme_coder/linkedin-post` actor input shape already proven working in
    reverse_discovery.py's own broad-search feature, just a different caller.

    EXPLICITLY NOT the Network/LinkedIn-monitor watch-list (LinkedinMonitorProfile) -- this
    function never imports or queries that table; see linkedin_search_config.py's own module
    docstring for the full reasoning.

    Search phrases come from linkedin_search_config.py, DERIVED from this tenant's real ICP/
    offering/business-context config, never hardcoded here -- and are rate-limited per-phrase
    (select_due_phrases/record_phrases_searched) so the same query isn't re-billed to Apify every
    single hourly tick."""
    from app.gtm_os.intelligence.linkedin_search_config import get_linkedin_search_config, record_phrases_searched, select_due_phrases

    config = get_linkedin_search_config(db, tenant_id)
    due_phrases = select_due_phrases(config)
    if not due_phrases:
        return []

    search_urls = [
        f"https://www.linkedin.com/search/results/content/?keywords={quote(phrase)}&datePosted=%22{config['date_posted_filter']}%22&origin=FACETED_SEARCH"
        for phrase in due_phrases
    ]
    signals = sense_linkedin_posts(db, tenant_id, search_urls, limit_per_source=config["posts_per_phrase"])
    record_phrases_searched(db, tenant_id, config, due_phrases)
    return signals


def sense_web_search(db: Session, tenant_id: int, query: str) -> list[GtmSignal]:
    api_key = _get_apify_api_key(db, tenant_id)
    overview_text = search_google_ai_overview(api_key, query)
    if overview_text is None:
        return []
    source_ref = query
    signal = GtmSignal(
        tenant_id=tenant_id,
        source="web_search",
        source_ref=source_ref,
        signal_type="search_result",
        observed_at=None,
        raw_evidence={"query": query, "ai_overview": overview_text},
        extracted_info={"text": overview_text},
        dedup_key=_dedup_key("web_search", source_ref),
    )
    db.add(signal)
    db.commit()
    return [signal]


def sense_company_website(db: Session, tenant_id: int, domain: str, company_id: int | None = None) -> list[GtmSignal]:
    text = scrape_company_website(domain)
    if not text:
        return []
    signal = GtmSignal(
        tenant_id=tenant_id,
        source="company_website",
        source_ref=domain,
        signal_type="page_content",
        observed_at=None,
        company_id=company_id,
        company_name_raw=domain,
        raw_evidence={"domain": domain, "text": text},
        extracted_info={"text": text},
        dedup_key=_dedup_key("company_website", domain),
    )
    db.add(signal)
    db.commit()
    return [signal]


def sense_theirstack_jobs(
    db: Session,
    tenant_id: int,
    offset: int = 0,
    limit: int = 25,
    exclude_domains: list[str] | None = None,
) -> list[GtmSignal]:
    response = execute_tool(
        "theirstack_job_search",
        {
            "job_title_pattern_or": JOB_TITLE_PATTERN_OR,
            "posted_at_max_age_days": JOB_POSTING_MAX_AGE_DAYS,
            "min_employee_count": EMPLOYEE_COUNT_MIN,
            "max_employee_count": JD_FIRST_EMPLOYEE_COUNT_MAX,
            "min_revenue_usd": REVENUE_MIN_USD,
            "max_revenue_usd": JD_FIRST_MAX_REVENUE_USD,
            "max_funding_usd": JD_FIRST_MAX_FUNDING_USD,
            "job_country_code_or": ["US"],
            "company_domain_not": exclude_domains or [],
            "order_by": [{"field": "date_posted", "desc": True}],
            "offset": offset,
            "limit": limit,
        },
    )
    raw = response.get("toolResponse", {}).get("raw", {})
    items = raw.get("data", []) if isinstance(raw, dict) else []
    signals = []
    for item in items:
        source_ref = str(item.get("id") or item.get("url") or "")
        if not source_ref:
            continue
        company = item.get("company") or {}
        signal = GtmSignal(
            tenant_id=tenant_id,
            source="theirstack_job",
            source_ref=source_ref,
            signal_type="job_posting",
            observed_at=_parse_dt(item.get("date_posted")),
            company_name_raw=company.get("name") if isinstance(company, dict) else None,
            raw_evidence=item,
            extracted_info={"title": item.get("job_title")},
            dedup_key=_dedup_key("theirstack_job", source_ref),
        )
        db.add(signal)
        signals.append(signal)
    db.commit()
    return signals


def _extract_thread_pages(raw_response: dict) -> list[dict]:
    """syncedMessages' top-level `data` is a list of pagination wrappers, only one of which
    actually holds thread rows (confirmed live, Step 10C -- not documented anywhere, and the
    "real" wrapper isn't reliably at a fixed index). Returns the first wrapper's inner `data`
    that's actually non-empty."""
    for wrapper in raw_response.get("data") or []:
        if isinstance(wrapper, dict) and wrapper.get("data"):
            return wrapper["data"]
    return []


def _match_contact(db: Session, vanity_profile_url: str | None) -> Contact | None:
    """Matches on prospectData.profileUrl (the vanity-URL form) ONLY -- never the thread-level
    `profileUrl` field, which is LinkedIn's opaque internal ID format and will never match
    Contact.linkedin_url (see sense_linkedin_replies()'s own docstring). Same ilike-on-handle
    approach already used by the existing SalesRobot webhook receiver, reused here rather than
    reinvented -- normalize_linkedin_url lives in app/linkedin_utils.py specifically so both call
    sites can share it without the intelligence layer importing from app/routes/*."""
    normalized = normalize_linkedin_url(vanity_profile_url)
    if not normalized:
        return None
    handle = normalized.rstrip("/").split("/")[-1]
    if not handle:
        return None
    return db.query(Contact).filter(Contact.linkedin_url.ilike(f"%{handle}%")).first()


def sense_linkedin_replies(
    db: Session,
    tenant_id: int,
    linkedin_account_uuid: str,
    campaign_uuids: list[str],
) -> list[GtmSignal]:
    """Two-stage discovery, per the Step 11 design doc: (1) a cheap sweep of
    get_campaign_prospects() across the given campaigns to find prospects with a real reply
    (isReplied=True), collecting their prospectUuid; (2) syncedMessages, filtered by
    prospectUuidFilter, ONLY for that reply-flagged subset -- never a blind syncedMessages call
    per prospect, which would be far more expensive than necessary given how rare real replies
    are (confirmed live: 1 of 70+ prospects sampled in Step 10A/10C).

    Only INBOUND (messageSentByMe == False) messages become GtmSignal rows -- our own outbound
    sends are never sensed as signals (they're our own actions, not observed prospect behavior,
    see the Step 11 design doc §1-3); the immediately preceding message (whichever direction) is
    embedded as context on the inbound signal that follows it, never as its own row.

    DELIBERATE EXCEPTION to this module's own "always insert, dedup_key just marks repeats" rule
    (see the module docstring): syncedMessages returns a thread's ENTIRE message history on every
    single call, not just what's new since last sync -- unlike every other source here, which
    only ever returns a handful of genuinely fresh items per sweep. Without an explicit
    already-sensed check before insert, every sync of every thread would re-create its full
    historical row set every time. So this function checks for an existing GtmSignal with the
    same (source, source_ref=messageId) BEFORE creating a new one, and skips it if found --
    the one adapter in this file that does this, and it's intentional, not an oversight.

    Attribution uses prospectData.profileUrl (vanity URL) -- confirmed live to match
    Contact.linkedin_url -- NEVER the thread-level `profileUrl` field, which is LinkedIn's opaque
    internal member-ID format and will never match anything (this was the root cause of the
    existing webhook receiver's 100% attribution-match failure, Step 10B). prospectData.prospectUuid
    is preserved on every signal regardless of whether the match succeeds, so a future
    Contact.salesrobot_prospect_uuid column (not added in this step) could re-attribute
    historical signals without re-fetching from the API."""
    reply_prospects: list[dict] = []
    connected_prospects: list[dict] = []
    for campaign_uuid in campaign_uuids:
        page = 0
        while True:
            try:
                result = get_campaign_prospects(campaign_uuid, linkedin_account_uuid, db, tenant_id, page=page, size=100)
            except SalesRobotError:
                break
            prospects = result.get("data", {}).get("data", [])
            for p in prospects:
                if not p.get("prospectUuid"):
                    continue
                if p.get("isReplied"):
                    reply_prospects.append(p)
                elif p.get("lastActivity") == "CONNECTED":
                    # Accepted the connection but hasn't replied. A real, weaker outcome that the
                    # learning layer had no way to see -- 24 of them, against 5 replies.
                    connected_prospects.append(p)
            if len(prospects) < 100:
                break
            page += 1

    signals: list[GtmSignal] = []

    # 2026-08-31, real fix confirmed live: prospectUuidFilter matches NOTHING. SalesRobot's
    # synced inbox returns those threads with prospectData/campaignUuid/campaignName all null --
    # the inbox simply is not attributed back to campaign prospects -- so filtering by prospect
    # UUID returns totalElements=0 every time, for prospects who demonstrably HAVE replied.
    # Verified against real data: 5 prospects with isReplied=True and lastActivity="REPLIED",
    # and every filtered call came back empty while the SAME unfiltered call returned threads.
    # That is why this source produced 1 signal in its entire lifetime and the whole
    # outcome/learning loop downstream of it (sales_outcomes, confirmed_patterns) sat at zero:
    # it was failing silently, reporting success with nothing found.
    #
    # So: fetch the inbox UNFILTERED and match threads to the reply-flagged prospects ourselves,
    # on LinkedIn's stable ACoAA-style unique id, which BOTH sides really do carry (thread
    # profileUrl vs prospect uniqueLinkedinId). Matching on our own side is also strictly safer
    # than trusting a provider filter we cannot see inside.
    def _unique_id(url: str | None) -> str | None:
        if not url:
            return None
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        return tail.lower() or None

    wanted_ids = {}
    for prospect in reply_prospects:
        uid = _unique_id(prospect.get("uniqueLinkedinId"))
        if uid:
            wanted_ids[uid] = prospect

    # syncedMessages IGNORES the `size` parameter -- confirmed live: asking for size=100 still
    # returns 20 per page and echoes size=20, across a real inbox of 822 threads / 42 pages. So
    # page count must come from the response's own totalPages, never inferred from "a short page
    # means the last page" -- that assumption stopped this after ONE page, reading 20 of 822
    # threads and missing every reply.
    threads: list[dict] = []
    remaining = dict(wanted_ids)
    page = 0
    while page < SYNCED_INBOX_MAX_PAGES and remaining:
        try:
            raw_response = get_synced_messages(linkedin_account_uuid, db, tenant_id, page=page, size=SYNCED_INBOX_PAGE_SIZE)
        except SalesRobotError:
            break
        page_threads = _extract_thread_pages(raw_response)
        if not page_threads:
            break
        for thread in page_threads:
            uid = _unique_id(thread.get("profileUrl"))
            if uid in remaining:
                threads.append(thread)
                remaining.pop(uid, None)   # stop as soon as every replier is found
        total_pages = _synced_total_pages(raw_response)
        page += 1
        if total_pages is not None and page >= total_pages:
            break

    # A replier with no matching inbox thread still gets a signal (2026-08-31). SalesRobot's
    # synced inbox does not reliably contain a thread for every campaign prospect who replied --
    # confirmed live: 5 prospects with lastActivity="REPLIED" and isReplied=True, and NONE of them
    # matched any of the 822 threads in the inbox (the thread profileUrls are ACoAA-encoded while
    # most prospect uniqueLinkedinIds are ACwAA-encoded, so the two identifier spaces do not even
    # overlap).
    #
    # The fact of the reply comes from the CAMPAIGN endpoint, which is reliable and already
    # fetched above; only the message TEXT needs the inbox. Refusing to record the reply because
    # its text is unavailable is what left sales_outcomes and confirmed_patterns at zero while
    # real people were replying -- the learning loop starved on a missing nice-to-have.
    matched_ids = {_unique_id(t.get("profileUrl")) for t in threads}
    for uid, prospect in wanted_ids.items():
        if uid in matched_ids:
            continue
        source_ref = f"prospect:{prospect.get('prospectUuid')}"
        already = (
            db.query(GtmSignal)
            .filter(GtmSignal.source == "linkedin_reply", GtmSignal.source_ref == source_ref)
            .first()
        )
        if already:
            continue
        contact = _match_contact(db, prospect.get("profileUrl"))
        signal = GtmSignal(
            tenant_id=tenant_id,
            source="linkedin_reply",
            source_ref=source_ref,
            signal_type="reply",
            observed_at=_parse_dt(prospect.get("firstReplyAt")),
            person_name_raw=prospect.get("fullName"),
            company_name_raw=prospect.get("companyName"),
            company_id=contact.company_id if contact else None,
            contact_id=contact.id if contact else None,
            raw_evidence=prospect,
            extracted_info={
                # No text: the reply is real and recorded, its CONTENT is simply not retrievable
                # for this prospect. But SalesRobot's OWN human/AI classification of that reply is
                # available on the record (tagList), so sentiment comes from a real label rather
                # than being guessed at or abandoned.
                "text": None,
                "reply_evidence": "campaign_prospect.lastActivity == 'REPLIED'",
                "tag_list": _parse_tag_list(prospect.get("tagList")),
                # Which message variant actually earned the reply -- the single most directly
                # actionable thing here for learning what works.
                "replied_variant": prospect.get("repliedVariant"),
                "last_activity": prospect.get("lastActivity"),
                "campaign_name": prospect.get("campaignName"),
                "campaign_uuid": prospect.get("campaignUuid"),
                "profile_url": prospect.get("profileUrl"),
            },
            dedup_key=_dedup_key("linkedin_reply", source_ref),
        )
        db.add(signal)
        signals.append(signal)
    if signals:
        db.commit()

    for prospect in connected_prospects:
        source_ref = f"connected:{prospect.get('prospectUuid')}"
        if db.query(GtmSignal).filter(GtmSignal.source == "linkedin_reply", GtmSignal.source_ref == source_ref).first():
            continue
        contact = _match_contact(db, prospect.get("profileUrl"))
        signal = GtmSignal(
            tenant_id=tenant_id, source="linkedin_reply", source_ref=source_ref,
            signal_type="connection_accepted", observed_at=_parse_dt(prospect.get("connectionAcceptedAt")),
            person_name_raw=prospect.get("fullName"), company_name_raw=prospect.get("companyName"),
            company_id=contact.company_id if contact else None,
            contact_id=contact.id if contact else None,
            raw_evidence=prospect,
            extracted_info={
                "text": None,
                "connection_evidence": "campaign_prospect.lastActivity == 'CONNECTED'",
                "last_activity": prospect.get("lastActivity"),
                "campaign_name": prospect.get("campaignName"),
                "campaign_uuid": prospect.get("campaignUuid"),
                "profile_url": prospect.get("profileUrl"),
            },
            dedup_key=_dedup_key("linkedin_reply", source_ref),
        )
        db.add(signal); signals.append(signal)
    if signals:
        db.commit()

    for thread in threads:
            thread_id = thread.get("threadId")
            prospect_data = thread.get("prospectData") or {}
            # prospectData is null on real inbox threads, so fall back to the thread's own
            # profileUrl -- which is the ACoAA unique-id form, matched against the prospect below.
            vanity_profile_url = prospect_data.get("profileUrl") or thread.get("profileUrl")
            matched_prospect = wanted_ids.get(_unique_id(thread.get("profileUrl")) or "")
            if matched_prospect is None:
                continue  # not one of the prospects who actually replied -- ignore the thread
            company_name_raw = prospect_data.get("companyName") or matched_prospect.get("companyName")
            person_name_raw = thread.get("nameOfPerson") or matched_prospect.get("fullName")
            campaign_uuid = thread.get("campaignUuid") or matched_prospect.get("campaignUuid")
            campaign_name = thread.get("campaignName") or matched_prospect.get("campaignName")

            contact = _match_contact(db, vanity_profile_url)
            company_id = contact.company_id if contact else None
            contact_id = contact.id if contact else None

            messages = sorted(
                (thread.get("threadedMessages") or {}).get("messages") or [],
                key=lambda m: m.get("ordinal") or 0,
            )
            for i, message in enumerate(messages):
                if message.get("messageSentByMe"):
                    continue  # outbound -- context only, never its own signal (see docstring)

                message_id = message.get("messageId")
                if not message_id:
                    continue  # can't dedup/reference without a real identifier

                already_sensed = (
                    db.query(GtmSignal)
                    .filter(GtmSignal.source == "linkedin_reply", GtmSignal.source_ref == message_id)
                    .first()
                )
                if already_sensed:
                    continue  # the documented exception to "always insert" -- see docstring

                previous_message = messages[i - 1] if i > 0 else None
                previous_direction = None
                if previous_message is not None:
                    previous_direction = "outbound" if previous_message.get("messageSentByMe") else "inbound"

                signal = GtmSignal(
                    tenant_id=tenant_id,
                    source="linkedin_reply",
                    source_ref=message_id,
                    signal_type="reply_message",
                    observed_at=_parse_dt(message.get("sentTime")),
                    company_id=company_id,
                    company_name_raw=company_name_raw,
                    contact_id=contact_id,
                    person_name_raw=person_name_raw,
                    raw_evidence={
                        "message": message,
                        "thread_context": {
                            "thread_id": thread_id,
                            "campaign_uuid": campaign_uuid,
                            "prospect_uuid": prospect_data.get("prospectUuid"),
                            "previous_message": previous_message,
                        },
                    },
                    extracted_info={
                        "text": message.get("messageText"),
                        "sent_time": message.get("sentTime"),
                        "previous_message_text": (previous_message or {}).get("messageText"),
                        "previous_message_direction": previous_direction,
                        "previous_message_sent_time": (previous_message or {}).get("sentTime"),
                        "thread_id": thread_id,
                        "message_id": message_id,
                        "prospect_uuid": prospect_data.get("prospectUuid"),
                        "campaign_uuid": campaign_uuid,
                        "campaign_name": campaign_name,
                    },
                    dedup_key=_dedup_key("linkedin_reply", message_id),
                )
                db.add(signal)
                signals.append(signal)

    db.commit()
    return signals


def _parse_unix_time(value) -> datetime | None:
    """Hacker News' own `time` field is a Unix timestamp integer, not an ISO string -- every
    other source's raw payload uses ISO strings, which is exactly what the shared _parse_dt()
    above already handles, so this is a small, source-specific helper rather than a change to
    that shared one. Returns None rather than raising, same discipline as _parse_dt."""
    if not value:
        return None
    try:
        return datetime.utcfromtimestamp(int(value))
    except (TypeError, ValueError, OSError):
        return None


def sense_hackernews_stories(db: Session, tenant_id: int, limit: int = 100) -> list[GtmSignal]:
    """Content Intelligence's first external source (Step 16C) -- Hacker News' official,
    free, unauthenticated API (app/hackernews_client.py). Part of the SAME shared GtmSignal
    architecture as every other adapter in this file; produces raw observations only -- no
    topic/trend classification, no Problem/Demand logic, nothing beyond "this story exists and
    matched a configured topic."

    TOPIC FILTERING (Step 16A's content_intelligence_topics, read via
    app/gtm_os/content/topics.py): if a tenant has zero enabled topics configured, this function
    senses NOTHING and returns an empty list immediately -- an empty topic list means "we
    haven't told the system what to look for yet," and sensing every top story indiscriminately
    would misrepresent that as a real decision. This is deliberately the SIMPLEST reasonable use
    of the topic config: a single deterministic substring match (match_topic(), reused from
    content/topics.py, not duplicated here) against each candidate story's title+text. This is
    NOT topic clustering/semantic matching -- it's the exact same phrase-matching discipline
    already proven in linkedin_monitor.py's match_keywords(), applied to a new source. Building
    anything more sophisticated (embeddings, semantic topic clusters) is explicitly out of scope
    for this step, per the Step 16 design doc's own "deterministic keyword matching first"
    recommendation -- not invented here.

    STORIES ONLY, NOT COMMENTS, in this first version -- a deliberate scope boundary, not an
    oversight. Automatically crawling every comment under every matched story would multiply API
    calls and signal volume unpredictably (a single popular story can have hundreds of
    comments); comment sensing is a real, separate future capability, not a config toggle bolted
    onto this first adapter.

    DEDUPLICATION AND MULTI-TENANCY: uses the top-N story-id snapshot (`get_top_story_ids`),
    which changes gradually -- the same story routinely appears across many consecutive sweep
    calls (a story can stay in the top 500 for hours/days), the same structural shape as
    sense_linkedin_replies()'s own documented "returns overlapping results every call" problem
    (Step 11A). So this function applies the SAME kind of pre-insert "already sensed" check --
    but, critically, SCOPED BY (tenant_id, source, source_ref) TOGETHER, not just
    (source, source_ref) the way sense_linkedin_replies() checks it. Hacker News is a single
    global public feed, not a per-tenant private conversation like SalesRobot's -- the exact
    same real story must be independently evaluable, and independently insertable, for every
    tenant whose own configured topics it happens to match. Scoping the dedup check by
    source/source_ref alone (sense_linkedin_replies()'s pattern) would incorrectly let tenant A's
    already-created signal for a story silently block tenant B from ever getting their own
    signal for that same story -- explicitly the bug this docstring exists to document as
    avoided, not the pattern to blindly copy."""
    topics = get_enabled_content_topics(db, tenant_id)
    if not topics:
        return []

    story_ids = get_top_story_ids(limit)
    signals = []
    for story_id in story_ids:
        source_ref = str(story_id)
        already_sensed = (
            db.query(GtmSignal)
            .filter(GtmSignal.tenant_id == tenant_id, GtmSignal.source == "hackernews_story", GtmSignal.source_ref == source_ref)
            .first()
        )
        if already_sensed:
            continue

        try:
            item = get_item(story_id)
        except Exception:  # noqa: BLE001 -- one item's fetch failure must never block the rest of the sweep
            continue
        if not item or item.get("type") != "story" or item.get("deleted") or item.get("dead"):
            continue

        title = item.get("title") or ""
        text = item.get("text") or ""
        matched_topic = match_topic(f"{title} {text}", topics)
        if not matched_topic:
            continue

        signal = GtmSignal(
            tenant_id=tenant_id,
            source="hackernews_story",
            source_ref=source_ref,
            signal_type="story",
            observed_at=_parse_unix_time(item.get("time")),
            person_name_raw=item.get("by"),
            raw_evidence=item,
            extracted_info={
                "title": title or None,
                "text": text or None,
                "author": item.get("by"),
                "score": item.get("score"),
                "comment_count": item.get("descendants"),
                "url": item.get("url"),
                "matched_topic": matched_topic["name"],
            },
            dedup_key=_dedup_key("hackernews_story", source_ref),
        )
        db.add(signal)
        signals.append(signal)

    db.commit()
    return signals


def _parse_feed_time(entry: dict) -> datetime | None:
    """Prefers `published_parsed`, falls back to `updated_parsed` -- both are `time.struct_time`,
    already normalized by feedparser itself from whatever real date format the feed actually
    used (see rss_client.py's own docstring for why that normalization is the reason feedparser
    was chosen over hand-rolled parsing). Returns None rather than inventing a timestamp when
    neither is present, same discipline as every other source's own time parser in this file."""
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    try:
        return datetime(*struct[:6])
    except (TypeError, ValueError):
        return None


def sense_rss_articles(db: Session, tenant_id: int, limit_per_feed: int = 20) -> list[GtmSignal]:
    """Content Intelligence's second external source (Step 16D) -- configured RSS/Atom feeds
    (app/gtm_os/content/feeds.py), fetched via app/rss_client.py. Same shared GtmSignal
    architecture as every other adapter; raw observation only, no topic clustering/trend/
    Content Opportunity logic.

    FEED CONFIGURATION is deliberately its own, separate Parameter-backed store
    (content_intelligence_feeds, feeds.py) -- NOT folded into content_intelligence_topics. A
    topic ("AI Sales Automation") and a feed (a specific blog's URL) are orthogonal: the same
    topic list should apply across every future source (Hacker News, RSS, eventually Reddit/
    News/...), and a feed URL has nothing to do with what topic it might match. Putting
    `rss_url` inside a topic definition would break that reusability the first time a second
    source needed its own per-source config field.

    TOPIC FILTERING: identical policy to sense_hackernews_stories() (Step 16C) -- if a tenant has
    zero enabled topics OR zero enabled feeds, this function senses nothing. Only items that
    match a configured topic (via the same deterministic match_topic() substring matcher, reused,
    not duplicated) become GtmSignal rows.

    UNMATCHED-ITEM DECISION (explicitly evaluated, per the Step 16D spec's own instruction):
    Option A (configured-topic sensing only) vs. Option B (store all feed content broadly, for a
    future topic-discovery pass to mine later). This implementation chooses **Option A**, same as
    Hacker News: Option B would mean ingesting every item from every configured feed regardless
    of relevance, with no volume/retention/cost bound -- directly the "silently ingest unlimited
    unrelated content" outcome this step's spec explicitly warns against. Option B is a real,
    legitimate future capability (semantic/topic-discovery work explicitly deferred to a later
    step) but would need its own deliberate volume/retention design, not a side effect of this
    adapter reusing Option A's off-the-shelf configuration.

    MULTI-TENANCY AND DEDUPLICATION: identical reasoning to sense_hackernews_stories() -- RSS
    feeds are a global source, not a per-tenant private one, so the "already sensed" check is
    scoped by (tenant_id, source, source_ref) TOGETHER, letting the same real article
    independently become a signal for every tenant whose own topics match it, without one
    tenant's row blocking another's.

    source_ref is the feed's own GUID when present, falling back to the article URL -- RSS/Atom
    GUIDs are the closest thing to a stable per-item identifier the format offers, but not every
    feed sets one.

    SYNDICATION IS EXPLICITLY OUT OF SCOPE HERE, per the Step 16D spec's own architectural
    boundary: the same article republished across two different feeds/publishers produces TWO
    separate GtmSignal rows (different source_ref values, since each feed's own GUID/URL differs)
    -- this is deliberately preserved as two pieces of evidence, not deduplicated, because
    detecting "these are actually the same underlying article" is a future content-similarity
    problem (Step 16 design doc Part 10), not something a same-source-ref dedup key can or should
    solve. What IS preserved here, for that future layer to use: `extracted_info["publisher"]`
    and `feed_url` on every signal -- the provenance a future independence/diversity calculation
    will need (Step 15 Part 8's own "10 copies of one source ≠ 10 independent points" principle,
    now extended to "10 publishers syndicating one article ≠ 10 independent publishers" -- not
    solved here, just not made unsolvable by this adapter's own field choices)."""
    topics = get_enabled_content_topics(db, tenant_id)
    if not topics:
        return []

    feeds = get_enabled_content_feeds(db, tenant_id)
    if not feeds:
        return []

    signals = []
    for feed in feeds:
        feed_url = feed["url"]
        feed_name = feed.get("name") or feed_url
        try:
            entries = fetch_feed_entries(feed_url)
        except Exception:  # noqa: BLE001 -- one feed's failure must never block the others
            continue

        for entry in entries[:limit_per_feed]:
            source_ref = entry.get("guid") or entry.get("link")
            if not source_ref:
                continue

            already_sensed = (
                db.query(GtmSignal)
                .filter(GtmSignal.tenant_id == tenant_id, GtmSignal.source == "rss_article", GtmSignal.source_ref == source_ref)
                .first()
            )
            if already_sensed:
                continue

            title = entry.get("title") or ""
            summary = entry.get("summary") or ""
            content = entry.get("content") or ""
            matched_topic = match_topic(f"{title} {summary} {content}", topics)
            if not matched_topic:
                continue

            signal = GtmSignal(
                tenant_id=tenant_id,
                source="rss_article",
                source_ref=source_ref,
                signal_type="article",
                observed_at=_parse_feed_time(entry),
                person_name_raw=entry.get("author"),
                # NOT company_name_raw=feed_name -- the publisher is who SENSED the content, not
                # a company this evidence is ABOUT (that field means something specific
                # elsewhere in this schema; a publisher name would misuse it). Publisher identity
                # lives in extracted_info only, per this function's own docstring.
                raw_evidence={"feed_url": feed_url, "feed_name": feed_name, "entry": entry},
                extracted_info={
                    "title": title or None,
                    "summary": summary or None,
                    "content": content or None,
                    "author": entry.get("author"),
                    "publisher": feed_name,
                    "feed_url": feed_url,
                    "article_url": entry.get("link"),
                    "categories": entry.get("tags"),
                    "matched_topic": matched_topic["name"],
                },
                dedup_key=_dedup_key("rss_article", source_ref),
            )
            db.add(signal)
            signals.append(signal)

    db.commit()
    return signals


def sense_website_visitors(db: Session, tenant_id: int, limit: int = 50) -> list[GtmSignal]:
    """Channels Intelligence step 4 (2026-08-27) -- feeds the already-built, already-running
    IP-identified website-visitor tracking (app/website_visitor_tracking.py's WebsiteVisitor
    table, populated independently by the marketing site's tracking snippet) into the main GTM-OS
    sensing pipeline as a real signal source. Same shared GtmSignal architecture as every other
    adapter in this file.

    ONLY company-resolved visits are sensed (company_lookup_status == "resolved") -- an
    unresolved IP carries no attributable company, nothing to sense. Deliberately does NOT filter
    by is_fuzzy_match: even a probabilistic company match is still real, better-than-nothing
    evidence that some real company visited -- but see the evidence-tier note below for why that
    uncertainty is handled downstream, not by dropping the row here.

    DELIBERATELY WEAK EVIDENCE, BY DESIGN: a single page visit says "someone at this company
    looked at our site," not "this company has a real problem/demand" -- nowhere near as strong
    as a job posting or a declared problem statement. event_type "website_visit" is deliberately
    left OUT of problem_detection.py's EVENT_TYPE_TIERS, so it falls through to
    DEFAULT_EVIDENCE_TIER ("contextual") -- the same treatment hiring_activity and
    market_pattern_observation already get. Per OPENING_TIERS, contextual evidence can never open
    a new hypothesis by itself; it can only support/boost one that already exists from stronger
    evidence. This is the intended behavior, not a gap -- a company visiting the site should
    realistically raise confidence on an existing signal, never single-handedly manufacture an
    Opportunity.

    COMPANY RESOLUTION: unlike every other source here (which only ever sets company_name_raw and
    lets resolve_company_for_signal()'s name-matching run later), this sensor does its own
    domain-based match against Company.domain (tenant-scoped via Batch, the same join pattern
    used throughout this codebase) at sense time, when the visitor's own resolved company_domain
    exactly matches an existing Company. An exact domain match is a materially stronger identity
    signal than name matching (company names collide; registered domains essentially don't), so
    reusing the same fuzzy name-matching machinery here would be a real downgrade, not a reuse of
    a good pattern. Setting company_id directly makes resolve_company_for_signal() take its own
    documented "already explicitly resolved" fast path -- no new resolution capability invented,
    just an earlier, more reliable way of populating the field it already knows how to trust.

    DEDUPLICATION: source_ref is the WebsiteVisitor row's own id -- each page-view beacon is a
    genuinely distinct, one-time event (not a recurring feed item that might update), so, unlike
    the HN/RSS sources above, this dedup check is scoped by source_ref alone within this tenant's
    own visitor rows -- there is no cross-tenant sharing concern here since WebsiteVisitor rows
    are already tenant-scoped at write time (app/website_visitor_tracking.py's record_visit)."""
    from app.db.models import Batch, Company, WebsiteVisitor

    visitors = (
        db.query(WebsiteVisitor)
        .filter(WebsiteVisitor.tenant_id == tenant_id, WebsiteVisitor.company_lookup_status == "resolved")
        .order_by(WebsiteVisitor.created_at.desc())
        .limit(limit)
        .all()
    )

    signals = []
    for visitor in visitors:
        source_ref = str(visitor.id)
        already_sensed = (
            db.query(GtmSignal)
            .filter(GtmSignal.tenant_id == tenant_id, GtmSignal.source == "website_visitor", GtmSignal.source_ref == source_ref)
            .first()
        )
        if already_sensed:
            continue

        company_id = None
        if visitor.company_domain:
            match = (
                db.query(Company)
                .join(Batch, Company.batch_id == Batch.id)
                .filter(Batch.tenant_id == tenant_id, Company.domain.ilike(visitor.company_domain))
                .first()
            )
            if match:
                company_id = match.id

        signal = GtmSignal(
            tenant_id=tenant_id,
            source="website_visitor",
            source_ref=source_ref,
            signal_type="page_visit",
            observed_at=visitor.created_at,
            company_id=company_id,
            company_name_raw=visitor.company_name,
            raw_evidence={
                "ip_address": visitor.ip_address,
                "page_path": visitor.page_path,
                "referrer": visitor.referrer,
                "user_agent": visitor.user_agent,
                "is_fuzzy_match": visitor.is_fuzzy_match,
            },
            extracted_info={
                "page_path": visitor.page_path,
                "referrer": visitor.referrer,
                "company_domain": visitor.company_domain,
                "company_website": visitor.company_website,
                "company_industry": visitor.company_industry,
                "company_employee_range": visitor.company_employee_range,
                "is_fuzzy_match": visitor.is_fuzzy_match,
            },
            dedup_key=_dedup_key("website_visitor", source_ref),
        )
        db.add(signal)
        signals.append(signal)

    db.commit()
    return signals


def sense_web_search_trends(db: Session, tenant_id: int, limit: int = 20) -> list[GtmSignal]:
    """Content Intelligence -- the "trend" leg (targeting the real ~60% of the content mix,
    2026-08-28 explicit instruction). No Reddit/X/Perplexity API integration exists in this
    codebase (confirmed by direct search) -- a real, already-working Google Search integration
    (search_google_organic_results, the same Apify actor free_decision_maker.py/
    reverse_discovery.py already use) stands in for those platforms until real API keys for them
    exist. Same shared GtmSignal architecture as every other adapter in this file -- feeds
    directly into the EXISTING, unmodified topic_linking.py -> candidate_extraction.py ->
    promotion.py -> trend_intelligence.py pipeline (that pipeline is already source-agnostic, see
    topic_linking.py's own module docstring, so it needed zero changes for this).

    ONE real query per enabled topic (get_enabled_content_topics, the same config HN/RSS sensing
    already reads) -- real cost $0.0055/query (GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD).
    `limit` bounds the number of topics processed per call, same "no runaway cost as config grows"
    discipline as every budget-bounded sensing function in this codebase.

    DEDUPLICATION: scoped by (tenant_id, source, source_ref) together, source_ref is the result's
    own URL -- same reasoning as sense_hackernews_stories()'s own docstring: a real article/page
    can legitimately be relevant to more than one of this tenant's topics, but the SAME topic
    searching the SAME query and getting the SAME URL again should never re-create a signal.

    NO observed_at: this actor's organicResults items carry no reliable publish-date field (only
    url/title/description, confirmed live) -- left null rather than inventing one, same
    discipline as every other source's own time parser in this file."""
    topics = get_enabled_content_topics(db, tenant_id)[:limit]
    if not topics:
        return []

    api_key = _get_apify_api_key(db, tenant_id)
    signals = []
    for topic in topics:
        query = topic["name"]
        try:
            results = search_google_organic_results(api_key, query, max_pages=1)
        except ApifyError:
            continue  # this topic's query failed -- picked up again next sweep, other topics unaffected

        for result in results:
            url = result.get("url")
            if not url:
                continue
            source_ref = url
            already_sensed = (
                db.query(GtmSignal)
                .filter(GtmSignal.tenant_id == tenant_id, GtmSignal.source == "web_search_trend", GtmSignal.source_ref == source_ref)
                .first()
            )
            if already_sensed:
                continue

            signal = GtmSignal(
                tenant_id=tenant_id,
                source="web_search_trend",
                source_ref=source_ref,
                signal_type="search_result",
                observed_at=None,
                raw_evidence=result,
                extracted_info={
                    "title": result.get("title"),
                    "summary": result.get("description"),
                    "url": url,
                    "matched_keyword": query,
                },
                dedup_key=_dedup_key("web_search_trend", source_ref),
            )
            db.add(signal)
            signals.append(signal)

    db.commit()
    return signals


def sense_competitor_content(db: Session, tenant_id: int, limit: int = 30) -> list[GtmSignal]:
    """Content Intelligence -- the "competitor" leg (targeting the real ~40% of the content mix,
    2026-08-28 explicit instruction). Same Google Search mechanism as sense_web_search_trends()
    above, but each query is scoped to one real, named competitor's own domain via a `site:`
    search operator, so results are actually that competitor's own content, not just anything
    mentioning them. Competitor list is app/gtm_os/content/competitors.py's own config (separate
    from topics.py, same reasoning feeds.py's own separation documents).

    `limit` bounds the total number of real (topic, competitor) query pairs made per call --
    real cost $0.0055/query, and the full cross product (topics x competitors) can legitimately
    exceed a sensible per-run budget as either list grows. Iterates topic-major (topic 1 against
    every competitor, then topic 2, ...) -- if capped, later topics simply aren't covered this
    run and are picked up on a subsequent one; this does NOT guarantee even coverage across
    topics when capped, which is an accepted tradeoff, not a silent gap (documented here rather
    than hidden).

    Every other property (dedup by (tenant_id, source, source_ref), extracted_info shape, no
    invented observed_at, per-query error isolation) is identical to sense_web_search_trends()
    above -- see that function's own docstring."""
    topics = get_enabled_content_topics(db, tenant_id)
    competitors = get_enabled_content_competitors(db, tenant_id)
    if not topics or not competitors:
        return []

    api_key = _get_apify_api_key(db, tenant_id)
    signals = []
    queries_made = 0
    for topic in topics:
        for competitor in competitors:
            if queries_made >= limit:
                return signals  # real per-run budget reached -- see docstring's coverage tradeoff note
            queries_made += 1

            query = f"site:{competitor['domain']} {topic['name']}"
            try:
                results = search_google_organic_results(api_key, query, max_pages=1)
            except ApifyError:
                continue

            for result in results:
                url = result.get("url")
                if not url:
                    continue
                source_ref = url
                already_sensed = (
                    db.query(GtmSignal)
                    .filter(GtmSignal.tenant_id == tenant_id, GtmSignal.source == "competitor_content", GtmSignal.source_ref == source_ref)
                    .first()
                )
                if already_sensed:
                    continue

                signal = GtmSignal(
                    tenant_id=tenant_id,
                    source="competitor_content",
                    source_ref=source_ref,
                    signal_type="search_result",
                    observed_at=None,
                    company_name_raw=competitor["name"],
                    raw_evidence=result,
                    extracted_info={
                        "title": result.get("title"),
                        "summary": result.get("description"),
                        "url": url,
                        "matched_keyword": topic["name"],
                        "competitor_name": competitor["name"],
                        "competitor_domain": competitor["domain"],
                    },
                    dedup_key=_dedup_key("competitor_content", source_ref),
                )
                db.add(signal)
                signals.append(signal)

    db.commit()
    return signals
