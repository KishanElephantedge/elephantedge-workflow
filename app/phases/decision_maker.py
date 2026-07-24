"""
Phase 6 — Decision Maker Intelligence.

Single-threaded waterfall, not multi-persona simultaneous search like Synefi's. This is a
direct, confirmed decision (not assumed): at Elephant Edge's SMB-scale ICP, there is no real
buying committee -- Gokul confirmed the founder/CEO is the actual decision-maker, with a
Head of Sales/GTM person (where one even exists) acting as an influencer at most, not a
parallel target. Phase 5 (Buying Committee Discovery) is effectively answered by this single
fact, not separately built out.

Reaches, in order, stopping at the first hit: CEO -> Founder -> Co-Founder. A broader
boolean-phrase fallback runs only if all three exact-title steps miss.
"""

from sqlalchemy.orm import Session

from app.db.models import Company, Contact
from app.deepline_client import execute_tool, extract_rows

CEO_WATERFALL = [
    ["CEO", "Chief Executive Officer"],
    ["Founder"],
    ["Co-Founder", "Cofounder"],
]

# Tried only after every exact-title step above has missed. Uses search_contact's
# title_filters mode (boolean phrase matching) rather than exact title_lists, since the
# point is to catch title variance the exact list doesn't anticipate (e.g. "Owner",
# "Managing Director", "President") -- same fallback principle as Synefi's persona search,
# adapted to a single-target waterfall instead of a persona set.
CEO_FALLBACK_FILTER = "CEO OR Chief Executive Officer OR Founder OR Co-Founder OR Owner OR Managing Director OR President"


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
    for step_index, titles in enumerate(CEO_WATERFALL):
        persons = _run_search_contact(company, {"title_lists": [{"name": f"ceo_step_{step_index}", "titles": titles}]})
        if persons:
            return _make_contact(company, db, persons, f"step={step_index}, titles={titles}")

    persons = _run_search_contact(company, {"title_filters": [{"name": "ceo_fallback", "filter": CEO_FALLBACK_FILTER}]})
    if persons:
        return _make_contact(company, db, persons, f"fallback title_filter={CEO_FALLBACK_FILTER}")

    return None


def run_decision_maker_id(batch_id: int, db: Session) -> dict:
    """Phase 6 entrypoint. No tier/score gate here -- this batch's companies were hand-picked
    (Phase 3 Qualification doesn't apply), so every company in the batch is searched."""
    companies = db.query(Company).filter(Company.batch_id == batch_id).all()

    found = 0
    not_found = 0
    for company in companies:
        contact = find_decision_maker(company, db)
        if contact:
            found += 1
        else:
            not_found += 1

    return {
        "companies_checked": len(companies),
        "decision_makers_found": found,
        "companies_with_no_contact": not_found,
    }
