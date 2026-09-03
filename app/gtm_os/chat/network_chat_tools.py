"""Network chat -- ask anything about the GTM partners we know, from both sources at once.

TWO SOURCES, ONE PERSON. We hold each partner twice and neither half is sufficient alone:
  - GTM University (LinkedinMonitorProfile.gtm_university_data): their formal profile -- bio,
    offerings, capabilities, website, stated ICP. Curated, and sometimes stale or empty (110 of
    183 partners have no ICP text there at all).
  - The GTM Partners Slack (SlackCommunityMessage): what they actually say, week to week --
    what they are struggling with, what they charge, which clients they win and lose.
The formal profile says who they claim to be; Slack says what is actually going on. A question
like "I have a meeting with this person, what should I talk about" is only answerable from both.

BOUNDARY. Partners describe THEIR CLIENTS in confidence in #ask-a-question. Nothing here treats
that as a prospect list, and no tool should be added that does -- see the same note in
app/gtm_os/community/slack_community.py. Their clients are not our leads; the partners are the
relationship.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

NETWORK_CHAT_SYSTEM_PROMPT = """You are the GTM network assistant for Elephant Edge. Today is {today}.

Elephant Edge is a certified partner inside GTM Partners' community: ~160 senior operators
(ex-CMOs, CROs, VPs) who have recently started their own fractional/consulting practices. We hold
each of them in two places -- their formal GTM University profile, and everything they have posted
in the community Slack (10,652 messages, 25 channels).

WHAT THIS COMMUNITY IS, established from their own messages -- use it to interpret what you find:
- They already refer work to each other and pay for it; 10% referral fees are discussed openly.
- Their clients want execution, not strategy alone. One partner with 7 years and $5-40M clients:
  "none have wanted strategy only." Most are solo and cannot deliver execution themselves.
- Their pain is selling, not delivering: pricing 319 mentions, prospecting 210, referrals 189,
  delivery only 75.

HOW TO ANSWER -- think, do not look up:
- Read what the tools return and REASON about it. Do not restate the tool output as a list of
  facts. The user can read a profile; what they need is what it means. If someone posts weekly
  outbound scorecards and keeps missing their meetings target, say they are struggling to convert
  activity into conversations -- do not just report the numbers.
- Answer the question that was actually asked. "What should I talk about in this meeting" wants
  two or three specific openers grounded in what that person said recently, not a biography.
- Call more than one tool when it helps. A question about fit is a get_partner_profile AND a
  search_gtm_community about what they are struggling with. Chain them; do not stop at the first
  result.
- Quote them. Real words, with the channel and the date, so the user can check you.
- Notice what is missing or odd, and say so: no ICP on file, no Slack activity in months, a
  profile that says one thing while their posts say another. Contradictions are usually the most
  useful thing on the page.
- Have a view. If asked who fits us best, rank them and give the reason. Hedging everything is
  not neutrality, it is a non-answer.
- Keep any URL exactly as-is so it stays clickable.
- Never guess an identity. If get_partner_profile returns ambiguous=true, show the user the
  options with their titles and companies and ask which one they mean. Answering about the wrong
  person is worse than asking.
- Say plainly when we do not have something. "No ICP on file and nothing in Slack since June" is
  a real, useful answer; inventing plausible detail is not.

