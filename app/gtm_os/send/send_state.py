"""V2 Send state -- Phase 7. One insert-only row per real send ATTEMPT, mirroring the exact
discipline app.db.models.CampaignPush already established for V1's own campaign pushes (never
update a row in place, add a new one for every attempt, commit immediately) -- extended here to
carry the extra identity a V2 send needs that CampaignPush doesn't: message_draft_id (V2 sends
are keyed to a specific MessageDraft, not just a Contact) and an explicit `retryable` flag (so
"permanent failure" vs "transient, worth retrying" is a real stored fact, never re-guessed from
error text).

This is a SEPARATE table from CampaignPush, not a shared one -- CampaignPush is V1's own
campaign-enrollment log (HeyReach/SalesRobot campaign pushes via run_campaign_execution) and has
no concept of a MessageDraft at all; reusing it would mean bolting on unrelated columns or
leaving them permanently null, the same "own table when the shape genuinely differs" precedent
already used elsewhere in this codebase (GtmIntelligenceRun vs AutonomousRun, GtmStrategy vs the
Score pipeline's own tables)."""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Session

from app.db.models import Base, Contact

# Real values a channel adapter can return -- "enrolled" (SmartLead: the provider ACCEPTED the
# lead into a campaign; its own async sequence engine sends the actual email later -- NOT the
# same claim as "sent", per explicit instruction not to conflate enrollment with delivery) and
# "request_submitted" (SalesRobot: the connection-request API call itself is the real action,
# submitted synchronously) are kept distinct rather than both being called "sent", since the
# underlying provider semantics genuinely differ.
SEND_ATTEMPT_STATUSES = {"enrolled", "request_submitted", "failed"}
SUCCESS_STATUSES = {"enrolled", "request_submitted"}


class MessageSendAttempt(Base):
    __tablename__ = "message_send_attempts"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    message_draft_id = Column(Integer, ForeignKey("message_drafts.id"), nullable=False)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False)

    channel = Column(String, nullable=False)  # "email" | "linkedin" -- copied from MessageDraft.channel at attempt time
    provider = Column(String, nullable=False)  # "smartlead" | "salesrobot"

    status = Column(String, nullable=False)  # one of SEND_ATTEMPT_STATUSES
    provider_ref = Column(String, nullable=True)  # SmartLead: campaign_id/added_count summary. SalesRobot: invitationId.
    error_message = Column(Text, nullable=True)
    # False = this specific failure cannot succeed on retry (permanently invalid contact,
    # explicit provider rejection, structural gap like no email/no linkedin_url) -- the send
    # stage stops retrying immediately rather than waiting out the configured attempt count.
    # Irrelevant/None on a successful attempt.
    retryable = Column(Boolean, nullable=True)

    attempted_at = Column(DateTime, default=datetime.utcnow)


def get_attempts_for_draft(db: Session, message_draft_id: int) -> list[MessageSendAttempt]:
    return (
        db.query(MessageSendAttempt)
        .filter(MessageSendAttempt.message_draft_id == message_draft_id)
        .order_by(MessageSendAttempt.attempted_at.asc())
        .all()
    )


def has_successful_send(db: Session, message_draft_id: int) -> bool:
    return (
        db.query(MessageSendAttempt.id)
        .filter(MessageSendAttempt.message_draft_id == message_draft_id, MessageSendAttempt.status.in_(SUCCESS_STATUSES))
        .first()
        is not None
    )


def last_attempt_permanently_failed(db: Session, message_draft_id: int) -> bool:
    """True only when the MOST RECENT attempt for this draft failed AND was marked
    non-retryable -- a real, stored fact, never inferred from error text at read time."""
    last = (
        db.query(MessageSendAttempt)
        .filter(MessageSendAttempt.message_draft_id == message_draft_id)
        .order_by(MessageSendAttempt.attempted_at.desc())
        .first()
    )
    return last is not None and last.status == "failed" and last.retryable is False


def last_send_at_for_contact(db: Session, contact_id: int) -> datetime | None:
    row = (
        db.query(MessageSendAttempt.attempted_at)
        .filter(MessageSendAttempt.contact_id == contact_id, MessageSendAttempt.status.in_(SUCCESS_STATUSES))
        .order_by(MessageSendAttempt.attempted_at.desc())
        .first()
    )
    return row[0] if row else None


def last_send_at_for_company(db: Session, company_id: int) -> datetime | None:
    row = (
        db.query(MessageSendAttempt.attempted_at)
        .join(Contact, MessageSendAttempt.contact_id == Contact.id)
        .filter(Contact.company_id == company_id, MessageSendAttempt.status.in_(SUCCESS_STATUSES))
        .order_by(MessageSendAttempt.attempted_at.desc())
        .first()
    )
    return row[0] if row else None


def count_successful_sends_since(db: Session, tenant_id: int, since: datetime) -> int:
    return (
        db.query(MessageSendAttempt.id)
        .filter(MessageSendAttempt.tenant_id == tenant_id, MessageSendAttempt.status.in_(SUCCESS_STATUSES), MessageSendAttempt.attempted_at >= since)
        .count()
    )


def is_within_cooldown(db: Session, contact: Contact, company_cooldown_days: int, contact_cooldown_days: int, now: datetime | None = None) -> tuple[bool, str | None]:
    """Returns (within_cooldown, reason). Checks contact-level cooldown first (the tighter,
    more specific constraint), then company-level -- both measured against MessageSendAttempt's
    own real send history, V2's only, never V1's separate CampaignPush log (kept deliberately
    independent, same "V1/V2 stay separate, no cross-system aggregation" precedent already used
    for the discovery budget in Phase 1)."""
    now = now or datetime.utcnow()

    last_contact_send = last_send_at_for_contact(db, contact.id)
    if last_contact_send is not None:
        elapsed_days = (now - last_contact_send).total_seconds() / 86400
        if elapsed_days < contact_cooldown_days:
            return True, f"contact last sent to {elapsed_days:.1f}d ago, cooldown is {contact_cooldown_days}d"

    if contact.company_id is not None:
        last_company_send = last_send_at_for_company(db, contact.company_id)
        if last_company_send is not None:
            elapsed_days = (now - last_company_send).total_seconds() / 86400
            if elapsed_days < company_cooldown_days:
                return True, f"company last sent to {elapsed_days:.1f}d ago, cooldown is {company_cooldown_days}d"

    return False, None
