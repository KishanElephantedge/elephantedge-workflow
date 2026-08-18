"""GTM signal monitor -- watches a fixed list of LinkedIn profiles (competitors, partners,
ecosystem people) for new posts, matches post text against a tiered keyword taxonomy, and
alerts (Slack + dashboard notification) on a hit. Not prospecting -- this is "did someone in
our ecosystem just publicly say something relevant to Elephant Edge's positioning right now,"
so we can engage while it's fresh.

Uses Apify's supreme_coder/linkedin-post actor directly (NOT through Deepline -- Deepline
Monitors, which would do this natively, is access-gated and this account doesn't have it,
confirmed live 2026-08-12). Real, confirmed pricing: ~$0.002/post scraped, not billed per
profile checked -- `scrape_until` is set to each profile's own last-checked time, so a profile
with no new posts since the last sweep costs close to nothing.

Keyword taxonomy provided by the user (sourced from a colleague's ChatGPT research,
2026-08-12) -- simple substring matching finds candidates, then an LLM relevance screen
(added 2026-08-13, after a real confirmed false positive live: a Tier 4 "partnership" keyword
matched a post about evaluating business partnerships in general, nothing to do with AI SDR)
judges whether the post is ACTUALLY about AI SDR/sales-automation before anything gets
alerted. Every keyword match is still stored either way (never silently dropped, same
never-delete-just-flag pattern as scoring/verification elsewhere in this codebase) -- only the
Slack/notification alert is gated on the classifier's judgment, so a human can always audit
what got filtered and why via the Targets tab."""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.apify_client import ApifyError, search_linkedin_posts
from app.apify_client import _get_api_key as _get_apify_api_key
from app.db.models import LinkedinMonitorProfile, LinkedinMonitorSignal, Parameter
from app.llm_client import generate_json
from app.notifications import create_notification
from app.slack_client import SlackError, send_slack_message

KEYWORDS_PARAMETER_KEY = "linkedin_monitor_keywords"

# Seed default, used only the first time (no Parameter row yet) -- from then on, the DB row is
# the source of truth and is fully editable from the dashboard's Targets > Settings tab, not
# this constant. Kept here only as the original starting taxonomy, not a hardcoded ceiling.
DEFAULT_KEYWORD_TIERS: dict[str, list[str]] = {
    "Tier 1: Direct signals": [
        "AI SDR", "AI Sales Development Representative", "Autonomous SDR", "Autonomous Sales",
        "AI outbound", "AI prospecting", "AI sales agent", "Sales Agent", "SDR Agent",
        "AI prospecting agent",
    ],
    "Tier 2: Workflow signals": [
        "Sales automation", "Sales workflow", "Automated prospecting", "Automated outbound",
        "Lead qualification automation", "Automated follow-up", "Sales orchestration",
        "GTM automation", "Revenue automation", "AI sales workflow",
    ],
    "Tier 3: Strategic signals": [
        "SDR replacement", "SDR productivity", "Future of SDR", "Autonomous revenue",
        "AI replacing SDRs", "AI replacing sales teams", "Agentic sales", "Agentic GTM",
        "AI-first sales organization",
    ],
    "Tier 4: Competitive signals": [
        "competitor mentions", "new AI SDR product", "new AI sales agent",
        "new outbound product", "product launch", "pricing announcement",
        "customer case study", "funding announcement", "partnership",
    ],
}

# First sweep for a never-checked profile looks back this far -- avoids either re-alerting a
# profile's entire posting history on day one, or missing everything if we set no lookback at
# all (the actor's own default with no scrapeUntil is "no date filter", i.e. potentially years
# of posts).
INITIAL_LOOKBACK_DAYS = 2

# Batched into groups, not one giant call for all profiles -- keeps each Apify call well under
# its own timeout and keeps a single slow/broken profile URL from failing the whole sweep.
PROFILES_PER_BATCH = 20


def get_keyword_tiers(db: Session, tenant_id: int) -> dict[str, list[str]]:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == KEYWORDS_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, dict) and param.value:
        return param.value
    return DEFAULT_KEYWORD_TIERS


def set_keyword_tiers(db: Session, tenant_id: int, tiers: dict[str, list[str]]) -> None:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == KEYWORDS_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = tiers
    else:
        param = Parameter(tenant_id=tenant_id, key=KEYWORDS_PARAMETER_KEY, value=tiers, description="GTM signal monitor keyword taxonomy, editable from Targets > Settings")
        db.add(param)
    db.commit()


