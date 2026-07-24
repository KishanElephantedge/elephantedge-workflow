"""
Phase 6 — Decision Maker Intelligence.

Single-threaded target, not multi-persona simultaneous search like Synefi's. This is a
direct, confirmed decision (not assumed): at Elephant Edge's SMB-scale ICP, there is no real
buying committee -- Gokul confirmed the founder/CEO is the actual decision-maker, with a
Head of Sales/GTM person (where one even exists) acting as an influencer at most, not a
parallel target. Phase 5 (Buying Committee Discovery) is effectively answered by this single
fact, not separately built out.

One search_contact call per company, using a boolean title filter rather than a sequence of
exact-title steps. Real-world titles are frequently compound free text ("CEO and Co-Founder",
"Founder & CEO") that an exact title_lists match misses entirely -- confirmed live on the
first real company run (GoZen's "Ambi Moorthy, CEO and Co-Founder" matched only the broad
filter, not any single exact-title step). search_contact already ranks its own results by
relevance, so running exact-title steps first before falling back to the same filter's superset
was pure wasted cost: up to 4 billed calls to land on a result the 1st call would have found.
"""

from sqlalchemy.orm import Session

from app.db.models import Company, Contact
from app.deepline_client import execute_tool, extract_rows

CEO_FILTER = "CEO OR Chief Executive Officer OR Founder OR Co-Founder OR Owner OR Managing Director OR President"


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


def _make_contact(company: Company, db: Session, persons: list[dict], reasoning: str) -> Contact:
    person = persons[0]
    contact = Contact(
        company_id=company.id,
        first_name=person.get("first_name"),
        last_name=person.get("last_name"),
        title=person.get("title"),
        linkedin_url=person.get("linkedin_url") or person.get("linkedin"),
        thread_role="founder_ceo",
        matched_title_reasoning=reasoning,
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


def find_decision_maker(company: Company, db: Session) -> Contact | None:
    persons = _run_search_contact(company, {"title_filters": [{"name": "ceo_filter", "filter": CEO_FILTER}]})
    if persons:
        return _make_contact(company, db, persons, f"title_filter={CEO_FILTER}")
    return None


def run_decision_maker_id(batch_id: int, db: Session) -> dict:
    """Phase 6 entrypoint. No tier/score gate here -- this batch's companies were hand-picked
    (Phase 3 Qualification doesn't apply), so every company in the batch is searched.

    Skips companies that already have a Contact -- this runs again whenever new companies are
    added to an existing batch, and search_contact bills real credits per call, so re-searching
    an already-resolved company on every re-run would silently double-bill it."""
    companies = db.query(Company).filter(Company.batch_id == batch_id).all()

    found = 0
    not_found = 0
    skipped = 0
    for company in companies:
        if company.contacts:
            skipped += 1
            continue
        contact = find_decision_maker(company, db)
        if contact:
            found += 1
        else:
            not_found += 1

    return {
        "companies_checked": len(companies),
        "decision_makers_found": found,
        "companies_with_no_contact": not_found,
        "companies_skipped_already_resolved": skipped,
    }
