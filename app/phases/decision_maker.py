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
filter, not any single exact-title step).

search_contact's own top-ranked result is NOT trusted blindly -- confirmed live (Gulf &
Western Industries) that a broad OR-filter like ours can rank the wrong person #1 (an
Executive Vice President, when the real President & CEO also exists in the same company's
data, per a manual LinkedIn Sales Navigator check). Each call now requests a few candidates
(CANDIDATES_PER_SEARCH) in the SAME billed call -- not a second call -- and locally picks
whichever one's actual title contains one of our target keywords, rather than accepting
whatever the provider's relevance ranking put first.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.budget_guard import BudgetExceededError, BudgetGuard
from app.db.models import Company, Contact
from app.deepline_client import execute_tool, extract_rows
from app.hubspot_client import HubSpotError
from app.phases.hiring_signal import assess_team_composition, has_qualifying_hiring_signal
from app.phases.hubspot_sync import sync_to_hubspot

CEO_FILTER = "CEO OR Chief Executive Officer OR Founder OR Co-Founder OR Owner OR Managing Director OR President"
# "president" deliberately excluded from this plain list -- it's a substring of "Vice
# President"/"Executive Vice President"/"Assistant Vice President", which are NOT what we
# want (confirmed live: this exact substring bug initially made the matcher accept an
# Executive Vice President too). Handled separately below with an explicit exclusion check.
CEO_TITLE_KEYWORDS = ["ceo", "chief executive officer", "founder", "owner", "managing director"]
CEO_TITLE_PRESIDENT_EXCLUSIONS = ["vice president", "svp", "avp", "evp"]

SALES_LEADER_FILTER = (
    "Head of Sales OR VP Sales OR VP of Sales OR Head of GTM OR Head of Growth OR "
    "Director of Sales OR Director of Business Development OR Head of Business Development OR "
    "Chief Revenue Officer OR CRO"
)
SALES_LEADER_TITLE_KEYWORDS = [
    "head of sales", "vp sales", "vp of sales", "head of gtm", "head of growth",
    "director of sales", "director of business development", "head of business development",
    "chief revenue officer", "cro",
]

# search_contact's own tool guidance: broad OR-filters like ours rank less precisely than
# narrow ones, and recommends a small page_size (1-3) specifically "so you can inspect
# candidate quality" -- confirmed live (Gulf & Western Industries: the #1-ranked result was
# an Executive Vice President, not the actual President & CEO who also exists in the same
# company's data, verified manually on LinkedIn Sales Navigator). Requesting a few
# candidates in the SAME billed call (not a second call -- $0.056/result, so 3 costs
# ~$0.17 instead of ~$0.056) and picking the one whose title actually contains one of our
# target words fixes this without any wasted/duplicate spend.
CANDIDATES_PER_SEARCH = 3


def _run_search_contact(company: Company, extra_payload: dict) -> list[dict]:
    payload = {
        "domain": company.domain,
        "page_size": CANDIDATES_PER_SEARCH,
        **extra_payload,
    }
    response = execute_tool("search_contact", payload)
    raw = response.get("toolResponse", {}).get("raw", {})
    if isinstance(raw, dict) and isinstance(raw.get("output"), dict):
        persons = raw["output"].get("persons")
        if isinstance(persons, list):
            return persons
    return extract_rows(response, "persons")


def _best_matching_person(persons: list[dict], title_keywords: list[str], require_bare_president: bool = False) -> dict | None:
    """Picks the first candidate whose actual title contains one of our target keywords,
    rather than blindly trusting persons[0]'s relevance rank -- the rank can and does put
    the wrong person first (e.g. an Executive Vice President ranked above the real CEO).

    require_bare_president=True additionally accepts a title containing "president" on its
    own (the CEO tier's case) -- but only if it's NOT one of the "Vice President" family,
    since "president" is a substring of "Vice President"/"Executive Vice President" and a
    naive `in` check would otherwise accept those too (confirmed live: this exact bug)."""
    for person in persons:
        title = (person.get("title") or "").lower()
        if any(keyword in title for keyword in title_keywords):
            return person
        if require_bare_president and "president" in title and not any(excl in title for excl in CEO_TITLE_PRESIDENT_EXCLUSIONS):
            return person
    return None