A LINE YOU DO NOT CROSS: partners describe their own clients in confidence in #ask-a-question.
Never propose approaching a partner's client, and never present client details as a lead list.
The partners are the relationship; their trust is the asset."""

SLACK_LINK = re.compile(r"<(https?://[^>|]+)(?:\|([^>]*))?>")
SLACK_USER = re.compile(r"<@(U[A-Z0-9]+)>")


def _clean(text: str, names: dict[str, str] | None = None) -> str:
    """Slack markup -> readable text, keeping URLs bare so they render as links."""
    t = SLACK_LINK.sub(lambda m: m.group(1), text or "")
    if names:
        t = SLACK_USER.sub(lambda m: "@" + names.get(m.group(1), m.group(1)), t)
    return t



def _fold(name: str) -> str:
    """Name reduced to a comparable form: accents stripped, punctuation dropped, case and spacing
    normalised.

    SQL ILIKE compares codepoints, so a search for "Isabel Londono" does not match the stored
    "Isabel Londono" -- confirmed live: she is partner 177 and posts in four Slack channels, and
    the assistant answered "no profile data on her" because the tilde did not match. Names in this
    data arrive from three separate sources (a GTM University scrape, Slack profiles, and typing),
    so accents, trailing spaces and punctuation disagree constantly. Postgres unaccent would need
    an extension; folding in Python needs nothing and the candidate sets here are small (183
    partners, 200 Slack users).
    """
    decomposed = unicodedata.normalize("NFKD", name or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", stripped.lower()).split())


def _name_matches(needle: str, haystack: str) -> bool:
    """True when the searched name is a sensible match for a stored one, ignoring accents.

    Substring in either direction, so "Sarah" finds "Sarah Allen-Short" and "Isabel Londono" finds
    "Isabel Londono". Also matches when every word of the shorter name appears in the longer, which
    handles middle names and credentials ("Natasha Sinutko Morgan" vs "Natasha Morgan").
    """
    a, b = _fold(needle), _fold(haystack)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    short, long_ = sorted((a.split(), b.split()), key=len)
    return len(short) > 1 and all(w in long_ for w in short)


NETWORK_CHAT_TOOLS = [
    {
        "name": "get_partner_profile",
        "description": (
            "Everything we know about ONE GTM partner, from both sources: their GTM University "
            "profile (bio, offerings, capabilities, website, stated ICP) AND their real Slack "
            "activity (how much they post, in which channels, and their actual recent messages). "
            "Use this for any question about a specific person -- 'who is Sarah', 'summary of this "
            "guy', 'I have a meeting with X, what should I talk about', 'what is X struggling with'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "partner name or part of it, e.g. 'Sarah' or 'Don Drury'"},
                "message_limit": {"type": "integer", "description": "how many of their messages to return, default 25, max 100"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "search_gtm_community",
        "description": (
            "Search the community Slack (10,652 messages, 25 channels) for real messages. Filter by "
            "text, channel and/or person. Use for 'what are people saying about pricing', 'who is "
            "struggling with outbound', 'what happens in #brand-systems'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "channel": {"type": "string", "description": "e.g. brand-systems, ask-a-question"},
                "person": {"type": "string"},
                "limit": {"type": "integer", "description": "default 25, max 100"},
            },
        },
    },
    {
        "name": "get_gtm_community_overview",
        "description": "Shape of the community: every channel with message counts and date span, plus the most active people and their titles. Good starting point for broad questions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_partners",
        "description": (
            "List/search the partners we track, with how complete their data is and how active they "
            "are in Slack. Use for 'who is most active', 'who has no ICP on file', 'who are the "
            "fractional CROs'. Sort by slack activity or name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "match name, title, company or their stated ICP text"},
                "sort_by": {"type": "string", "enum": ["slack_activity", "name"], "description": "default slack_activity"},
                "limit": {"type": "integer", "description": "default 30, max 100"},
            },
        },
    },
]


def _slack_names(db) -> dict[str, str]:
    from app.gtm_os.community.slack_community import SlackCommunityMessage
    rows = (
        db.query(SlackCommunityMessage.user_id, SlackCommunityMessage.user_name)
        .filter(SlackCommunityMessage.user_name.isnot(None)).distinct().all()
    )
    return {r[0]: r[1] for r in rows if r[0]}


def get_partner_profile(db: Session, tenant_id: int, name: str, message_limit: int = 25) -> dict:
    from app.db.models import LinkedinMonitorProfile
    from app.gtm_os.community.slack_community import SlackCommunityMessage
    from app.phases.partner_icp import get_structured_icp

    # Fold-match over all partners rather than an ILIKE: names disagree on accents, trailing
    # spaces and punctuation across our three sources, and ILIKE compares codepoints. 183 rows.
    candidates = [
        p for p in db.query(LinkedinMonitorProfile).filter(LinkedinMonitorProfile.tenant_id == tenant_id).all()
        if _name_matches(name, p.name or "")
    ]
    # Slack-only people matter here: 9 members post in the community but have no GTM University
    # profile, so a partner-table-only search would report "we have nothing" about someone who is
    # demonstrably active.
    slack_only = [
        n for (n,) in db.query(SlackCommunityMessage.user_name)
        .filter(SlackCommunityMessage.tenant_id == tenant_id, SlackCommunityMessage.user_name.isnot(None))
        .distinct().all()
        if _name_matches(name, n) and not any(_name_matches(n, p.name or "") for p in candidates)
    ]

    # AMBIGUITY IS RETURNED, NOT RESOLVED. "Isabel" matches both Isabel Londono and Isabella
    # (Kalender) Moore, and silently picking the first produced a confident answer about the wrong
    # person. When more than one person matches, hand back the full list with enough to tell them
    # apart and let the human choose -- guessing an identity is the one thing this must not do.
    total_matches = len(candidates) + len(slack_only)
    if total_matches > 1:
        activity = dict(
            db.query(SlackCommunityMessage.user_name, func.count(SlackCommunityMessage.id))
            .filter(SlackCommunityMessage.tenant_id == tenant_id)
            .group_by(SlackCommunityMessage.user_name).all()
        )

        def _msgs(person_name: str) -> int:
            """Message count by folded name -- a direct dict lookup misses whenever the partner
            table and Slack spell the same person differently, which is most of the interesting
            cases (accents, trailing spaces, credentials)."""
            return sum(c for n, c in activity.items() if n and _name_matches(person_name, n))

        options = [
            {"name": (p.name or "").strip(), "title": p.title, "company": p.company,
             "linkedin_url": p.linkedin_url,
             "source": "GTM University + Slack" if _msgs(p.name or "") else "GTM University only",
             "slack_messages": _msgs(p.name or ""), "active": p.active}
            for p in candidates
        ] + [
            {"name": n, "title": None, "company": None, "linkedin_url": None,
             "source": "Slack only (no GTM University profile)", "slack_messages": _msgs(n), "active": None}
            for n in slack_only
        ]
        options.sort(key=lambda o: -o["slack_messages"])
        return {
            "searched_for": name,
            "ambiguous": True,
            "match_count": total_matches,
            "message": (
                f"{total_matches} people match '{name}'. Ask the user which one they mean before "
                "answering -- do not pick one. List them with their titles and companies."
            ),
            "options": options,
        }

    candidates.sort(key=lambda p: (not p.active, len(p.name or "")))
    profile = candidates[0] if candidates else None
    if profile is None and slack_only:
        name = slack_only[0]  # Slack-only person: resolve their activity under their Slack name

    out: dict = {"searched_for": name, "ambiguous": False}
    if profile:
        gtm = profile.gtm_university_data or {}
        out["gtm_university_profile"] = {
            "name": profile.name, "title": profile.title, "company": profile.company,
            "linkedin_url": profile.linkedin_url, "website": gtm.get("website"),
            "partner_type": gtm.get("partnerType"), "keywords": gtm.get("keywords"),
            "description": gtm.get("description"), "sells_to": profile.sells_to,
            "capabilities": gtm.get("internalCapabilities"),
            "booking_link": gtm.get("mainCta"),
            "stated_icp_text": (gtm.get("icp") or "").strip() or None,
            "structured_icp": get_structured_icp(profile),
            "classification": profile.classification_status,
        }
    else:
        out["gtm_university_profile"] = None
        out["note"] = "No GTM University profile on file under that name -- they may be in Slack only."

    slack_names = [
        n for (n,) in db.query(SlackCommunityMessage.user_name)
        .filter(SlackCommunityMessage.tenant_id == tenant_id, SlackCommunityMessage.user_name.isnot(None))
        .distinct().all()
        if _name_matches(name, n) or (profile and _name_matches(profile.name or "", n))
    ]
    if not slack_names:
        out["slack_activity"] = {"total_messages": 0, "note": "No Slack activity found under that name."}
        return out
    q = db.query(SlackCommunityMessage).filter(
        SlackCommunityMessage.tenant_id == tenant_id, SlackCommunityMessage.user_name.in_(slack_names)
    )
    total = q.count()
    if not total:
        out["slack_activity"] = {"total_messages": 0, "note": "No Slack activity found under that name."}
        return out

    by_channel = dict(
        db.query(SlackCommunityMessage.channel, func.count(SlackCommunityMessage.id))
        .filter(SlackCommunityMessage.tenant_id == tenant_id, SlackCommunityMessage.user_name.in_(slack_names))
        .group_by(SlackCommunityMessage.channel)
        .order_by(func.count(SlackCommunityMessage.id).desc()).all()
    )
    first, last = (
        db.query(func.min(SlackCommunityMessage.posted_at), func.max(SlackCommunityMessage.posted_at))
        .filter(SlackCommunityMessage.tenant_id == tenant_id, SlackCommunityMessage.user_name.in_(slack_names)).first()
    )
    names = _slack_names(db)
    rows = q.order_by(SlackCommunityMessage.posted_at.desc()).limit(min(message_limit, 100)).all()
    out["slack_activity"] = {
        "slack_name": rows[0].user_name if rows else None,
        "slack_title": rows[0].user_title if rows else None,
        "total_messages": total,
        "by_channel": {f"#{k}": v for k, v in by_channel.items()},
        "first_seen": first.strftime("%Y-%m-%d") if first else None,
        "last_seen": last.strftime("%Y-%m-%d") if last else None,
        "recent_messages": [
            {"channel": f"#{r.channel}", "when": r.posted_at.strftime("%Y-%m-%d") if r.posted_at else None,
             "is_reply": r.is_reply, "reactions": r.reaction_count, "text": _clean(r.text, names)[:2000]}
            for r in rows
        ],
    }
    return out


def list_partners(db: Session, tenant_id: int, search: str | None = None,
                  sort_by: str = "slack_activity", limit: int = 30) -> dict:
    from app.db.models import LinkedinMonitorProfile
    from app.gtm_os.community.slack_community import SlackCommunityMessage
    from app.phases.partner_icp import get_structured_icp

    q = db.query(LinkedinMonitorProfile).filter(LinkedinMonitorProfile.tenant_id == tenant_id)
    if search:
        like = f"%{search.strip()}%"
        q = q.filter(
            (LinkedinMonitorProfile.name.ilike(like)) | (LinkedinMonitorProfile.title.ilike(like))
            | (LinkedinMonitorProfile.company.ilike(like)) | (LinkedinMonitorProfile.sells_to.ilike(like))
        )
    profiles = q.all()

    activity = dict(
        db.query(SlackCommunityMessage.user_name, func.count(SlackCommunityMessage.id))
        .filter(SlackCommunityMessage.tenant_id == tenant_id, SlackCommunityMessage.user_name.isnot(None))
        .group_by(SlackCommunityMessage.user_name).all()
    )

    rows = []
    for p in profiles:
        icp = get_structured_icp(p) or {}
        rows.append({
            "name": p.name, "title": p.title, "company": p.company,
            "linkedin_url": p.linkedin_url,
            "slack_messages": activity.get(p.name, 0),
            "has_icp_text": bool(((p.gtm_university_data or {}).get("icp") or "").strip()),
            "icp_industries": icp.get("industries") or [],
            "icp_geographies": icp.get("geographies") or [],
            "icp_revenue_band_usd": [icp.get("revenue_min_usd"), icp.get("revenue_max_usd")],
        })
    rows.sort(key=(lambda r: -r["slack_messages"]) if sort_by != "name" else (lambda r: (r["name"] or "").lower()))
    return {"total_partners": len(rows), "returned": min(len(rows), limit), "partners": rows[:min(limit, 100)]}


def execute_network_chat_tool(name: str, tool_input: dict, db: Session, tenant_id: int) -> dict:
    if name == "get_partner_profile":
        return get_partner_profile(db, tenant_id, tool_input["name"], tool_input.get("message_limit", 25))
    if name == "list_partners":
        return list_partners(db, tenant_id, tool_input.get("search"),
                             tool_input.get("sort_by", "slack_activity"), tool_input.get("limit", 30))
    if name in ("search_gtm_community", "get_gtm_community_overview"):
        from app.gtm_os.community.slack_community import community_overview, search_community
        if name == "get_gtm_community_overview":
            return community_overview(db, tenant_id)
        res = search_community(
            db, tenant_id, query=tool_input.get("query"), channel=tool_input.get("channel"),
            person=tool_input.get("person"), limit=tool_input.get("limit", 25),
        )
        names = _slack_names(db)
        for m in res["messages"]:
            m["text"] = _clean(m["text"], names)
        return res
    raise ValueError(f"unknown network chat tool: {name}")
