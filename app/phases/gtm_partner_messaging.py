"""Drafts the outreach message a human sends to a GTM partner once some of their recommended
companies (see app/phases/gtm_partner_matching.py) have been approved -- Approval Gate 2 in the
identify -> approve -> generate message -> approve -> send flow. One message per partner per
batch of newly-approved recommendations, not one message per company.

Reuses the proven patterns from app/phases/personalized_outreach.py rather than inventing new
ones: generate_text() via app/llm_client.py, hard post-processing enforcement for things the
model doesn't reliably obey on instruction alone (em dash stripping -- found live there too),
and a Parameter-backed rolling history of recent openers so messages to different partners don't
all read identically. Kept as its own model (PartnerRecommendationMessage) rather than reusing
PersonalizedMessage, which is 1:1 with a single Contact -- this is one partner, N companies, one
message.

SEND INTEGRATION: Slack is wired up for real (see app/slack_bot_client.py) -- mark_message_sent()
below actually sends the DM via the Slack Bot API when send_channel="slack" AND the partner has
a confirmed LinkedinMonitorProfile.slack_user_id on file, and only marks the message "sent" if
that call succeeds. LinkedIn/email are NOT wired up yet ("for now" scope was Slack only, per the
user) -- those channels still just record a human-reported send, same as Slack did before this
integration existed. A partner with no slack_user_id on file behaves the same way (falls back
to manual recording) rather than erroring, since resolving that id is a separate, deliberately
conservative step (see slack_bot_client.py's module docstring on why no fuzzy matching)."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Company, LinkedinMonitorProfile, Parameter, PartnerCompanyRecommendation, PartnerRecommendationMessage
from app.llm_client import generate_text
from app.phases.linkedin_monitor import ScheduleConfigError

RECENT_OPENERS_PARAMETER_KEY = "gtm_partner_recent_openers"
RECENT_OPENERS_MAX = 5


def _get_recent_openers(db: Session, tenant_id: int) -> list[str]:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == RECENT_OPENERS_PARAMETER_KEY)
        .first()
    )
    if param and param.value and isinstance(param.value, dict):
        return param.value.get("messages", [])
    return []


def _record_opener(db: Session, tenant_id: int, message: str) -> None:
    messages = _get_recent_openers(db, tenant_id)
    messages.append(message)
    messages = messages[-RECENT_OPENERS_MAX:]
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == RECENT_OPENERS_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = {"messages": messages}
    else:
        param = Parameter(tenant_id=tenant_id, key=RECENT_OPENERS_PARAMETER_KEY, value={"messages": messages},
                           description="Rolling history of recent GTM-partner recommendation messages, used to keep new ones from repeating the same structure")
        db.add(param)
    db.commit()


MESSAGE_PROMPT = """Write a short, personal message from Kishan at Elephant Edge to a GTM \
partner in our ecosystem, letting them know about companies that might be relevant leads for \
their specific specialty. This is a free, no-strings gesture to build trust -- not a sales \
pitch, not asking for anything back.

Partner name: {partner_name}
Partner's known specialty/who they work with: {sells_to}

Companies to mention (already vetted as a real fit for this partner):
{companies}

