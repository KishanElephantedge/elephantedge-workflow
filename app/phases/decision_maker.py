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
from app.deepline_client import DeeplineError, execute_tool, extract_rows
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
    """Real bug fix (2026-08-18): this call used to have no error handling at all -- a single
    real failure (confirmed live: Deepline balance hit $0, crustdata_v3_person_search errored
    out) propagated all the way up through find_decision_maker() and the per-company loop in
    _run_apify_autonomous_cycle(), crashing the ENTIRE day's autonomous run outright, discarding
    tracking of whatever companies discovery had already found that day (AutonomousRun.
    companies_discovered was never set, since the crash happened before that assignment).

    Now: a DeeplineError here is treated the same as a genuine "no candidates found" result for
    THIS ONE company -- find_decision_maker() falls through to the sales-leader tier and then
    to "no contact" exactly as it already does for a real empty response, and the run continues
    to the next company instead of dying. The caller's own PAID_DECISION_MAKER_FALLBACK_CAP
    still bounds how many companies can hit this failure in one run (a real, external outage
    like a $0 Deepline balance won't resolve mid-run, so retrying every remaining company would
    just repeat the same failure) -- this fix doesn't change that cap, only stops one company's
    failure from being treated as the whole run's failure."""
    payload = {
        "domain": company.domain,
        "page_size": CANDIDATES_PER_SEARCH,
        **extra_payload,
    }
    try:
        response = execute_tool("search_contact", payload)
    except DeeplineError:
        return []
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


def _make_contact(company: Company, db: Session, tenant_id: int, person: dict, reasoning: str, thread_role: str) -> Contact:
    # Free -- already present in search_contact's own response, just never extracted before
    # (confirmed live 2026-08-10). professional_email preferred over personal_email since it's
    # the one actually tied to the company domain we're targeting. Only ever populated via this
    # (paid Deepline) path -- free_decision_maker.py's Jobo+Apify contacts have no email source.
    email = person.get("professional_email") or person.get("personal_email")
    contact = Contact(
        company_id=company.id,
        first_name=person.get("first_name"),
        last_name=person.get("last_name"),
        title=person.get("title"),
        linkedin_url=person.get("linkedin_url") or person.get("linkedin"),
        email=email,
        email_source="deepline" if email else None,
        thread_role=thread_role,
        matched_title_reasoning=reasoning,
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)

    if not contact.email:
        # Deferred import -- same circular-import reason as the free-path call in
        # find_decision_maker below.
        from app.phases.free_decision_maker import resolve_fallback_email
        fallback = resolve_fallback_email(db, tenant_id, company, contact.first_name)
        if fallback:
            contact.email, contact.email_source = fallback
            db.commit()

    return contact


def find_decision_maker(company: Company, db: Session, tenant_id: int, allow_paid_fallback: bool = True) -> tuple[Contact | None, bool]:
    """Returns (contact, used_paid_fallback). used_paid_fallback is True whenever the paid
    Deepline search_contact path was actually called -- regardless of whether it found
    anyone -- since search_contact bills per call, not per hit. Callers that need to bound
    real spend across a batch (see autonomous_orchestrator.py's per-run paid-fallback cap,
    added 2026-08-11) count on this to know when money was actually spent, not just when a
    contact was found.

    allow_paid_fallback=False skips the paid path entirely on a free miss -- used once a
    caller's own per-run cap on paid attempts has been reached, so remaining companies that
    miss the free layer simply get no contact instead of costing more."""
    # Free/cheap resolution first (Jobo leadership + verified Apify LinkedIn lookup) -- a miss
    # here costs nothing and falls straight through to the existing paid Deepline flow below,
    # unchanged. Deferred import: free_decision_maker.py imports the title-keyword constants
    # from this module, so a top-level import here would be circular.
    from app.phases.free_decision_maker import find_free_decision_maker
    free_person = find_free_decision_maker(db, tenant_id, company)
    if free_person:
        return _make_contact(company, db, tenant_id, free_person, free_person["reasoning"], free_person["thread_role"]), False

    if not allow_paid_fallback:
        return None, False

    persons = _run_search_contact(company, {"title_filters": [{"name": "ceo_filter", "filter": CEO_FILTER}]})
    person = _best_matching_person(persons, CEO_TITLE_KEYWORDS, require_bare_president=True)
    if person:
        return _make_contact(company, db, tenant_id, person, f"title_filter={CEO_FILTER}, verified_title={person.get('title')!r}", "founder_ceo"), True

    # No primary (founder/CEO) contact exists at all for this domain -- try the secondary
    # sales-leader target rather than leaving the company with zero contact.
    persons = _run_search_contact(company, {"title_filters": [{"name": "sales_leader_filter", "filter": SALES_LEADER_FILTER}]})
    person = _best_matching_person(persons, SALES_LEADER_TITLE_KEYWORDS)
    if person:
        return _make_contact(company, db, tenant_id, person, f"title_filter={SALES_LEADER_FILTER}, verified_title={person.get('title')!r}", "sales_leader"), True

    return None, True


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
        # NOTE: the old score.tier == "excluded" gate was removed here -- under the new
        # 18-variable GTM Fit Score, "excluded" is the default outcome for most real
        # companies (four variables score 0 until built, so max achievable is below the
        # "cool" threshold), so this would have rejected almost everything before the real
        # gates below even ran. Score is now a ranking signal, not a pass/fail gate -- the
        # real gates are has_qualifying_hiring_signal and the team-composition check.
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
        contact, _ = find_decision_maker(company, db, tenant_id)
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
