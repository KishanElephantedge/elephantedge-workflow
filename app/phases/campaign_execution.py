"""Phase 11 — Campaign Execution. Pushes every not-yet-successfully-pushed contact in a
batch through a given OutreachChannel. The channel is injected by the caller (see
app/routes/api.py), never hardcoded here -- this function has no idea HeyReach exists.

Commits after EVERY contact, not once at the end of the loop (fixed 2026-08-14, real audit
finding) -- an uncaught exception on contact N (e.g. a bare network timeout -- push_lead's own
try/except only catches its channel's own typed error, nothing else) used to roll back the
WHOLE batch's uncommitted CampaignPush rows, including ones for contacts 1..N-1 that had
already been genuinely pushed to the real outreach channel. The external side effect (a real
LinkedIn connection request or email) had already happened and can't be undone, but the DB
record of it would vanish -- so the next sweep would see those contacts as "never pushed" and
push them AGAIN, sending a real duplicate to a real prospect with zero trace of the first
attempt anywhere. Catching every exception around push_lead (not just the channel's typed
error) and committing immediately after each contact means a failure on contact N can never
affect contacts already recorded earlier in this same loop, and also means contact N+1 still
gets attempted instead of the whole batch aborting."""

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

        if contact.excluded_from_push:
            skipped += 1
            db.add(CampaignPush(
                contact_id=contact.id,
                heyreach_campaign_id=None,
                status="skipped",
                error_message="Excluded from push via dashboard",
                pushed_at=None,
            ))
            db.commit()
            continue

        try:
            result = channel.push_lead(contact)
        except Exception as e:
            result = {"status": "failed", "error_message": f"unexpected error: {e}", "channel_ref": None}

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
