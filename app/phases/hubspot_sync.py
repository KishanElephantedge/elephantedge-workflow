"""Pushes a company + its resolved decision-maker contact into HubSpot CRM, for the sales
team's own visibility. Fires only after Decision Maker succeeds (a real, actionable lead) --
not at raw Discovery -- so HubSpot never fills up with unvetted companies the sales team then
has to manually clean out. Best-effort: a HubSpot failure here never breaks the Decision Maker
phase itself, since HubSpot sync is a side effect of a successful search, not a precondition
for it."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Company, Contact
from app.hubspot_client import associate_contact_to_company, create_company, create_contact, find_company_by_domain


def sync_to_hubspot(company: Company, contact: Contact, db: Session, tenant_id: int) -> None:
    if contact.hubspot_synced_at:
        return

    company_id = find_company_by_domain(company.domain, db, tenant_id)
    if company_id is None:
        company_id = create_company(company.name, company.domain, db, tenant_id)

    contact_id = create_contact(contact.first_name, contact.last_name, contact.title, db, tenant_id)
    associate_contact_to_company(contact_id, company_id, db, tenant_id)

    contact.hubspot_synced_at = datetime.utcnow()
    db.commit()