def _make_contact(company: Company, db: Session, person: dict, reasoning: str, thread_role: str) -> Contact:
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
    person = _best_matching_person(persons, CEO_TITLE_KEYWORDS, require_bare_president=True)
    if person:
        return _make_contact(company, db, person, f"title_filter={CEO_FILTER}, verified_title={person.get('title')!r}", "founder_ceo")

    # No primary (founder/CEO) contact exists at all for this domain -- try the secondary
    # sales-leader target rather than leaving the company with zero contact.
    persons = _run_search_contact(company, {"title_filters": [{"name": "sales_leader_filter", "filter": SALES_LEADER_FILTER}]})
    person = _best_matching_person(persons, SALES_LEADER_TITLE_KEYWORDS)
    if person:
        return _make_contact(company, db, person, f"title_filter={SALES_LEADER_FILTER}, verified_title={person.get('title')!r}", "sales_leader")

    return None


def run_decision_maker_id(batch_id: int, db: Session, tenant_id: int, retry_company_ids: list[int] | None = None, budget_guard: BudgetGuard | None = None) -> dict:
    """Phase 6 entrypoint.

    Real gate, added after finding real batches (e.g. batch 21) where every company had zero
    hiring signal, yet all were searched and contacts got pushed to a real campaign anyway:
    a company only reaches search_contact if has_qualifying_hiring_signal() is true -- a
    role-title match OR real JD-content evidence, either is enough. Firmographic fit alone
    (passing Discovery) is not sufficient; a company showing no evidence at all of needing what
    our product does gets excluded here, before the most expensive phase runs for it.

    A company's score tier is checked here too, not just hiring-signal presence -- a company
    can have a weak hiring signal (enough to pass has_qualifying_hiring_signal) yet still
    score "excluded" overall once ability-to-pay/outbound-maturity/product-fit are factored
    in. Excluded companies are never deleted (see _select_top_companies in
    autonomous_orchestrator.py -- kept fully visible for review), so this check has to
    happen here, at the point of the expensive action, not rely on the row being absent.

    By default, skips any company already searched -- whether it found a contact or not.
    search_contact bills real credits per call regardless of outcome, and a company with
    genuinely no findable record would otherwise get re-billed on every single re-run forever,
    including a future autonomous daily cycle, since a "not found" result never produces a
    Contact row to skip on its own.

    retry_company_ids overrides the skip for those specific companies only -- an explicit,
    deliberate re-attempt (e.g. after correcting a wrong domain), never an automatic one.

    budget_guard, if given, is checked after every company -- this is the single most
    expensive phase per company (search_contact, ~$0.17 at CANDIDATES_PER_SEARCH=3, real
    per-result price is $0.056), so this is the loop where a hard
    spend cap matters most. Stops the loop (rather than raising out of it) the moment this
    run's cap is reached, leaving already-resolved companies' contacts in place."""
    retry_ids = set(retry_company_ids or [])
    companies = db.query(Company).filter(Company.batch_id == batch_id).all()

    found = 0
    not_found = 0
    skipped = 0
    excluded_no_signal = 0
    excluded_low_score = 0
    excluded_full_team = 0
    hubspot_synced = 0
    hubspot_errors = []
    budget_stopped_early = False
    for company in companies:
        already_done = company.contacts or company.decision_maker_searched_at
        if already_done and company.id not in retry_ids:
            skipped += 1
            continue
        if company.score is not None and company.score.tier == "excluded":
            excluded_low_score += 1
            continue
        if not has_qualifying_hiring_signal(company):
            excluded_no_signal += 1
            continue
        # Team-composition gate -- a company can pass has_qualifying_hiring_signal (a real
        # posting exists) yet already have a full GTM team or have GTM covered internally by
        # the hiring role's own occupant (found live: SmartWinnr, 12 sales + 10 marketing,
        # whose "Sr. Program Manager" hire personally does GTM per her own profile). Checked
        # here, before the expensive search_contact call, same as the score-tier gate above.
        team_fit = assess_team_composition(company, db)
        if team_fit["tier"] == "excluded":
            excluded_full_team += 1
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
        "companies_excluded_no_hiring_signal": excluded_no_signal,
        "companies_excluded_low_score": excluded_low_score,
        "companies_excluded_full_team": excluded_full_team,
        "budget_stopped_early": budget_stopped_early,
    }
