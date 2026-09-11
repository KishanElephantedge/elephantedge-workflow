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

from app.db.models import Batch, CampaignPush, Company, Contact
from app.outreach.base import OutreachChannel


def run_campaign_execution(batch_id: int, db: Session, channel: OutreachChannel) -> dict:
    contacts = db.query(Contact).join(Company).filter(Company.batch_id == batch_id).all()

    # 2026-08-25, explicit instruction -- the real offering/campaign tracking link. A batch
    # tagged at creation time (Batch.offering_name/campaign_label, e.g. one of the 4 real
    # outbound campaigns launched this session) has every CampaignPush it produces stamped with
    # the same tags, so a push can finally be traced back to which offering/campaign it belonged
    # to -- previously impossible, this table had no such join key at all. An untagged batch
    # (real ICP-discovery batches, not a named outbound campaign) stamps None, exactly as before.
    batch = db.get(Batch, batch_id)
    batch_offering_name = batch.offering_name if batch else None
    campaign_label = batch.campaign_label if batch else None

    def _offering_for(contact: Contact) -> str | None:
        """A discovery batch legitimately mixes companies matched to several different offerings
        -- confirmed live 2026-09-11: one real batch held Consulting, Digital Playbook, Sales OS
        and Execution matches together. Batch.offering_name is a single value meant for a batch
        that IS one named outbound campaign (see its own comment); using it here for a mixed batch
        routed every contact through whichever campaign that one field happened to name, or through
        the SalesRobotChannel's own tenant-wide fallback when it was unset -- which is how 123 real
        contacts over 6+ weeks ended up in a campaign with zero follow-up configured while other,
        working, per-offering campaigns sat unused. Company.resolved_offering_name (set by
        icp_offering_matching/offering_tiebreak at ICP-match time) is checked first so each contact
        routes by its OWN match; batch_offering_name remains the answer for a batch that really is
        one offering end to end, and this only ever narrows or preserves existing behavior -- an
        untagged batch with no per-company resolution either still returns None, same as before."""
        return (contact.company.resolved_offering_name if contact.company else None) or batch_offering_name

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

        offering_name = _offering_for(contact)

        if contact.excluded_from_push:
            skipped += 1
            db.add(CampaignPush(
                contact_id=contact.id,
                heyreach_campaign_id=None,
                status="skipped",
                error_message="Excluded from push via dashboard",
                pushed_at=None,
                offering_name=offering_name,
                campaign_label=campaign_label,
            ))
            db.commit()
            continue

        try:
            result = channel.push_lead(contact, offering_name=offering_name)
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
            offering_name=offering_name,
            campaign_label=campaign_label,
        ))
        db.commit()

    return {"contacts_checked": len(contacts), "pushed": pushed, "failed": failed, "skipped": skipped}
