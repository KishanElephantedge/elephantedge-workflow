"""get_jobs_to_be_done() -- composes four real, already-computed backend signals into one
prioritized, explainable queue. Nothing here is persisted (Approved decision #4): every call
re-derives the queue from current state, so it can never go stale or need a cleanup job.

Priority order (Approved decision #5, grounded in what's actually real in this codebase, not an
invented score):
    1. deal_needs_you      -- blocking, closest to revenue (execution_readiness.py, unmodified)
    2. hot_leads_to_review -- real high-intent flag (Company.hot_lead, unmodified)
    3. contacts_to_find    -- missing information blocking an otherwise-real account
    4. worth_engaging      -- lower urgency, topic-level (no company_id on real GtmSignal rows
                               today -- Approved decision #7, never pretended otherwise)

"calls_to_make" has no real qualifying condition in this tenant's data (SalesOutcome,
CampaignEvent, CalendarBooking outcomes are all empty; CampaignEvent also has no tenant_id
column) -- always returned as an empty, explicitly unavailable category (Approved decision #2).

Within each category, items are ordered by the most real recency field available for that
category (Approved decision #6) -- never an invented score."""

from sqlalchemy.orm import Session

from app.db.models import Company, Contact
from app.gtm_os.execution.execution_readiness import get_next_execution_action
from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.opportunity.opportunity import Opportunity

# Human-readable labels for execution_readiness.py's own EXECUTION_STATUSES vocabulary -- purely
# presentational, never a re-derivation of what the status means.
_EXEC_STATUS_LABEL = {
    "blocked": "Blocked on a prerequisite",
    "message_generation_available": "Ready to draft a message",
    "message_insufficient_context": "Message draft could not be generated",
    "message_blocked_by_quality_gate": "Message draft failed the quality gate",
    "ready_for_review": "Message draft ready for review",
    "approved": "Message approved -- ready to request a meeting",
}


def _deal_needs_you(db: Session, tenant_id: int) -> list[dict]:
    opportunity_ids = [
        row[0] for row in db.query(Opportunity.id).filter(Opportunity.tenant_id == tenant_id).all()
    ]
    items = []
    for opportunity_id in opportunity_ids:
        exec_result = get_next_execution_action(db, tenant_id, opportunity_id)
        if not exec_result["requires_human_review"]:
            continue
        opp = db.get(Opportunity, opportunity_id)
        company = db.get(Company, opp.company_id) if opp.company_id else None
        company_name = opp.company_name_raw or (company.name if company else "Unknown company")
        items.append({
            "category": "deal_needs_you",
            "title": company_name,
            "description": _EXEC_STATUS_LABEL.get(exec_result["status"], exec_result["status"].replace("_", " ")),
            "reason": exec_result["reason"],
            "company_id": opp.company_id,
            "company_name": company_name,
            "contact_id": None,
            "opportunity_id": opportunity_id,
            "source_type": "execution_action",
            "source_id": opportunity_id,
            "action_route": f"/v2/pipeline/{opportunity_id}",
            "_sort_key": opp.last_updated_at,
        })
    items.sort(key=lambda i: i["_sort_key"] or "", reverse=True)
    return items


def _hot_leads_to_review(db: Session, tenant_id: int) -> list[dict]:
    companies = (
        db.query(Company)
        .join(Company.batch)
        .filter(Company.hot_lead.is_(True))
        .filter(Company.batch.has(tenant_id=tenant_id))
        .all()
    )
    items = [
        {
            "category": "hot_leads_to_review",
            "title": c.name,
            "description": "Flagged as a hot lead",
            "reason": c.hot_lead_reasoning,
            "company_id": c.id,
            "company_name": c.name,
            "contact_id": None,
            "opportunity_id": None,
            "source_type": "company",
            "source_id": c.id,
            "action_route": f"/v2/accounts/{c.id}",
            # Company has no "flagged_at" timestamp -- created_at is the closest real field
            # available, not an invented recency signal.
            "_sort_key": c.created_at,
        }
        for c in companies
    ]
    items.sort(key=lambda i: i["_sort_key"] or "", reverse=True)
    return items


