"""Channels Intelligence step 7 (2026-08-27) -- visibility into Majji's OWN LinkedIn content
output, for the "LinkedIn content" channel. Deliberately NOT built on top of
app/phases/linkedin_monitor.py's LinkedinMonitorProfile/LinkedinMonitorSignal tables, even though
this reuses that same module's underlying Apify call (search_linkedin_posts, the confirmed real,
working `supreme_coder/linkedin-post` actor) -- that table's whole surrounding machinery is
purpose-built for a DIFFERENT job (competitor/partner ecosystem watching): a keyword taxonomy
scored against the AI-SDR positioning, an LLM relevance screen judging whether a match is a real
signal, GTM partner classification, and a Slack alert reading "GTM Signal Detected." None of that
makes sense applied to Majji's own content -- there is no keyword to filter on (every post he
makes is relevant by definition), no "is this actually about AI SDR" judgment to make about his
own words, no partner classification to run on himself, and no reason to alert him that he just
posted. Reusing that table would silently misuse a positioning-specific data model as a generic
one, exactly the kind of conflation this codebase's own conventions (see problem_hypothesis.py,
company_resolution.py) warn against elsewhere.

WHAT THIS IS NOT: real per-post ENGAGEMENT (likes/comments/reactions, and who they came from) is
explicitly out of scope here -- LinkedIn's officially-grantable API products don't cover
personal-profile post analytics (Community Management API is Company-Page-scoped only; Member
Data Portability API is EU/EEA/Switzerland-member-only, and Majji is India-based), and no Apify
actor for scraping real per-post engager identities has been evaluated or confirmed to work in
this codebase. Fabricating a "likes_count"/"engagers" field from an unconfirmed actor schema
would misrepresent invented numbers as real data -- raw_evidence stores the actor's full,
untouched response instead, so any real engagement-shaped fields it happens to return are
preserved for a future, deliberately-verified integration, never silently parsed and trusted.

Real revenue ATTRIBUTION for this channel is already fully covered by the existing, shipped
outcome_channel field (a human self-reports "linkedin_content" when recording a meeting's
outcome) -- this module only adds visibility into whether/how often Majji is actually posting,
not automated lead capture from his content."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.apify_client import ApifyError, search_linkedin_posts
from app.apify_client import _get_api_key as _get_apify_api_key
from app.db.models import OwnLinkedinPost, Parameter

OWN_PROFILE_URL_PARAMETER_KEY = "own_linkedin_profile_url"


def get_own_linkedin_profile_url(db: Session, tenant_id: int) -> str | None:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == OWN_PROFILE_URL_PARAMETER_KEY)
        .first()
    )
    return param.value if param and isinstance(param.value, str) and param.value.strip() else None


def set_own_linkedin_profile_url(db: Session, tenant_id: int, url: str) -> None:
    if not url or not url.strip():
        raise ValueError("own LinkedIn profile url must be a non-empty string")
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == OWN_PROFILE_URL_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = url.strip()
    else:
        param = Parameter(tenant_id=tenant_id, key=OWN_PROFILE_URL_PARAMETER_KEY, value=url.strip(), description="Majji's own LinkedIn profile URL, for LinkedIn content channel tracking")
        db.add(param)
    db.commit()


def get_recent_own_posts(db: Session, tenant_id: int, limit: int = 20) -> list[OwnLinkedinPost]:
    return (
        db.query(OwnLinkedinPost)
        .filter(OwnLinkedinPost.tenant_id == tenant_id)
        .order_by(OwnLinkedinPost.posted_at.desc().nullslast(), OwnLinkedinPost.created_at.desc())
        .limit(limit)
        .all()
    )


def sync_own_linkedin_posts(db: Session, tenant_id: int, limit: int = 10) -> dict:
    """On-demand sync (not on any recurring schedule -- deliberately manual, real Apify spend
    each call, ~$0.002/post scraped, same real pricing as linkedin_monitor.py's own sweep). Returns
    {"status": "not_configured" | "ok" | "apify_error", "new_posts": <int>, "error": <str|None>}.

    Real dedup: post_urn unique per tenant (ix_own_linkedin_posts_tenant_urn) -- a re-sync that
    sees the same post again inserts nothing new, same discipline as every other sensing source
    in this codebase."""
    profile_url = get_own_linkedin_profile_url(db, tenant_id)
    if not profile_url:
        return {"status": "not_configured", "new_posts": 0, "error": None}

    api_key = _get_apify_api_key(db, tenant_id)
    try:
        posts = search_linkedin_posts(api_key, [profile_url], limit_per_source=limit)
    except ApifyError as e:
        return {"status": "apify_error", "new_posts": 0, "error": str(e)}

    new_count = 0
    for post in posts:
        urn = post.get("urn")
        if not urn:
            continue
        already_seen = (
            db.query(OwnLinkedinPost)
            .filter(OwnLinkedinPost.tenant_id == tenant_id, OwnLinkedinPost.post_urn == urn)
            .first()
        )
        if already_seen:
            continue

        posted_at = None
        if post.get("postedAtISO"):
            try:
                posted_at = datetime.fromisoformat(post["postedAtISO"].replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                pass

        db.add(OwnLinkedinPost(
            tenant_id=tenant_id,
            post_urn=urn,
            post_url=post.get("url"),
            post_text=post.get("text"),
            posted_at=posted_at,
            raw_evidence=post,
        ))
        new_count += 1

    db.commit()
    return {"status": "ok", "new_posts": new_count, "error": None}
