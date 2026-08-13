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
2026-08-12) -- deliberately simple substring matching for v1, not an LLM relevance classifier.
The colleague's research recommended an AI classifier (to catch paraphrases like "the SDR as
we knew it is disappearing" without keyword "AI SDR") and to reject false positives like "We're
hiring an SDR" -- worth adding as a second pass once the keyword version is proven live, not
before, per the same lesson learned on decision-maker verification: an LLM layer with no
evidence discipline can produce dangerous false positives, so it needs to be built and tested
deliberately, not bolted on speculatively."""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.apify_client import ApifyError, search_linkedin_posts
from app.apify_client import _get_api_key as _get_apify_api_key
from app.db.models import LinkedinMonitorProfile, LinkedinMonitorSignal
from app.notifications import create_notification
from app.slack_client import SlackError, send_slack_message

KEYWORD_TIERS: dict[str, list[str]] = {
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


def match_keywords(text: str) -> list[tuple[str, str]]:
    """Case-insensitive substring match against every tier -- returns [(tier, keyword), ...]
    for every keyword found, not just the first."""
    text_lower = (text or "").lower()
    hits = []
    for tier, keywords in KEYWORD_TIERS.items():
        for keyword in keywords:
            if keyword.lower() in text_lower:
                hits.append((tier, keyword))
    return hits


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
    sweep (its last_checked_at is left untouched), not marked as checked."""
    profiles = (
        db.query(LinkedinMonitorProfile)
        .filter(LinkedinMonitorProfile.tenant_id == tenant_id)
        .filter(LinkedinMonitorProfile.active.is_(True))
        .all()
    )
    if not profiles:
        return {"profiles_checked": 0, "signals_found": 0}

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
            hits = match_keywords(post.get("text") or "")
            if not hits:
                continue

            posted_at = None
            if post.get("postedAtISO"):
                try:
                    posted_at = datetime.fromisoformat(post["postedAtISO"].replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    pass

            signal = LinkedinMonitorSignal(
                tenant_id=tenant_id, profile_id=profile.id, post_urn=urn,
                post_url=post.get("url"), post_text=post.get("text"),
                author_name=post.get("authorName"), posted_at=posted_at,
                matched_keywords=[kw for _, kw in hits],
                tier=min({tier for tier, _ in hits}),  # most senior (lowest tier number) wins
            )
            db.add(signal)
            db.commit()
            signals_found += 1

            try:
                send_slack_message(_format_slack_alert(profile, post, hits), db, tenant_id)
                signal.alerted_at = datetime.utcnow()
                db.commit()
            except SlackError:
                pass
            create_notification(
                db, tenant_id, "linkedin_signal",
                f"GTM signal: {profile.name or profile.linkedin_url}",
                f"Matched {', '.join(sorted({kw for _, kw in hits}))} -- {post.get('url') or ''}",
                severity="info",
            )

        for profile in batch:
            profile.last_checked_at = now
            profiles_checked += 1
        db.commit()

    return {"profiles_checked": profiles_checked, "signals_found": signals_found}