def _contacts_to_find(db: Session, tenant_id: int) -> list[dict]:
    companies = (
        db.query(Company)
        .join(Company.batch)
        .filter(Company.decision_maker_searched_at.isnot(None))
        .filter(Company.batch.has(tenant_id=tenant_id))
        .all()
    )
    items = []
    for c in companies:
        contacts = db.query(Contact).filter(Contact.company_id == c.id).all()
        if not contacts:
            items.append({
                "category": "contacts_to_find",
                "title": c.name,
                "description": "No decision-maker contact found",
                "reason": "Decision-maker search completed but no contact was found",
                "company_id": c.id,
                "company_name": c.name,
                "contact_id": None,
                "opportunity_id": None,
                "source_type": "company",
                "source_id": c.id,
                "action_route": f"/v2/accounts/{c.id}",
                "_sort_key": c.decision_maker_searched_at,
            })
            continue
        # Approved decision #8 -- a distinct, real, non-invented sub-case: a Contact exists but
        # has no confirmed email (email_source is null). This is NOT the same as "no contact
        # found" and is labeled separately.
        for contact in contacts:
            if not contact.email_source:
                name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or "Unnamed contact"
                items.append({
                    "category": "contacts_to_find",
                    "title": c.name,
                    "description": "Incomplete contact information",
                    "reason": f"{name} was found, but no confirmed email is on file",
                    "company_id": c.id,
                    "company_name": c.name,
                    "contact_id": contact.id,
                    "opportunity_id": None,
                    "source_type": "contact",
                    "source_id": contact.id,
                    "action_route": f"/v2/accounts/{c.id}",
                    "_sort_key": c.decision_maker_searched_at,
                })
    items.sort(key=lambda i: i["_sort_key"] or "", reverse=True)
    return items


def _worth_engaging(db: Session, tenant_id: int) -> list[dict]:
    interpreted_signal_ids = {
        row[0] for row in db.query(InterpretedSignal.source_signal_id).filter(InterpretedSignal.tenant_id == tenant_id).all()
    }
    signal_query = db.query(GtmSignal).filter(GtmSignal.tenant_id == tenant_id)
    if interpreted_signal_ids:
        signal_query = signal_query.filter(~GtmSignal.id.in_(interpreted_signal_ids))
    signals = signal_query.all()
    items = []
    for s in signals:
        title = s.company_name_raw or s.person_name_raw or f"{s.source.replace('_', ' ')} signal"
        snippet = None
        if isinstance(s.extracted_info, dict):
            snippet = s.extracted_info.get("title") or s.extracted_info.get("snippet")
        # Approved decision #7 -- real GtmSignal rows in this tenant have no company_id, so this
        # stays topic-level (routed to Market Intelligence) rather than pretending it's an
        # account-level lead. If a future signal DOES carry a real company_id, route to that
        # account instead -- this is a structural check, not a hardcoded assumption either way.
        action_route = f"/v2/accounts/{s.company_id}" if s.company_id else "/v2/market-intelligence"
        items.append({
            "category": "worth_engaging",
            "title": title,
            "description": snippet or f"{s.signal_type.replace('_', ' ')} observed via {s.source.replace('_', ' ')}",
            "reason": "Observed by GTM intelligence sensing, not yet interpreted",
            "company_id": s.company_id,
            "company_name": s.company_name_raw,
            "contact_id": s.contact_id,
            "opportunity_id": None,
            "source_type": "gtm_signal",
            "source_id": s.id,
            "action_route": action_route,
            "_sort_key": s.captured_at,
        })
    items.sort(key=lambda i: i["_sort_key"] or "", reverse=True)
    return items


def get_jobs_to_be_done(db: Session, tenant_id: int) -> dict:
    deal_needs_you = _deal_needs_you(db, tenant_id)
    hot_leads = _hot_leads_to_review(db, tenant_id)
    contacts_to_find = _contacts_to_find(db, tenant_id)
    worth_engaging = _worth_engaging(db, tenant_id)

    all_items = []
    for priority, group in enumerate((deal_needs_you, hot_leads, contacts_to_find, worth_engaging), start=1):
        for item in group:
            item = {k: v for k, v in item.items() if k != "_sort_key"}
            item["priority"] = priority
            all_items.append(item)

    return {
        "items": all_items,
        "category_status": {
            "deal_needs_you": {"count": len(deal_needs_you), "available": True},
            "hot_leads_to_review": {"count": len(hot_leads), "available": True},
            "contacts_to_find": {"count": len(contacts_to_find), "available": True},
            "calls_to_make": {
                "count": 0,
                "available": False,
                "reason": "No real reply/booking signal exists yet for this tenant "
                "(SalesOutcome.positive_reply, CampaignEvent, and CalendarBooking outcomes are "
                "all empty; CampaignEvent also has no tenant_id column).",
            },
            "worth_engaging": {"count": len(worth_engaging), "available": True},
        },
    }
