"""
Phase 6 — Decision Maker Intelligence.

Single-threaded target, not multi-persona simultaneous search like Synefi's. Per Gokul's
confirmed guidance: Founder/CEO/Co-Founder is the primary target; a Head of Sales/VP Sales/
Head of GTM person is a secondary target, tried only when no primary contact exists at all
(not pursued in parallel) -- covers companies with no findable founder/CEO record (e.g. no
public leadership data indexed for that domain) rather than leaving them with zero contact.

One search_contact call per target tier, using a boolean title filter rather than a sequence
of exact-title steps. Real-world titles are frequently compound free text ("CEO and Co-Founder",
"Founder & CEO") that an exact title_lists match misses entirely -- confirmed live on the
first real company run (GoZen's "Ambi Moorthy, CEO and Co-Founder" matched only the broad
filter, not any single exact-title step). search_contact already ranks its own results by
relevance, so running exact-title steps first before falling back to the same filter's superset
was pure wasted cost: up to 4 billed calls to land on a result the 1st call would have found.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.budget_guard import BudgetExceededError, BudgetGuard
from app.db.models import Company, Contact
from app.deepline_client import execute_tool, extract_rows
from app.hubspot_client import HubSpotError
from app.phases.hubspot_sync import sync_to_hubspot

CEO_FILTER = "CEO OR Chief Executive Officer OR Founder OR Co-Founder OR Owner OR Managing Director OR President"

SALES_LEADER_FILTER = (
    "Head of Sales OR VP Sales OR VP of Sales OR Head of GTM OR Head of Growth OR "
    "Director of Sales OR Director of Business Development OR Head of Business Development OR "
    "Chief Revenue Officer OR CRO"
)


def _run_search_contact(company: Company, extra_payload: dict) -> list[dict]:
    payload = {
        "domain": company.domain,
        # Billed per result returned; only ever use persons[0], so request exactly 1.
        "page_size": 1,
        **extra_payload,
    }
    response = execute_tool("search_contact", payload)
    raw = response.get("toolResponse", {}).get("raw", {})
    if isinstance(raw, dict) and isinstance(raw.get("output"), dict):
        persons = raw["output"].get("persons")
        if isinstance(persons, list):
            return persons
    return extract_rows(response, "persons")


def _make_contact(company: Company, db: Session, persons: list[dict], reasoning: str, thread_role: str) -> Contact:
    person = persons[0]
    contact = Contact(
        company_id=company.id,
        first_name=person.get("first_name"),
        last_name=person.get("last_name"),
        title=person.get("title"),
        linkedin_url=person.get("linkedin_url") or person.get("linkedin"),
        thread_role=thread_role,
        matched_title_reasoning=reasoning,
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


def find_decision_maker(company: Company, db: Session) -> Contact | None:
    persons = _run_search_contact(company, {"title_filters": [{"name": "ceo_filter", "filter": CEO_FILTER}]})
    if persons:
        return _make_contact(company, db, persons, f"title_filter={CEO_FILTER}", "founder_ceo")

    # No primary (founder/CEO) contact exists at all for this domain -- try the secondary
    # sales-leader target rather than leaving the company with zero contact.
    persons = _run_search_contact(company, {"title_filters": [{"name": "sales_leader_filter", "filter": SALES_LEADER_FILTER}]})
    if persons:
        return _make_contact(company, db, persons, f"title_filter={SALES_LEADER_FILTER}", "sales_leader")

    return None


def run_decision_maker_id(batch_id: int, db: Session, tenant_id: int, retry_company_ids: list[int] | None = None, budget_guard: BudgetGuard | None = None) -> dict:
    """Phase 6 entrypoint. No tier/score gate here -- this batch's companies were hand-picked
    (Phase 3 Qualification doesn't apply), so every company in the batch is searched.

    By default, skips any company already searched -- whether it found a contact or not.
    search_contact bills real credits per call regardless of outcome, and a company with
    genuinely no findable record would otherwise get re-billed on every single re-run forever,
    including a future autonomous daily cycle, since a "not found" result never produces a
    Contact row to skip on its own.

    retry_company_ids overrides the skip for those specific companies only -- an explicit,
    deliberate re-attempt (e.g. after correcting a wrong domain), never an automatic one.

    budget_guard, if given, is checked after every company -- this is the single most
    expensive phase per company (search_contact, ~$0.256), so this is the loop where a hard
    spend cap matters most. Stops the loop (rather than raising out of it) the moment this
    run's cap is reached, leaving already-resolved companies' contacts in place."""
    retry_ids = set(retry_company_ids or [])
    companies = db.query(Company).filter(Company.batch_id == batch_id).all()

    found = 0
    not_found = 0
    skipped = 0
    hubspot_synced = 0
    hubspot_errors = []
    budget_stopped_early = False
    for company in companies:
        already_done = company.contacts or company.decision_maker_searched_at
        if already_done and company.id not in retry_ids:
            skipped += 1
            continue
        contact = find_decision_maker(company, db)
        company.decision_maker_searched_at = datetime.utcnow()
        db.commit()
        if contact:
            found += 1
            # HubSpot sync is a side effect of a successful search, not a precondition for
            # it -- a HubSpot outage or bad token must never fail the Decision Maker phase.
            # The error is still surfaced in the response, not swallowed silently, so a bad
            # token doesn't go unnoticed.
            try:
                sync_to_hubspot(company, contact, db, tenant_id)
                hubspot_synced += 1
            except HubSpotError as e:
                hubspot_errors.append(f"{company.name}: {e}")
        else:
            not_found += 1

        if budget_guard is not None:
            try:
                budget_guard.check()
            except BudgetExceededError:
                budget_stopped_early = True
                break

    return {
        "companies_checked": len(companies),
        "decision_makers_found": found,
        "companies_with_no_contact": not_found,
        "hubspot_synced": hubspot_synced,
        "hubspot_errors": hubspot_errors,
        "companies_skipped_already_resolved": skipped,
        "budget_stopped_early": budget_stopped_early,
    }
