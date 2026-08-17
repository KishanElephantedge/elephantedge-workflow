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
history on every call, unlike every other source's small-incremental-results shape)."""

import hashlib
from datetime import datetime

from sqlalchemy.orm import Session

from app.apify_client import _get_api_key as _get_apify_api_key
from app.apify_client import search_google_ai_overview, search_linkedin_jobs, search_linkedin_posts
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
from app.rss_client import fetch_feed_entries


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
        signal = GtmSignal(
            tenant_id=tenant_id,
            source="linkedin_job",
            source_ref=source_ref,
            signal_type="job_posting",
            observed_at=_parse_dt(item.get("postedAt") or item.get("datePosted")),
            company_name_raw=item.get("organizationName") or item.get("companyName"),
            raw_evidence=item,
            extracted_info={
                "title": item.get("title"),
                "location": item.get("location"),
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
      - company_name_raw <- parsed from author.occupation via the SAME deterministic
        _guess_company_name() parser reverse_discovery.py already uses for this exact purpose
        (occupation is a free-text "Title at Company" string, only reliably populated when the
        actor scrapes a profile directly -- true here, since this function is always called with
        real profile_urls, unlike reverse_discovery's broad keyword-search path). This is a raw,
        unverified guess (matching the field's own "_raw" naming), not a resolved company_id --
        no paid verification call (Jobo/Google ICP-floor check) is made here, keeping this layer
        purely observational and free, per the sensing layer's own discipline.
    occupation and author_profile_url are also preserved in extracted_info -- real source-level
    identifiers a later, not-yet-built entity-resolution layer will need, not derived judgments."""
    api_key = _get_apify_api_key(db, tenant_id)
    items = search_linkedin_posts(api_key, profile_urls, scrape_until=scrape_until, limit_per_source=limit_per_source)
    signals = []
    for item in items:
        source_ref = str(item.get("urn") or item.get("postUrl") or item.get("url") or "")
        if not source_ref:
            continue
        author = item.get("author") or {}
        occupation = author.get("occupation") or ""
        signal = GtmSignal(
            tenant_id=tenant_id,
            source="linkedin_post",
            source_ref=source_ref,
            signal_type="post",
            observed_at=_parse_dt(item.get("postedAtISO") or item.get("postedAt") or item.get("date")),
            person_name_raw=item.get("authorName") or item.get("author"),
            company_name_raw=_guess_company_name(occupation),
            raw_evidence=item,
            extracted_info={
                "text": item.get("text") or item.get("content"),
                "occupation": occupation or None,
                "author_profile_url": item.get("authorProfileUrl") or item.get("inputUrl"),
            },
            dedup_key=_dedup_key("linkedin_post", source_ref),
        )
        db.add(signal)
        signals.append(signal)
    db.commit()
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
    reply_prospect_uuids: list[str] = []
    for campaign_uuid in campaign_uuids:
        page = 0
        while True:
            try:
                result = get_campaign_prospects(campaign_uuid, linkedin_account_uuid, db, tenant_id, page=page, size=100)
            except SalesRobotError:
                break
            prospects = result.get("data", {}).get("data", [])
            for p in prospects:
                if p.get("isReplied") and p.get("prospectUuid"):
                    reply_prospect_uuids.append(p["prospectUuid"])
            if len(prospects) < 100:
                break
            page += 1

    signals: list[GtmSignal] = []
    for prospect_uuid in reply_prospect_uuids:
        try:
            raw_response = get_synced_messages(
                linkedin_account_uuid, db, tenant_id, prospect_uuid_filter=[prospect_uuid], size=1,
            )
        except SalesRobotError:
            continue

        for thread in _extract_thread_pages(raw_response):
            thread_id = thread.get("threadId")
            prospect_data = thread.get("prospectData") or {}
            vanity_profile_url = prospect_data.get("profileUrl")  # NOT thread.get("profileUrl") -- see docstring
            company_name_raw = prospect_data.get("companyName")
            person_name_raw = thread.get("nameOfPerson")
            campaign_uuid = thread.get("campaignUuid")
            campaign_name = thread.get("campaignName")

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