SCHEDULE_PARAMETER_KEY = "linkedin_monitor_schedule"

# Historical default -- this was hardcoded as `minutes=45` directly in main.py's
# scheduler.add_job() call until 2026-08-18, with no way to change it without a code change/
# redeploy, and the "Poll Interval" stat on the Targets page just displayed this same number as
# a static string rather than reading anything real. Kept as the fallback default only; the
# real, current interval is whatever's stored via set_monitor_schedule() below.
DEFAULT_SCHEDULE = {"days": 0, "hours": 0, "minutes": 45, "enabled": True}

# Real safety floor -- prevents an accidental 0/1-minute interval from hammering the Apify
# actor (and this tenant's Apify spend) every time the scheduler ticks. 5 minutes is well
# under any of this monitor's real use cases (checking a LinkedIn profile for new posts) but
# still guards against a fat-fingered save.
MIN_SCHEDULE_MINUTES = 5


class ScheduleConfigError(ValueError):
    """Raised when a schedule save fails validation -- never silently clamped or coerced."""


def get_monitor_schedule(db: Session, tenant_id: int) -> dict:
    """Returns {"days", "hours", "minutes", "enabled", "interval_minutes"} -- the first four are
    exactly what a human edits (see Targets > Settings); interval_minutes is the same value
    pre-computed as a single total, for callers (main.py's scheduler) that just need one
    number, so that arithmetic lives in exactly one place."""
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == SCHEDULE_PARAMETER_KEY)
        .first()
    )
    schedule = dict(param.value) if param and isinstance(param.value, dict) and param.value else dict(DEFAULT_SCHEDULE)
    for key, default in DEFAULT_SCHEDULE.items():
        schedule.setdefault(key, default)
    schedule["interval_minutes"] = schedule["days"] * 1440 + schedule["hours"] * 60 + schedule["minutes"]
    return schedule


