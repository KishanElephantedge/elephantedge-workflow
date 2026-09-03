"""The GTM Partners Slack community, stored so it can be ASKED QUESTIONS OF rather than read.

WHY THIS EXISTS. Elephant Edge sits inside GTM Partners' certified-partner Slack: ~160 senior
operators (ex-CMOs, CROs, VPs) who have just started their own fractional practices. Reading the
archive once produced a useful answer; the point of storing it is that the CEO can ask his own
questions of it in the dashboard chat, on his own time, without anyone exporting a file first.

WHAT THE COMMUNITY IS, from its own messages -- context a caller needs to interpret results:
  - They already refer work to each other AND pay for it (10% referral fees discussed openly).
  - Their clients want execution, not strategy alone ("none have wanted strategy only" -- Don
    Drury, 7 years, $5-40M clients). Most are solo and cannot deliver it.
  - Their pain is selling, not delivering: pricing 319 mentions, prospecting 210, referrals 189,
    delivery only 75.

BOUNDARY -- read this before adding any tool here. #ask-a-question contains ~2,100 messages where
partners describe THEIR CLIENTS in detail: company, revenue, retention numbers, the founder's
blind spot. That is a private professional forum, not a lead source. Nothing in this module
surfaces client details as prospecting targets, and no tool here should be added that does. The
relationship is the asset; mining it would end the relationship and the referrals with it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, JSON

from app.db.models import Base

logger = logging.getLogger(__name__)

WORKSPACE = "GTM Certified Partners"


class SlackCommunityMessage(Base):
    """One Slack message (top-level or thread reply) from a community workspace we belong to.

    Deliberately its own table rather than reusing Signal: a Signal is evidence about a PROSPECT
    that feeds the opportunity pipeline, and these messages are explicitly NOT that (see the
    module docstring's boundary). Keeping them separate makes it structurally hard for community
    conversation to leak into prospecting.
    """

    __tablename__ = "slack_community_messages"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    workspace = Column(String, nullable=False)
    channel = Column(String, nullable=False)
    ts = Column(String, nullable=False)          # Slack's own message id, unique per channel
    thread_ts = Column(String, nullable=True)    # set when this is a reply; equals the parent's ts
    is_reply = Column(Boolean, nullable=False, default=False)
    posted_at = Column(DateTime, nullable=True)
    user_id = Column(String, nullable=True)
    user_name = Column(String, nullable=True)
    user_title = Column(String, nullable=True)
    text = Column(Text, nullable=True)
    reply_count = Column(Integer, nullable=True)
    reaction_count = Column(Integer, nullable=True)
    file_count = Column(Integer, nullable=True)
    permalink_ref = Column(String, nullable=True)  # channel_id/ts -- enough to rebuild a link
    raw = Column(JSON, nullable=True)
    ingested_at = Column(DateTime, default=datetime.utcnow)


def ingest_archive(db, tenant_id: int, archive_path: str) -> dict:
    """Loads a fetched archive JSON into the table. Idempotent on (tenant, channel, ts).

    Re-running after a fresh fetch updates edited messages and adds new ones rather than
    duplicating -- a community archive is refreshed, not appended to.
    """
    archive = json.load(open(archive_path))
    users = archive.get("users") or {}
    existing = {
        (r[0], r[1])
        for r in db.query(SlackCommunityMessage.channel, SlackCommunityMessage.ts)
        .filter(SlackCommunityMessage.tenant_id == tenant_id).all()
    }

    counts = {"channels": 0, "inserted": 0, "updated": 0, "skipped_no_text": 0, "skipped_duplicate": 0}
    seen_this_run: set[tuple[str, str]] = set()
    for channel, c in (archive.get("channels") or {}).items():
        if c.get("error"):
            continue
        counts["channels"] += 1
        rows = [(m, False, None) for m in c.get("messages", [])]
        for parent_ts, replies in (c.get("threads") or {}).items():
            rows.extend((m, True, parent_ts) for m in replies)

        for m, is_reply, parent_ts in rows:
            ts = m.get("ts")
            if not ts:
                continue
            # A reply posted with "also send to channel" (subtype thread_broadcast) comes back from
            # BOTH conversations.history and conversations.replies, so the same ts appears twice in
            # one pass. Track as we go rather than only reading the table once up front.
            if (channel, ts) in seen_this_run:
                counts["skipped_duplicate"] += 1
                continue
            seen_this_run.add((channel, ts))
            text = (m.get("text") or "").strip()
            if not text and not m.get("files"):
                counts["skipped_no_text"] += 1
                continue
            u = users.get(m.get("user")) or {}
            values = dict(
                tenant_id=tenant_id, workspace=archive.get("workspace", WORKSPACE), channel=channel,
                ts=ts, thread_ts=parent_ts or m.get("thread_ts"), is_reply=is_reply,
                posted_at=datetime.utcfromtimestamp(float(ts)),
                user_id=m.get("user"), user_name=u.get("name"), user_title=u.get("title"),
                text=text, reply_count=m.get("reply_count"),
                reaction_count=sum(r.get("count", 0) for r in (m.get("reactions") or [])),
                file_count=len(m.get("files") or []),
                permalink_ref=f"{c.get('id')}/{ts}", raw=m,
            )
            if (channel, ts) in existing:
                db.query(SlackCommunityMessage).filter(
                    SlackCommunityMessage.tenant_id == tenant_id,
                    SlackCommunityMessage.channel == channel,
                    SlackCommunityMessage.ts == ts,
                ).update({k: v for k, v in values.items() if k not in ("tenant_id", "channel", "ts")})
                counts["updated"] += 1
            else:
                db.add(SlackCommunityMessage(**values))
                counts["inserted"] += 1
        db.commit()
    return counts


def search_community(db, tenant_id: int, query: str | None = None, channel: str | None = None,
                     person: str | None = None, limit: int = 25) -> dict:
    """Free-text search across the archive. Returns real messages, never a summary -- the caller
    (Claude, in the dashboard chat) does the interpreting, so nothing is pre-digested here."""
    q = db.query(SlackCommunityMessage).filter(SlackCommunityMessage.tenant_id == tenant_id)
    if query:
        q = q.filter(SlackCommunityMessage.text.ilike(f"%{query.strip()}%"))
    if channel:
        q = q.filter(SlackCommunityMessage.channel.ilike(f"%{channel.strip().lstrip('#')}%"))
    if person:
        q = q.filter(SlackCommunityMessage.user_name.ilike(f"%{person.strip()}%"))
    total = q.count()
    rows = q.order_by(SlackCommunityMessage.posted_at.desc()).limit(min(limit, 100)).all()
    return {
        "total_matches": total,
        "returned": len(rows),
        "messages": [
            {
                "channel": f"#{r.channel}", "who": r.user_name, "title": r.user_title,
                "when": r.posted_at.strftime("%Y-%m-%d") if r.posted_at else None,
                "is_reply": r.is_reply, "reactions": r.reaction_count,
                "text": (r.text or "")[:1500],
            }
            for r in rows
        ],
    }


def community_overview(db, tenant_id: int) -> dict:
    """Shape of the archive: channels, sizes, date span, most active people. The orientation a
    question like "what is going on in this community" needs before it can be answered."""
    from sqlalchemy import func

    rows = (
        db.query(SlackCommunityMessage.channel, func.count(SlackCommunityMessage.id),
                 func.min(SlackCommunityMessage.posted_at), func.max(SlackCommunityMessage.posted_at))
        .filter(SlackCommunityMessage.tenant_id == tenant_id)
        .group_by(SlackCommunityMessage.channel)
        .order_by(func.count(SlackCommunityMessage.id).desc()).all()
    )
    people = (
        db.query(SlackCommunityMessage.user_name, SlackCommunityMessage.user_title,
                 func.count(SlackCommunityMessage.id))
        .filter(SlackCommunityMessage.tenant_id == tenant_id,
                SlackCommunityMessage.user_name.isnot(None))
        .group_by(SlackCommunityMessage.user_name, SlackCommunityMessage.user_title)
        .order_by(func.count(SlackCommunityMessage.id).desc()).limit(20).all()
    )
    return {
        "workspace": WORKSPACE,
        "total_messages": sum(r[1] for r in rows),
        "channels": [
            {"channel": f"#{r[0]}", "messages": r[1],
             "from": r[2].strftime("%Y-%m") if r[2] else None,
             "to": r[3].strftime("%Y-%m") if r[3] else None}
            for r in rows
        ],
        "most_active_people": [{"name": p[0], "title": p[1], "messages": p[2]} for p in people],
    }
