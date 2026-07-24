"""Phase 11 — Campaign Execution. Pushes every not-yet-successfully-pushed contact in a
batch through a given OutreachChannel. The channel is injected by the caller (see
app/routes/api.py), never hardcoded here -- this function has no idea HeyReach exists."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import CampaignPush, Company, Contact
from app.outreach.base import OutreachChannel


def run_campaign_execution(batch_id: int, db: Session, channel: OutreachChannel) -> dict:
    contacts = db.query(Contact).join(Company).filter(Company.batch_id == batch_id).all()

    already_pushed_ids = {
        p.contact_id
        for p in db.query(CampaignPush)
        .filter(CampaignPush.contact_id.in_([c.id for c in contacts]))
        .filter(CampaignPush.status == "pushed")
        .all()
    }

    pushed = failed = skipped = 0
    for contact in contacts:
        if contact.id in already_pushed_ids:
            skipped += 1
            continue

        result = channel.push_lead(contact)
        if result["status"] == "pushed":
            pushed += 1
        elif result["status"] == "skipped":
            skipped += 1
        else:
            failed += 1

        db.add(CampaignPush(
            contact_id=contact.id,
            heyreach_campaign_id=result.get("channel_ref"),
            status=result["status"],
            error_message=result.get("error_message"),
            pushed_at=datetime.utcnow() if result["status"] == "pushed" else None,
        ))
    db.commit()

    return {"contacts_checked": len(contacts), "pushed": pushed, "failed": failed, "skipped": skipped}