def set_monitor_schedule(db: Session, tenant_id: int, days: int, hours: int, minutes: int, enabled: bool) -> dict:
    """Validates and stores the real schedule a human configured, then returns it (via
    get_monitor_schedule(), same shape every reader expects, including interval_minutes)."""
    for name, value in (("days", days), ("hours", hours), ("minutes", minutes)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ScheduleConfigError(f"{name!r} must be a non-negative integer")
    if not isinstance(enabled, bool):
        raise ScheduleConfigError("'enabled' must be true/false")
    total_minutes = days * 1440 + hours * 60 + minutes
    if total_minutes < MIN_SCHEDULE_MINUTES:
        raise ScheduleConfigError(f"interval must be at least {MIN_SCHEDULE_MINUTES} minutes total (got {total_minutes})")

    value = {"days": days, "hours": hours, "minutes": minutes, "enabled": enabled}
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == SCHEDULE_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = value
    else:
        param = Parameter(tenant_id=tenant_id, key=SCHEDULE_PARAMETER_KEY, value=value, description="LinkedIn monitor poll interval + pause/resume, editable from Targets > Settings")
        db.add(param)
    db.commit()
    return get_monitor_schedule(db, tenant_id)


UNFILTERED_TIER = "Unfiltered"
UNFILTERED_KEYWORD = "(no keyword filter configured -- capturing all activity)"


def is_unfiltered(keyword_tiers: dict[str, list[str]]) -> bool:
    """True when every tier's keyword list has been explicitly cleared (all empty). This is
    deliberately distinct from "no Parameter row at all" -- that case still falls back to
    DEFAULT_KEYWORD_TIERS in get_keyword_tiers() and is never treated as unfiltered. Only a real,
    stored, fully-emptied taxonomy means "capture everything, no filter" -- an intentional choice
    made from the Targets > Settings tab, not an accidental/missing-config state."""
    return sum(len(v) for v in keyword_tiers.values()) == 0


def match_keywords(text: str, keyword_tiers: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Case-insensitive substring match against every tier -- returns [(tier, keyword), ...]
    for every keyword found, not just the first. When the taxonomy has been fully cleared
    (is_unfiltered), every post is a match -- an empty filter means "capture all activity," never
    "match nothing" (the previous, silent-failure behavior)."""
    if is_unfiltered(keyword_tiers):
        return [(UNFILTERED_TIER, UNFILTERED_KEYWORD)]
    text_lower = (text or "").lower()
    hits = []
    for tier, keywords in keyword_tiers.items():
        for keyword in keywords:
            if keyword.lower() in text_lower:
                hits.append((tier, keyword))
    return hits


RELEVANCE_PROMPT = """You are screening a LinkedIn post for Elephant Edge, a company that \
builds AI SDR / autonomous outbound sales systems. This post was flagged because it contains \
the keyword(s): {keywords} (taxonomy tier: {tier}). A keyword match alone is not enough --\
common words like "partnership" or "product launch" show up constantly in posts that have \
nothing to do with AI SDR or sales automation (confirmed real case: a post literally titled \
"partnership advice" about evaluating business partners in general, unrelated to AI or sales \
tooling, matched purely on the word "partnership").

Post text:
\"\"\"{post_text}\"\"\"

Judge whether this post is GENUINELY about AI SDRs, autonomous/automated sales, sales \
workflow or GTM automation, or a competing product/company in this exact space -- not just \
using one of the matched words in an unrelated context.

Return JSON exactly:
{{"relevance_score": <0-100 integer>, "recommended_action": "engage" | "monitor" | "ignore", "reason": "<one sentence>"}}

- "engage": directly relevant, worth a human response or engagement angle right now
- "monitor": tangentially relevant, worth knowing about but not urgent
- "ignore": not actually relevant -- matched on keyword text alone, wrong topic"""


def classify_relevance(post_text: str, matched_keywords: list[str], tier: str, db: Session, tenant_id: int) -> dict:
    """Second pass after a keyword match -- returns {relevance_score, recommended_action,
    reason}. Falls back to "monitor" (never silently drops a real match) if the LLM call
    itself fails, since a classifier outage must never be indistinguishable from "confirmed
    irrelevant"."""
    try:
        result = generate_json(
            RELEVANCE_PROMPT.format(keywords=", ".join(matched_keywords), tier=tier, post_text=(post_text or "")[:2000]),
            db, tenant_id, max_tokens=200,
        )
        if "recommended_action" not in result:
            raise ValueError("missing recommended_action")
        return result
    except Exception as e:
        return {"relevance_score": None, "recommended_action": "monitor", "reason": f"classifier unavailable: {e}"}


def _format_slack_alert(profile: LinkedinMonitorProfile, post: dict, hits: list[tuple[str, str]]) -> str:
    tiers = sorted({tier for tier, _ in hits})
    keywords = sorted({kw for _, kw in hits})
    author = post.get("authorName") or profile.name or profile.linkedin_url
    text = (post.get("text") or "").strip()
    excerpt = text[:400] + ("..." if len(text) > 400 else "")
    return (
        f"🔥 *GTM Signal Detected*\n\n"
        f"*{author}*{f' ({profile.company})' if profile.company else ''} just posted.\n\n"
        f"*Matched:* {', '.join(keywords)}\n"
        f"*Signal tier:* {', '.join(tiers)}\n\n"
        f"*Post:*\n{excerpt}\n\n"
        f"*Link:* {post.get('url') or ''}\n"
        f"*Posted:* {post.get('postedAtISO') or ''}"
    )


def run_linkedin_monitor_sweep(db: Session, tenant_id: int) -> dict:
    """Checks every active monitored profile for new posts since its own last_checked_at,
    keyword-matches anything new, and alerts on a hit. Safe to call repeatedly (e.g. every
    30-60 min via the scheduler) -- already-seen posts are deduped by the unique
    (profile_id, post_urn) index, and a profile whose actor call fails is skipped for this
    sweep (its last_checked_at is left untouched), not marked as checked.

    PAUSE (Targets > Settings, 2026-08-18): the scheduler job itself keeps firing on its own
    fixed cadence regardless -- pausing means this function no-ops immediately, same pattern as
    autonomous_orchestrator.py's is_autonomous_enabled() check. Deliberately not an APScheduler
    pause_job()/resume_job() call: that state lives only in the running process's memory and
    would silently reset to "resumed" on every deploy/restart, whereas a DB flag is the real,
    persistent source of truth a human actually set."""
    schedule = get_monitor_schedule(db, tenant_id)
    if not schedule["enabled"]:
        return {"profiles_checked": 0, "signals_found": 0, "status": "paused"}

    profiles = (
        db.query(LinkedinMonitorProfile)
        .filter(LinkedinMonitorProfile.tenant_id == tenant_id)
        .filter(LinkedinMonitorProfile.active.is_(True))
        .all()
    )
    if not profiles:
        return {"profiles_checked": 0, "signals_found": 0}

    keyword_tiers = get_keyword_tiers(db, tenant_id)
    api_key = _get_apify_api_key(db, tenant_id)
    now = datetime.utcnow()
    signals_found = 0
    profiles_checked = 0

    for i in range(0, len(profiles), PROFILES_PER_BATCH):
        batch = profiles[i:i + PROFILES_PER_BATCH]
        # scrapeUntil is a single value per actor call, but each profile in a batch may have a
        # different last_checked_at -- use the OLDEST cutoff in the batch (the actor call still
        # returns some already-seen posts for profiles checked more recently, but the
        # per-post-urn unique index silently no-ops those, so this only costs a few extra
        # scraped posts, never a missed one).
        cutoffs = [p.last_checked_at for p in batch if p.last_checked_at]
        scrape_until = min(cutoffs) if cutoffs else (now - timedelta(days=INITIAL_LOOKBACK_DAYS))
        urls = [p.linkedin_url for p in batch]
        try:
            posts = search_linkedin_posts(api_key, urls, scrape_until=scrape_until.isoformat(), limit_per_source=5)
        except ApifyError:
            continue  # this batch's profiles are left unchecked, picked up again next sweep

        by_url = {p.linkedin_url.rstrip("/").lower(): p for p in batch}
        for post in posts:
            input_url = (post.get("inputUrl") or post.get("authorProfileUrl") or "").rstrip("/").lower()
            profile = by_url.get(input_url)
            if not profile:
                continue
            urn = post.get("urn")
            if not urn:
                continue
            already_seen = (
                db.query(LinkedinMonitorSignal)
                .filter(LinkedinMonitorSignal.profile_id == profile.id)
                .filter(LinkedinMonitorSignal.post_urn == urn)
                .first()
            )
            if already_seen:
                continue
            hits = match_keywords(post.get("text") or "", keyword_tiers)
            if not hits:
                continue

            posted_at = None
            if post.get("postedAtISO"):
                try:
                    posted_at = datetime.fromisoformat(post["postedAtISO"].replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    pass

            matched_kw = [kw for _, kw in hits]
            tier = min({t for t, _ in hits})  # most senior (lowest tier number) wins
            if tier == UNFILTERED_TIER:
                # The AI-SDR relevance screen (classify_relevance) exists to judge whether a
                # KEYWORD MATCH is a real signal or a false positive -- meaningless with no
                # keyword to judge, and would silently classify unrelated real activity as
                # "ignore," muting exactly what unfiltered mode is supposed to surface. Every
                # unfiltered post alerts -- see the "Only 'ignore' ... still alert" comment below.
                classification = {"relevance_score": None, "recommended_action": "monitor", "reason": "unfiltered mode -- no keyword taxonomy configured, capturing and alerting all activity"}
            else:
                classification = classify_relevance(post.get("text") or "", matched_kw, tier, db, tenant_id)

            signal = LinkedinMonitorSignal(
                tenant_id=tenant_id, profile_id=profile.id, post_urn=urn,
                post_url=post.get("url"), post_text=post.get("text"),
                author_name=post.get("authorName"), posted_at=posted_at,
                matched_keywords=matched_kw, tier=tier,
                relevance_score=classification.get("relevance_score"),
                recommended_action=classification.get("recommended_action"),
                classifier_reason=classification.get("reason"),
            )
            db.add(signal)
            db.commit()
            signals_found += 1

            # Only "ignore" is confirmed-irrelevant -- both "engage" and "monitor" (plus the
            # classifier-unavailable fallback, which defaults to "monitor") still alert, since
            # missing a real signal is worse than one extra notification.
            if classification.get("recommended_action") != "ignore":
                try:
                    send_slack_message(_format_slack_alert(profile, post, hits), db, tenant_id)
                    signal.alerted_at = datetime.utcnow()
                    db.commit()
                except SlackError:
                    pass
                create_notification(
                    db, tenant_id, "linkedin_signal",
                    f"GTM signal: {profile.name or profile.linkedin_url}",
                    f"Matched {', '.join(sorted(set(matched_kw)))} -- {post.get('url') or ''}",
                    severity="info",
                )

        for profile in batch:
            profile.last_checked_at = now
            profiles_checked += 1
        db.commit()

    return {"profiles_checked": profiles_checked, "signals_found": signals_found}
