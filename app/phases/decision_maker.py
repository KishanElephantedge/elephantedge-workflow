"""
Phase 6 — Decision Maker Intelligence.

Multi-persona target (raised from a single-threaded target, 2026-08-20, per an urgent live
product decision -- outreach to one contact per company means the whole company's chance
depends on one person's inbox habits): up to MAX_CONTACTS_PER_COMPANY real people per company,
spanning BOTH the Founder/CEO/Co-Founder family and the Head of Sales/VP Sales/Head of GTM
family, not "secondary tier only when the primary is missing." Deterministic role relevance
only (title-keyword matching) -- no numeric lead score is invented to rank within a tier.

At most 2 search_contact calls per company regardless (one per tier), same cost-bounding as
the original single-contact design -- the sales-leader tier is only called when the CEO tier's
own already-paid-for candidates (CANDIDATES_PER_SEARCH per call) didn't fill the quota.

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

# Third fallback tier (2026-08-20, explicit product decision): tried only when the CEO/Founder
# tier and the sales-leader tier together still haven't filled the contact quota for a
# company. Broadens beyond "sales leadership specifically" to any other real senior leader --
# Vice President generally (not just VP Sales) and CTO/Chief Technology Officer, both
# deliberately excluded from the tiers above (VP was excluded from the CEO tier's bare-
# "president" check specifically to avoid false-positiving on "Vice President"; CTO was never
# searched for at all until now). CEO/Founder/Co-Founder/President are included again here too
# -- a company can have more than one person in that family (e.g. two co-founders), and this
# tier's title match runs against whatever this call's own candidates are, independent of who
# the earlier tiers already found (seen_keys dedup still applies).
BROADER_LEADERSHIP_FILTER = (
    "CEO OR Chief Executive Officer OR Founder OR Co-Founder OR President OR Vice President OR "
    "VP OR CTO OR Chief Technology Officer"
)
BROADER_LEADERSHIP_TITLE_KEYWORDS = [
    "ceo", "chief executive officer", "founder", "president", "vice president", "vp",
    "cto", "chief technology officer",
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


def _matching_persons(
    persons: list[dict], title_keywords: list[str], require_bare_president: bool = False, exclude_keys: set | None = None
) -> list[dict]:
    """Returns EVERY candidate whose actual title contains one of our target keywords, not
    just the first -- the same broad OR-filter call already returns up to CANDIDATES_PER_SEARCH
    real people in one billed request; previously only persons[0]'s match was kept and the
    rest silently discarded even when several were real, distinct decision-makers (e.g. both a
    Founder and a Co-Founder). Rank is still not blindly trusted -- a title-keyword match is
    required, same as before, since the provider's own relevance rank can and does put the
    wrong person first (e.g. an Executive Vice President ranked above the real CEO).

    require_bare_president=True additionally accepts a title containing "president" on its
    own (the CEO tier's case) -- but only if it's NOT one of the "Vice President" family,
    since "president" is a substring of "Vice President"/"Executive Vice President" and a
    naive `in` check would otherwise accept those too (confirmed live: this exact bug).

    exclude_keys, if given, is both read AND mutated in place -- callers pass the same set
    across multiple tiers/sources so a person already picked up (free layer, or the CEO tier
    call) is never counted twice against the same company's contact quota."""
    exclude_keys = exclude_keys if exclude_keys is not None else set()
    matches = []
    for person in persons:
        title = (person.get("title") or "").lower()
        is_match = any(keyword in title for keyword in title_keywords)
        if not is_match and require_bare_president and "president" in title and not any(excl in title for excl in CEO_TITLE_PRESIDENT_EXCLUSIONS):
            is_match = True
        if not is_match:
            continue
        key = (str(person.get("first_name") or "").strip().lower(), str(person.get("last_name") or "").strip().lower())
        if key in exclude_keys:
            continue
        exclude_keys.add(key)
        matches.append(person)
    return matches


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


MAX_CONTACTS_PER_COMPANY = 3


def find_decision_makers(
    company: Company, db: Session, tenant_id: int, allow_paid_fallback: bool = True, max_contacts: int = MAX_CONTACTS_PER_COMPANY,
    existing_contacts: list[Contact] | None = None,
) -> tuple[list[Contact], bool]:
    """Multi-contact version -- fetches up to max_contacts real decision-makers per company,
    spanning both the founder/CEO/Co-Founder family and the sales-leader family (Head of
    Sales/VP Sales/Head of GTM/etc.), rather than a single primary contact with a sales-leader
    fallback tried only when the primary is missing. Real-work rationale: outreach to a single
    contact per company means the whole company's chance depends on one person's inbox habits;
    multiple real, role-relevant contacts at the same company materially raises the odds of a
    reply. Deterministic role relevance only (title-keyword matching, same as before) -- no
    numeric lead score is invented for ranking within the tiers.

    existing_contacts (V2 Phase 3 addition, optional, default None -- V1's own callers never
    pass this, so their behavior is completely unchanged): Contact rows this company ALREADY
    has (e.g. from a prior run). When given, max_contacts is treated as the TOTAL desired
    contact count for the company (existing + new), not "new contacts on top of existing" --
    and every existing contact's name is seeded into the dedup set, so a fresh search that
    happens to surface the same real person again never creates a duplicate Contact row for
    them. This is what lets a caller "top up" a company that already has 1 of 3 desired
    contacts without re-fetching (and re-paying for) someone already known.

    Returns (contacts, used_paid_fallback) -- contacts here are only the NEWLY created rows,
    never re-wraps existing_contacts. used_paid_fallback is True whenever the paid Deepline
    search_contact path was actually called -- regardless of how many (if any) it found --
    since search_contact bills per call, not per hit. Callers that need to bound real spend
    across a batch (see autonomous_orchestrator.py's per-run paid-fallback cap) count on this
    to know when money was actually spent, not just when a contact was found.

    allow_paid_fallback=False skips the paid path entirely once the free layer's contacts are
    collected -- used once a caller's own per-run cap on paid attempts has been reached, so
    remaining companies that miss/undershoot the free layer simply get fewer contacts instead
    of costing more."""
    seen_keys: set[tuple[str, str]] = set()
    for existing in existing_contacts or []:
        seen_keys.add((str(existing.first_name or "").strip().lower(), str(existing.last_name or "").strip().lower()))
    target = max(0, max_contacts - len(existing_contacts or []))
    contacts: list[Contact] = []

    # Free/cheap resolution first (Jobo leadership + verified Apify LinkedIn lookup, now
    # multi-candidate) -- a miss here costs nothing and falls straight through to the existing
    # paid Deepline flow below for whatever quota it leaves unfilled. Deferred import:
    # free_decision_maker.py imports the title-keyword constants from this module, so a
    # top-level import here would be circular.
    from app.phases.free_decision_maker import find_free_decision_makers
    for free_person in find_free_decision_makers(db, tenant_id, company, target):
        key = (str(free_person.get("first_name") or "").strip().lower(), str(free_person.get("last_name") or "").strip().lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        contacts.append(_make_contact(company, db, tenant_id, free_person, free_person["reasoning"], free_person["thread_role"]))
        if len(contacts) >= target:
            break

    if len(contacts) >= target or not allow_paid_fallback:
        return contacts, False

    used_paid = False

    # CEO/Founder tier -- search_contact already returns up to CANDIDATES_PER_SEARCH real
    # candidates in this one billed call; every one whose title actually matches is kept (not
    # just the top-ranked), filling the quota from a call we're already paying for.
    persons = _run_search_contact(company, {"title_filters": [{"name": "ceo_filter", "filter": CEO_FILTER}]})
    used_paid = True
    for person in _matching_persons(persons, CEO_TITLE_KEYWORDS, require_bare_president=True, exclude_keys=seen_keys):
        if len(contacts) >= target:
            break
        contacts.append(_make_contact(company, db, tenant_id, person, f"title_filter={CEO_FILTER}, verified_title={person.get('title')!r}", "founder_ceo"))

    # Sales-leader tier -- only called if the quota still isn't filled (same cost-bounding
    # principle throughout: never more paid calls than the quota genuinely still needs), not
    # "only when zero primary found" -- a company can genuinely want both a founder AND a
    # sales-leader contact.
    if len(contacts) < target:
        persons = _run_search_contact(company, {"title_filters": [{"name": "sales_leader_filter", "filter": SALES_LEADER_FILTER}]})
        used_paid = True
        for person in _matching_persons(persons, SALES_LEADER_TITLE_KEYWORDS, exclude_keys=seen_keys):
            if len(contacts) >= target:
                break
            contacts.append(_make_contact(company, db, tenant_id, person, f"title_filter={SALES_LEADER_FILTER}, verified_title={person.get('title')!r}", "sales_leader"))

    # Broader leadership tier -- last resort, only called if the first two tiers together
    # still haven't filled the quota. Widens to Vice President generally and CTO, plus another
    # pass at CEO/Founder/President in case this call's own candidate set surfaces someone the
    # first (differently-ranked) CEO-tier call didn't.
    if len(contacts) < target:
        persons = _run_search_contact(company, {"title_filters": [{"name": "broader_leadership_filter", "filter": BROADER_LEADERSHIP_FILTER}]})
        used_paid = True
        for person in _matching_persons(persons, BROADER_LEADERSHIP_TITLE_KEYWORDS, exclude_keys=seen_keys):
            if len(contacts) >= target:
                break
            contacts.append(_make_contact(company, db, tenant_id, person, f"title_filter={BROADER_LEADERSHIP_FILTER}, verified_title={person.get('title')!r}", "other_leadership"))

    return contacts, used_paid


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
        contacts, _ = find_decision_makers(company, db, tenant_id)
        company.decision_maker_searched_at = datetime.utcnow()
        db.commit()
        if contacts:
            found += len(contacts)
            # HubSpot sync is a side effect of a successful search, not a precondition for
            # it -- a HubSpot outage or bad token must never fail the Decision Maker phase.
            # The error is still surfaced in the response, not swallowed silently, so a bad
            # token doesn't go unnoticed. Every contact for this company is synced, not just
            # one -- HubSpot's own object model already supports multiple contacts per company.
            for contact in contacts:
                try:
                    sync_to_hubspot(company, contact, db, tenant_id)
                    hubspot_synced += 1
                except HubSpotError as e:
                    hubspot_errors.append(f"{company.name} ({contact.first_name} {contact.last_name}): {e}")
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