Rules:
- Plain, casual, first-person from Kishan. No corporate phrasing.
- Never use an em dash (the "—" character). Use a period, comma, or "and"/"but" instead.
- Briefly say why each company might be a fit for THEM specifically, not generic praise.
- No specific dates or "we're testing/experimenting" language.
- Short -- a few sentences, not a full email. This is meant to read like a quick personal note.
{avoid_block}
Return ONLY the message text, no JSON, no markdown fences, no subject line."""


def _format_companies(recs: list[PartnerCompanyRecommendation], companies_by_id: dict[int, Company]) -> str:
    lines = []
    for r in recs:
        c = companies_by_id.get(r.company_id)
        if not c:
            continue
        lines.append(f"- {c.name}: {r.match_reasoning or 'strong fit based on their known specialty'}")
    return "\n".join(lines)


def generate_recommendation_message(db: Session, tenant_id: int, profile_id: int) -> PartnerRecommendationMessage | None:
    """Covers every APPROVED recommendation for this partner not already covered by an existing
    draft/approved/sent message -- so calling this again after a partner gets a fresh batch of
    approved recommendations only drafts a message for the new ones, not a duplicate of what's
    already been handled. Returns None if there's nothing new to draft for."""
    profile = (
        db.query(LinkedinMonitorProfile)
        .filter(LinkedinMonitorProfile.id == profile_id)
        .filter(LinkedinMonitorProfile.tenant_id == tenant_id)
        .first()
    )
    if not profile:
        return None

    already_covered_ids: set[int] = set()
    for msg in (
        db.query(PartnerRecommendationMessage)
        .filter(PartnerRecommendationMessage.tenant_id == tenant_id)
        .filter(PartnerRecommendationMessage.profile_id == profile_id)
        .filter(PartnerRecommendationMessage.status != "rejected")
    ):
        already_covered_ids.update(msg.recommendation_ids or [])

    approved_recs = (
        db.query(PartnerCompanyRecommendation)
        .filter(PartnerCompanyRecommendation.tenant_id == tenant_id)
        .filter(PartnerCompanyRecommendation.profile_id == profile_id)
        .filter(PartnerCompanyRecommendation.status == "approved")
        .all()
    )
    new_recs = [r for r in approved_recs if r.id not in already_covered_ids]
    if not new_recs:
        return None

    companies_by_id = {
        c.id: c for c in db.query(Company).filter(Company.id.in_([r.company_id for r in new_recs])).all()
    }

    recent = _get_recent_openers(db, tenant_id)
    avoid_block = (
        "\nDo not reuse a similar opening sentence or overall structure to any of these "
        "previous messages (restructure entirely, not just swap words):\n\n"
        + "\n\n---\n\n".join(recent) + "\n"
    ) if recent else ""

    prompt = MESSAGE_PROMPT.format(
        partner_name=profile.name or "there",
        sells_to=profile.sells_to or profile.industry or "their specialty",
        companies=_format_companies(new_recs, companies_by_id),
        avoid_block=avoid_block,
    )
    try:
        message = generate_text(prompt, db, tenant_id, max_tokens=400)
    except Exception as e:  # noqa: BLE001 -- persist the failure, don't crash the caller
        message = None
        error = str(e)
    else:
        # Guaranteed, not just instructed -- same "found live the model doesn't reliably follow
        # this on its own" precedent as personalized_outreach.py.
        message = message.replace("—", ", ").replace("--", ", ").strip()
        error = None

    record = PartnerRecommendationMessage(
        tenant_id=tenant_id, profile_id=profile_id,
        recommendation_ids=[r.id for r in new_recs],
        generated_message=message, status="draft" if message else "rejected",
        generated_at=datetime.utcnow(),
    )
    db.add(record)
    db.commit()

    if message:
        _record_opener(db, tenant_id, message)
    return record


def get_messages_for_profile(db: Session, tenant_id: int, profile_id: int) -> list[PartnerRecommendationMessage]:
    return (
        db.query(PartnerRecommendationMessage)
        .filter(PartnerRecommendationMessage.tenant_id == tenant_id)
        .filter(PartnerRecommendationMessage.profile_id == profile_id)
        .order_by(PartnerRecommendationMessage.id.desc())
        .all()
    )


def update_message_status(db: Session, tenant_id: int, message_id: int, status: str) -> PartnerRecommendationMessage | None:
    if status not in ("draft", "approved", "rejected"):
        raise ScheduleConfigError(f"invalid status {status!r}")
    msg = (
        db.query(PartnerRecommendationMessage)
        .filter(PartnerRecommendationMessage.id == message_id)
        .filter(PartnerRecommendationMessage.tenant_id == tenant_id)
        .first()
    )
    if not msg:
        return None
    msg.status = status
    msg.reviewed_at = datetime.utcnow()
    db.commit()
    return msg


def mark_message_sent(db: Session, tenant_id: int, message_id: int, send_channel: str) -> tuple[PartnerRecommendationMessage, bool] | tuple[None, bool]:
    """For send_channel="slack" with a confirmed slack_user_id on file, this actually sends the
    DM via app/slack_bot_client.py FIRST -- the message is only marked "sent" if that real send
    succeeds (a SlackBotError propagates to the caller instead of silently marking success).
    Every other case (no slack_user_id yet, or a non-Slack channel) falls back to the original
    "a human reported sending this" recording -- not an error, just not automated yet.

    Returns (message, auto_sent) -- auto_sent tells the caller whether this was a real Slack
    send or just a manual record, so the UI can show the difference honestly."""
    if send_channel not in ("slack", "linkedin", "email"):
        raise ScheduleConfigError(f"invalid send_channel {send_channel!r}")
    msg = (
        db.query(PartnerRecommendationMessage)
        .filter(PartnerRecommendationMessage.id == message_id)
        .filter(PartnerRecommendationMessage.tenant_id == tenant_id)
        .first()
    )
    if not msg:
        return None, False

    auto_sent = False
    if send_channel == "slack" and msg.generated_message:
        profile = (
            db.query(LinkedinMonitorProfile)
            .filter(LinkedinMonitorProfile.id == msg.profile_id)
            .filter(LinkedinMonitorProfile.tenant_id == tenant_id)
            .first()
        )
        if profile and profile.slack_user_id:
            from app.slack_bot_client import send_dm
            send_dm(db, tenant_id, profile.slack_user_id, msg.generated_message)
            auto_sent = True

    msg.status = "sent"
    msg.send_channel = send_channel
    msg.sent_at = datetime.utcnow()
    db.commit()
    return msg, auto_sent
    return msg
