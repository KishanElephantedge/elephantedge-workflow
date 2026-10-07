"""Play F -- ICP filters only (no signal): for partners whose buyer is defined by firmographics.

    search     one HarvestAPI LinkedIn people search built from the partner's own ICP (company
               headcount band, decision-maker titles, geography) -- $0.07 a page of 25 people,
               each with a LinkedIn URL. A cursor continues from the next page on the next run.
    verify     FREE first: an obvious vendor/agency/recruiter is rejected by name alone (no
               lookup at all), then the free public LinkedIn company page decides size fit for
               everyone else. The PAID company lookup ($0.003) is only spent on a company that
               survives both free checks -- see the 2026-09-28 fix note below.
    qualify    ONE LLM call per company against the partner's ICP notes -> qualified / rejected,
               and (2026-09-28 fix) writes the same Problem->Demand->Opportunity->Strategy rows
               every other play writes, so a qualified lead is actually visible in the
               partner's own Pipeline/Accounts dashboard -- a qualified lead used to dead-end as
               a bare GtmLead row nobody could see or act on
    contact    free -- the person found by the search IS the contact (LinkedIn outreach)

Rows are written to the PARTNER's tenant (their data stays theirs), while every paid call is
reserved against the BILLING tenant's combined budget (Elephant Edge's -- the Deepline account
and the Gemini key belong to it), the same arrangement as the existing partner discovery runs.

REAL FIX, 2026-09-28 (first production run: 17 companies found, 0 qualified, $0.254 spent). 14 of
17 were rejected purely on company SIZE (HarvestAPI's people search can only filter by LinkedIn's
own wide buckets -- "11-50" for a 30-100 ICP band pulls in plenty of real 11-29-person companies
too, a structural limit of the bucket system, not a bug) and 2 more were an obvious consultancy
and an obvious staffing agency, rejected by name alone. Every one of those 16 still paid the full
$0.003 harvestapi_get_company lookup before being rejected -- exactly the "fetch, then discover
it was never in the ICP" waste flagged live. This module already had the free public-page check
(app/phases/company_profile_check.py's fetch_public_company_profile, used by the hiring play's
Apify path) available and simply wasn't using it here. Now: a free company-name keyword match
rejects an obvious vendor with zero lookup, and the free page's own declared size band is checked
BEFORE ever calling the paid endpoint -- which is now reserved only for companies that survive
both free filters, i.e. the ones actually worth a paid look."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.models import Batch, CampaignPush, Company, Contact, Parameter
from app.gtm_os.plays.lead import (
    STATE_CONTACT_FOUND, STATE_QUALIFIED, STATE_REJECTED, STATE_SIGNAL, GtmLead, normalize_linkedin_url,
)
from app.spend_ledger import SpendBlocked

logger = logging.getLogger(__name__)

PLAY = "icp_filters"
BILLING_TENANT_ID = 2
CURSOR_KEY = "icp_filters_play_cursor"
EXHAUSTION_COOLDOWN_DAYS = 7  # how long to wait, once a filter set is fully paged through,
                              # before paying to check it again (see search_icypeas)
# _resolve_decision_makers_batch: company NAMES, not URLs (a real bug -- see that function's
# own comment), so a page mixes real matches with same-named unrelated companies. Smaller batch
# and more pages than the URL-based hiring play uses, so real matches aren't buried in noise;
# not yet tuned against real yield data, first value chosen deliberately conservative.
NAME_BATCH_SIZE = 15
NAME_SEARCH_PAGES = 3
# LinkedIn / Sales Navigator company-size buckets.
HEADCOUNT_BUCKETS = [(1, 10), (11, 50), (51, 200), (201, 500), (501, 1000), (1001, 5000), (5001, 10000), (10001, 10**9)]

# Free, name-only rejection -- the same disqualifiers the Qualifier prompt below already states
# ("the company sells sales, marketing, consulting, coaching, agency or recruiting services
# itself"). A company whose own name says this needs no paid lookup to confirm it.
_VENDOR_NAME_PATTERN = re.compile(
    r"\b(agency|agencies|consulting|consultants?|consultancy|recruiters?|recruiting|staffing|"
    r"talent acquisition|talent solutions|headhunt(?:ers?|ing)|coaching|coaches|advisory|advisors?)\b",
    re.IGNORECASE,
)


def _looks_like_a_vendor(company_name: str | None) -> bool:
    return bool(company_name) and bool(_VENDOR_NAME_PATTERN.search(company_name))


# Free safety net, 2026-10-04: Icypeas' query-level `type.exclude` (NON_COMPANY_TYPES, below)
# clearly isn't catching everything -- "Town of Rockport" (type/industry came back blank),
# "Longboat Key Fire Rescue" (industry "Public Safety"), and "Lakeland Elementary Schools"
# (industry "Education Management") all slipped through a live run despite Government Agency/
# Educational Institution being excluded. Rather than trust the provider's own type field alone,
# catch the same obvious cases for free, the same way _looks_like_a_vendor() already does by
# name -- a municipal government or school's own name almost always says so.
_GOVERNMENT_EDUCATION_NAME_PATTERN = re.compile(
    r"\b(town of|city of|county of|township of|village of|borough of|municipal(?:ity)?|"
    r"fire (?:rescue|department)|police department|sheriff'?s? (?:office|department)|"
    r"elementary school|middle school|high school|school district|public schools?|"
    r"independent school district)\b",
    re.IGNORECASE,
)
_GOVERNMENT_EDUCATION_INDUSTRIES = {
    "Government Administration", "Public Safety", "Law Enforcement",
    "Primary and Secondary Education", "Primary/Secondary Education", "Education Management",
    "Higher Education", "Education Administration Programs", "Military", "Judiciary",
}


def _looks_like_government_or_education(company_name: str | None, industry: str | None) -> bool:
    if company_name and _GOVERNMENT_EDUCATION_NAME_PATTERN.search(company_name):
        return True
    return bool(industry) and industry in _GOVERNMENT_EDUCATION_INDUSTRIES


def _size_fits(band: tuple[int, int | None] | None, lo: int, hi: int) -> bool | None:
    """False (confident reject) only when the band is CONFIRMED TOO BIG -- its whole declared
    range sits above the ICP's max. Never rejects on "too small": real bug found live
    2026-09-28 -- BePresent's public page declares "2-10 employees" while its real, paid-lookup
    count is 31, genuinely inside a 30-100 ICP. LinkedIn's self-declared band understates a
    company that has grown since it was last updated (the exact asymmetry
    company_profile_check.py's own profile_rejection_reason() already documented for the
    hiring play -- "TOO BIG only, never too small" -- this play just hadn't reused it). A
    band that looks small, or has no band at all, returns None so the caller always falls back
    to the paid, exact-count lookup rather than risk discarding a real fit."""
    if not band:
        return None
    band_lo, _band_hi = band
    return False if band_lo > hi else None


def headcount_ranges(lo: int | None, hi: int | None) -> str | None:
    """Every LinkedIn bucket that overlaps the ICP band (30-100 -> "11-50,51-200"); the exact
    count is checked per company afterwards."""
    if lo is None and hi is None:
        return None
    lo, hi = lo or 1, hi or 10**9
    labels = [f"{a}-{b}" if b < 10**9 else "10001+" for a, b in HEADCOUNT_BUCKETS if a <= hi and b >= lo]
    return ",".join(labels) or None


def search_filters(icp: dict) -> dict:
    return {
        "companyHeadcount": headcount_ranges(icp.get("employee_min"), icp.get("employee_max")),
        "currentJobTitles": ",".join(icp.get("decision_maker_titles") or ["Owner", "Founder", "CEO"]),
        "locations": ",".join(icp.get("geographies") or ["United States"]),
    }


def _cursor(db: Session, tenant_id: int) -> Parameter:
    param = db.query(Parameter).filter(Parameter.tenant_id == tenant_id, Parameter.key == CURSOR_KEY).first()
    if param is None:
        param = Parameter(tenant_id=tenant_id, key=CURSOR_KEY, value={},
                          description="Play F: next LinkedIn people-search page (reset when the ICP filters change)")
        db.add(param)
        db.commit()
    return param


def _batch(db: Session, tenant_id: int) -> Batch:
    name = f"ICP filter search -- {datetime.utcnow().date().isoformat()}"
    batch = db.query(Batch).filter(Batch.tenant_id == tenant_id, Batch.name == name).first()
    if batch is None:
        batch = Batch(tenant_id=tenant_id, name=name, source="play_icp_filters", status="in_progress")
        db.add(batch)
        db.commit()
    return batch


# Icypeas' own LinkedIn "company type" categories that are never a real operating buyer --
# caught for free at search time (2026-09-28 finding: "SaaS Alliance", type "Educational
# Institution", was really a community/Slack group, not a company, and would have cost a real
# lookup to discover under the old bucket-search path).
NON_COMPANY_TYPES = ["Educational Institution", "Government Agency", "Nonprofit", "Non-profit Organizations",
                     "Self-Employed", "Self-Owned"]
# LinkedIn's own industry taxonomy -- the same disqualifiers _looks_like_a_vendor() catches by
# name, now also excluded at the SEARCH level so a vendor/agency never gets fetched at all.
NON_BUYER_INDUSTRIES = ["Staffing and Recruiting", "Management Consulting", "Marketing and Advertising",
                        "Business Consulting and Services", "Human Resources Services", "IT Services and IT Consulting"]


def icypeas_filters_for_icp(icp: dict, db: Session | None = None, tenant_id: int | None = None) -> dict:
    """REAL FIX, 2026-10-04: the partner's own stated `industries` was never read here -- the
    search only ever enforced headcount/geography plus a fixed vendor-exclude list, regardless
    of what industry the partner actually told us to target. Found live against Majji's new
    Professional Services ICP: 21/21 companies returned were hospitals, law firms, construction,
    manufacturing, insurance, a fire department, a school district -- everything BUT Professional
    Services, because nothing ever told Icypeas to only include it. `industry.include` is now
    built from the partner's own icp['industries'] whenever set, same as `location.include`
    already reads icp['geographies'] -- so the search is actually driven by that specific
    partner's ICP, not a hardcoded assumption. The vendor-exclude list stays on unconditionally
    underneath it (never show an agency/staffing firm even if a partner's broad industry list
    would technically include it).

    REGISTRY-DRIVEN, 2026-10-07: the filter payload is no longer hand-built here. It is rendered
    from app/gtm_os/sourcing/registry.py, which holds the exact filter expression Icypeas accepts
    for each ICP atom, taken from its published schema. A call site can no longer invent a field
    name or quietly forget one -- forgetting `industries` is precisely what sent 21 hospitals and
    law firms into a Professional Services ICP. Anything the provider cannot enforce comes back
    from icp_coverage() as a residual/unsupported atom instead of vanishing.

    The two exclude lists below are OUR policy, not the partner's ICP, so they are applied on top
    of the rendered atoms rather than living in the registry: we never want an agency or a
    government body regardless of what any partner asks for.
    """
    from app.gtm_os.sourcing.atoms import INDUSTRY, decompose_icp
    from app.gtm_os.sourcing.registry import ICYPEAS_FIND_COMPANIES, coverage_for

    icp_atoms = decompose_icp(icp)
    coverage = coverage_for(ICYPEAS_FIND_COMPANIES, icp_atoms)
    filters = dict(coverage.filters)

    # Phase 2, 2026-10-07: resolve the partner's words against Icypeas' REAL value space before
    # searching. "Professional Services" is not a value Icypeas has, so sending it as an industry
    # enum matched zero companies and cost $0.175 to discover. With a db session we can do better,
    # for free: use confirmed taxonomy values when we have learned any, and otherwise fall back to
    # Icypeas' own free-text `keyword` filter rather than a classification that matches nothing.
    # Without a session (unit tests, pure filter inspection) behaviour is unchanged.
    if db is not None:
        from app.gtm_os.sourcing.resolution import resolve_atom

        for atom in icp_atoms.by_key(INDUSTRY):
            resolved = resolve_atom(db, ICYPEAS_FIND_COMPANIES, atom, tenant_id=tenant_id)
            if resolved.resolved:
                filters.pop("industry", None)
                filters.update(resolved.filter_fragment)

    filters.setdefault("location", {"include": ["United States"]})
    industry = dict(filters.get("industry") or {})
    # Our standing policy excludes, plus whatever THIS partner's own runs have repeatedly
    # rejected. The partner's include list is protected: their stated intent outranks our
    # inference, so a value they asked for is never excluded by something we learned.
    excludes = list(NON_BUYER_INDUSTRIES)
    if db is not None and tenant_id is not None:
        try:
            from app.gtm_os.sourcing.atoms import INDUSTRY as _INDUSTRY
            from app.gtm_os.sourcing.exclusions import learned_exclusions

            protected = set(icp.get("industries") or []) | set(industry.get("include") or [])
            for value in learned_exclusions(db, tenant_id, "icypeas", _INDUSTRY, protected=protected):
                if value not in excludes:
                    excludes.append(value)
        except Exception as e:  # noqa: BLE001 -- never block a run on the exclusion lookup
            logger.warning("learned exclusions skipped: %s: %s", type(e).__name__, e)
    industry["exclude"] = excludes
    filters["industry"] = industry
    filters["type"] = {"exclude": NON_COMPANY_TYPES}
    return filters


def icp_coverage(icp: dict):
    """Which ICP requirements this play's provider actually enforces, and which it does not.

    Exposed so a run can REPORT its unsupported atoms instead of silently ignoring them -- the
    failure mode that let "no dedicated marketing hire" sit in Majji's ICP for days, enforced by
    nothing while looking configured.
    """
    from app.gtm_os.sourcing.atoms import decompose_icp
    from app.gtm_os.sourcing.registry import ICYPEAS_FIND_COMPANIES, coverage_for

    return coverage_for(ICYPEAS_FIND_COMPANIES, decompose_icp(icp))


def _resolve_decision_makers_batch(db: Session, tenant_id: int, companies: list, titles: list[str]) -> dict:
    """Phase 9, 2026-10-07: now a thin wrapper over the SHARED resolver
    (app/gtm_os/sourcing/decision_maker.py), which hiring.py's own decision-maker resolution also
    calls. This play used to have its own copy of this logic -- see that module's docstring for
    the two real bugs that drifted between the two copies before they were unified. Kept as a
    named function here (rather than inlining the shared call at each site in this file) so the
    rest of this play's code and its existing tests do not need to change shape."""
    from app.gtm_os.sourcing.decision_maker import resolve_decision_makers_batch

    return resolve_decision_makers_batch(
        db, tenant_id, companies, titles,
        default_thread_role="icp_filter_decision_maker",
        reasoning_label="HarvestAPI LinkedIn search (batched)",
    )


def _create_icypeas_lead(db: Session, tenant_id: int, key: str, company: Company, contact: Contact,
                         first_name: str | None, last_name: str | None, person_linkedin: str | None, co: dict) -> None:
    specialties = ", ".join(s.get("value") for s in (co.get("specialties") or []) if s.get("value"))[:300]
    evidence = (
        f"Person: {first_name or ''} {last_name or ''} -- {contact.title or ''}\n\n"
        f"Company: {co.get('name')} | {co.get('industry')} | {co.get('numberOfEmployees')} employees | "
        f"HQ {co.get('address')} | {co.get('website') or ''}\nSpecialties: {specialties}\nAbout: {co.get('description') or ''}"
    )
    db.add(GtmLead(tenant_id=tenant_id, play=PLAY, lead_key=key, company_id=company.id, contact_id=contact.id,
                   person_name=f"{first_name or ''} {last_name or ''}".strip(), person_linkedin_url=person_linkedin,
                   state=STATE_SIGNAL, evidence=evidence))
    db.commit()


def _process_icypeas_company(db: Session, tenant_id: int, co: dict, known_leads: set, pending: list,
                             department_atoms: list | None = None) -> str | None:
    """One search result -> a rejected lead, a created lead, or a deferral into `pending` for
    the batched paid decision-maker resolver. Returns the outcome label to count, or None when
    deferred (its real outcome is only known once the batch resolves). Raises OperationalError
    up to the caller on a dropped connection -- deliberately NOT caught here, so the caller can
    decide whether the page's progress (the cursor token) still gets saved regardless."""
    from app.phases.decision_maker_reasoning import select_best_decision_makers
    from app.phases.free_decision_maker import _jobo_leadership_candidates, _real_linkedin_url_from_jobo, _split_name

    url = co.get("url") or ""
    if "linkedin.com/company/" not in url:
        return "no_linkedin_url"
    slug = url.rstrip("/").rsplit("/company/", 1)[-1].split("?")[0]
    key = f"company:{slug}"
    if key in known_leads:
        return "known"
    known_leads.add(key)

    name = co.get("name") or ""
    industry_label = co.get("industry")
    if _looks_like_a_vendor(name):
        db.add(GtmLead(tenant_id=tenant_id, play=PLAY, lead_key=key, state=STATE_REJECTED,
                       qualifier_reason=f"company name matches a vendor/agency/recruiter pattern: {name!r}"))
        db.commit()
        return "vendor_name_match"
    if _looks_like_government_or_education(name, industry_label):
        db.add(GtmLead(tenant_id=tenant_id, play=PLAY, lead_key=key, state=STATE_REJECTED,
                       qualifier_reason=f"government/education body, not a real buyer: {name!r} (industry: {industry_label!r})"))
        db.commit()
        # Push this back into the QUERY so we stop paying for the category. Icypeas bills per
        # returned result, so a rejection we only apply locally is a row we bought for nothing --
        # and the next run buys it again. See exclusions.py for the two safeguards.
        try:
            from app.gtm_os.sourcing.atoms import INDUSTRY
            from app.gtm_os.sourcing.exclusions import record_rejection

            record_rejection(db, tenant_id, "icypeas", INDUSTRY, industry_label,
                             reason="government/education body")
        except Exception as e:  # noqa: BLE001 -- learning must never break a paid run
            logger.warning("exclusion learning skipped: %s: %s", type(e).__name__, e)
        return "government_or_education"

    company = (db.query(Company).join(Batch, Company.batch_id == Batch.id)
               .filter(Batch.tenant_id == tenant_id, Company.linkedin_url == url).first())
    if company is not None and db.query(CampaignPush.id).join(Contact, CampaignPush.contact_id == Contact.id).filter(
            Contact.company_id == company.id).first():
        return "in_outreach"
    if company is None:
        # Real, free bonus -- Icypeas' own revenue estimate came back on 2 of the 3 companies
        # in the 2026-09-28 test, at no extra cost.
        revenue = co.get("estimatedRevenuRange") or {}
        rev_lo = (revenue.get("estimatedMinRevenue") or {}).get("amount")
        rev_hi = (revenue.get("estimatedMaxRevenue") or {}).get("amount")
        rev_unit = 1_000_000 if (revenue.get("estimatedMinRevenue") or {}).get("unit") == "MILLION" else 1
        company = Company(batch_id=_batch(db, tenant_id).id, name=name, domain=None, linkedin_url=url,
                          industry=co.get("industry"), employee_count=co.get("numberOfEmployees"),
                          location=co.get("address"), source="icypeas:find_companies",
                          estimated_revenue_lower_usd=int(rev_lo * rev_unit) if rev_lo is not None else None,
                          estimated_revenue_higher_usd=int(rev_hi * rev_unit) if rev_hi is not None else None)
        db.add(company)
        db.commit()

        # Phase 5, 2026-10-07: every company we create goes into the SHARED pool too, for free --
        # it is data we already paid for (or, when this row came from the pool itself, data we
        # already had). A later run for a different partner whose ICP also matches this company
        # costs them nothing. Per-row cost is not attributed here (the page price is flat across
        # however many rows it returns), so cost_usd is left honest rather than invented.
        try:
            from app.gtm_os.sourcing import pool as sourcing_pool

            sourcing_pool.record(
                db, linkedin_url=url, domain=None, name=name, industry=co.get("industry"),
                headcount=co.get("numberOfEmployees"),
                revenue_low_usd=company.estimated_revenue_lower_usd,
                revenue_high_usd=company.estimated_revenue_higher_usd,
                location=co.get("address"), country=None,
                source_provider="icypeas", source_endpoint="find-companies", cost_usd=None,
            )
        except Exception as e:  # noqa: BLE001 -- pool-building must never break a paid run
            logger.warning("pool recording skipped: %s: %s", type(e).__name__, e)

    # Free -- Jobo's own leadership list, an agent picks the buyer, only Jobo's genuine (not
    # Crunchbase) LinkedIn URL is usable for outreach.
    leadership = _jobo_leadership_candidates(db, tenant_id, company)

    # ENRICH-TO-DECIDE, phase 7 (2026-10-07). "No dedicated marketing hire" (and any future
    # department_headcount requirement) has no provider that can search it -- Icypeas: verified
    # absent; Apollo: documented in its UI but its API parameter is unverified. It sat in free-text
    # notes, enforced by nothing, since this requirement was first set. The leadership list above
    # is already fetched for free for decision-maker resolution; deciding a department-presence
    # atom from the SAME data costs nothing extra. Only a CONFIRMED violation rejects -- an empty
    # or inconclusive leadership list is never read as "no marketing hire", the same asymmetry as
    # sample verification, because Jobo's index missing a title is not proof the role is absent.
    if department_atoms:
        from app.gtm_os.sourcing.compose import enrich_to_decide

        for atom in department_atoms:
            decision = enrich_to_decide(atom, leadership)
            if decision.satisfied is False:
                db.add(GtmLead(tenant_id=tenant_id, play=PLAY, lead_key=key, state=STATE_REJECTED,
                               qualifier_reason=f"{atom.name} requirement violated: {decision.evidence}"))
                db.commit()
                return f"department_requirement_failed:{atom.qualifier}"

    usable = [p for p in leadership if _real_linkedin_url_from_jobo(p)]
    person = None
    if usable:
        picks = select_best_decision_makers(db, tenant_id, company, usable, 1)
        person = next((p for p in usable if picks and p.get("name") == picks[0]["name"]), None)
    if person is not None:
        first_name, last_name = _split_name(person.get("name") or "")
        person_linkedin = normalize_linkedin_url(_real_linkedin_url_from_jobo(person))
        contact = Contact(company_id=company.id, first_name=first_name, last_name=last_name, title=person.get("title"),
                          linkedin_url=f"https://www.{person_linkedin}", thread_role="icp_filter_decision_maker",
                          matched_title_reasoning=f"Jobo leadership match, free: {picks[0].get('reasoning') or ''}"[:500])
        db.add(contact)
        db.commit()
        _create_icypeas_lead(db, tenant_id, key, company, contact, first_name, last_name, person_linkedin, co)
        return "created"

    # Real gap found live 2026-09-28: the free-only path found a usable decision maker for 0 of
    # 25 real, exact-headcount-matched companies in the first live test -- Jobo's leadership
    # index rarely has a genuine (non-Crunchbase) LinkedIn URL. Deferred to a single batched
    # paid resolution after this page, rather than paying per company -- one call covers up to
    # 50 companies.
    pending.append((key, company, co))
    return None


def search_icypeas(db: Session, tenant_id: int, icp: dict, pages: int = 1) -> dict:
    """REAL FIX, 2026-09-28: company-first search on Icypeas' EXACT numeric headcount (confirmed
    live: 336 real US companies at exactly 30-100 employees for Majji's ICP, via the free
    icypeas_count_companies check), replacing HarvestAPI's LinkedIn-bucket people search above
    as the default. The old path paid $0.003-0.07 to discover a company was never in the ICP
    band at all -- structurally unavoidable with a bucketed search (see search()'s own history).
    An exact numeric filter has no such waste: every returned company is already a real match.

    The decision maker is tried free first -- the SAME Jobo leadership lookup the hiring play
    already uses -- then, ONLY for a company that misses, batch-resolved via one paid HarvestAPI
    LinkedIn search covering up to 50 companies at once (_resolve_decision_makers_batch). The
    free-only design was tried first and measured live 2026-09-28: 0 of 25 real, exact-headcount
    companies had a usable (non-Crunchbase) LinkedIn URL in Jobo's index -- a 0% yield despite
    paying for a perfect company match, which is what made the paid fallback necessary rather
    than optional."""
    import hashlib

    from app.deepline_client import DeeplineError, DeeplineSpendBlocked, execute_tool

    filters = icypeas_filters_for_icp(icp, db=db, tenant_id=tenant_id)
    fingerprint = hashlib.sha1(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:12]
    cursor = _cursor(db, tenant_id)
    saved = dict(cursor.value or {})
    state = saved if saved.get("filters") == fingerprint else {}
    token = state.get("token")

    # Real gap found live 2026-09-28 (Majji: "what if these runs out of 336 after a few days"):
    # once pagination genuinely exhausts (Icypeas returns no next token), the OLD code just
    # restarted from page 1 on the next run -- re-paying to re-fetch the exact same
    # already-known companies, forever, producing zero new leads. Now: once exhausted, no
    # search call is made at all (free) until EXHAUSTION_COOLDOWN_DAYS has passed, giving
    # Icypeas' real database time to grow into a genuinely different result set (new
    # companies founded, existing ones crossing into the headcount band) before paying to
    # check again.
    exhausted_at = state.get("exhausted_at")
    if exhausted_at:
        cooldown_until = datetime.fromisoformat(exhausted_at) + timedelta(days=EXHAUSTION_COOLDOWN_DAYS)
        if datetime.utcnow() < cooldown_until:
            return {"companies": 0, "created": 0, "outcomes": {}, "exhausted_until": cooldown_until.isoformat(),
                   "stopped": f"pool exhausted for these filters as of {exhausted_at}; next check {cooldown_until.date()}"}

    # What the partner actually asked for, and -- separately -- which of those requirements this
    # search genuinely enforced. Verification below may only judge the rows against the second
    # list: an industry searched by free-text keyword was never promised as a classification, so
    # scoring the returned industry label against it would manufacture false failures.
    from app.gtm_os.sourcing.atoms import DEPARTMENT_HEADCOUNT as A_DEPARTMENT_HEADCOUNT
    from app.gtm_os.sourcing.atoms import HEADCOUNT as A_HEADCOUNT
    from app.gtm_os.sourcing.atoms import INDUSTRY as A_INDUSTRY
    from app.gtm_os.sourcing.atoms import REVENUE as A_REVENUE
    from app.gtm_os.sourcing.atoms import decompose_icp
    from app.gtm_os.sourcing.outcomes import QUALITY_FAIL as OUTCOME_QUALITY_FAIL

    icp_atoms = decompose_icp(icp)
    enforced_industry_names = (
        {a.name for a in icp_atoms.by_key(A_INDUSTRY)} if (filters.get("industry") or {}).get("include") else set()
    )

    # QUOTA, phase 3 (2026-10-07). Icypeas bills per REQUESTED result, so a fixed page of 25 cost
    # $0.175 whether the partner needed 25 more accounts or 3 -- and it charged again the next day
    # even when their target was already met. Buy the shortfall and nothing more.
    from app.gtm_os.sourcing.quota import plan as plan_quota
    from app.gtm_os.sourcing.registry import ICYPEAS_FIND_COMPANIES as _ICYPEAS

    quota = plan_quota(db, tenant_id, PLAY, _ICYPEAS.page_size_max or 200)
    if quota.satisfied:
        # The cheapest possible outcome: the partner has what they need today, so this costs $0.
        return {"companies": 0, "created": 0, "outcomes": {}, "stopped": quota.reason,
                "quota": {"target": quota.target, "delivered_today": quota.delivered_today,
                          "remaining": 0, "page_size": 0}}

    known_leads = {k for (k,) in db.query(GtmLead.lead_key).filter(GtmLead.tenant_id == tenant_id, GtmLead.play == PLAY)}
    result = {"companies": 0, "created": 0, "outcomes": {}, "stopped": None}

    def count(outcome):
        result["outcomes"][outcome] = result["outcomes"].get(outcome, 0) + 1

    pending: list = []

    # POOL-FIRST, phase 5 (2026-10-07). Before paying any provider, check our OWN data: a company
    # bought for a different partner is free for this one if it genuinely matches their ICP. Only
    # headcount/revenue/geography are checked (see pool.py for why industry is deliberately
    # excluded from pool matching), and only fresh rows are delivered -- a stale pool row is left
    # for a real run to re-verify rather than silently handed out as a current match.
    try:
        from app.gtm_os.sourcing import pool as sourcing_pool

        pool_matches = sourcing_pool.find_matches(db, tenant_id, PLAY, icp, limit=quota.remaining)
        for match in pool_matches:
            if not match.fresh:
                continue
            outcome = _process_icypeas_company(db, tenant_id, sourcing_pool.to_search_row(match.row),
                                               known_leads, pending,
                                               department_atoms=icp_atoms.by_key(A_DEPARTMENT_HEADCOUNT))
            sourcing_pool.mark_delivered(db, tenant_id, match.row.id, PLAY)
            if outcome is not None:
                count(f"pool:{outcome}")
                if outcome == "created":
                    result["created"] += 1
    except OperationalError:
        db.rollback()
    except Exception as e:  # noqa: BLE001 -- the pool is an optimization, never a dependency
        logger.warning("pool-first lookup skipped: %s: %s", type(e).__name__, e)

    # Shrink what we still need to BUY by whatever the pool just delivered for free. The pool
    # deliveries just created real GtmLead rows for this tenant, so re-running the SAME plan
    # naturally sees them in delivered_today and recomputes a correct remaining/page_size from
    # scratch -- cheaper and less error-prone than patching the arithmetic by hand.
    delivered_from_pool = result["created"]
    if delivered_from_pool:
        quota = plan_quota(db, tenant_id, PLAY, _ICYPEAS.page_size_max or 200)
    result["quota"] = {"target": quota.target, "delivered_today": quota.delivered_today,
                       "remaining": quota.remaining, "page_size": quota.page_size,
                       "delivered_from_pool": delivered_from_pool}
    if quota.satisfied:
        result["stopped"] = result["stopped"] or f"daily target met using {delivered_from_pool} from the shared pool"
        return result

    # REAL FIX, 2026-10-05: icypeas_count_companies is priced FREE ($0, deepline_client.py) for
    # exactly this reason -- verify a filter set actually matches something before ever paying
    # for a page -- but nothing in this function ever called it, so a filter value that doesn't
    # match Icypeas' real taxonomy (e.g. a partner's own wording like "Professional Services"
    # that isn't literally how Icypeas categorizes companies) was only discovered AFTER paying
    # $0.175 for an empty page. This is intentionally generic, not a one-off fix for that one
    # value: ANY partner's ICP wording, from any source/platform, gets validated for free before
    # the first real spend, every time the filter fingerprint changes. If the count can't be
    # parsed with confidence, this does NOT block the run -- a false "looks empty" must never
    # silently stop a real, paying search; it only blocks on a CONFIRMED zero.
    if token is None:
        # Only on a genuinely fresh start for this filter fingerprint, never mid-pagination --
        # a resumed page already proved the filter matches something real. If this count check
        # itself finds zero, it writes exhausted_at below, so the existing cooldown check above
        # (at the top of this function) naturally prevents re-checking the same empty filter set
        # again within EXHAUSTION_COOLDOWN_DAYS -- no separate "already checked" flag needed.
        try:
            count_response = execute_tool("icypeas_count_companies", {"query": filters})
        except (DeeplineSpendBlocked, DeeplineError):
            count_response = None
        if count_response is not None:
            raw = (count_response.get("toolResponse") or {}).get("raw") or {}
            real_count = None
            for key in ("count", "total", "totalCount", "resultsCount", "nbResults"):
                if isinstance(raw.get(key), int):
                    real_count = raw[key]
                    break
            if real_count == 0:
                cursor.value = {"filters": fingerprint, "exhausted_at": datetime.utcnow().isoformat()}
                db.commit()
                result["free_count_checked"] = 0
                result["exhausted"] = True
                return result

    for _ in range(pages):
        payload = {"query": filters,
                   "pagination": {"size": quota.page_size, **({"token": token} if token else {})}}
        try:
            response = execute_tool("icypeas_find_companies", payload)
        except DeeplineSpendBlocked as e:
            result["stopped"] = f"budget: {e}"
            break
        except DeeplineError as e:
            result["stopped"] = f"search failed: {e}"
            break
        raw = (response.get("toolResponse") or {}).get("raw") or {}
        leads = raw.get("leads") or []
        token = (raw.get("pagination") or {}).get("token")
        result["companies"] += len(leads)

        # Learn Icypeas' real industry taxonomy from rows we have already paid for. Its published
        # value list 404s, so this is the only source of truth we have -- and it is free. Each run
        # makes the next resolution better: the 21 wrong companies on 2026-10-03 are exactly how
        # we learned that "Law Practice" and "Facilities Services" are real values while
        # "Professional Services" is not.
        try:
            from app.gtm_os.sourcing.atoms import INDUSTRY
            from app.gtm_os.sourcing.resolution import record_observed_values

            record_observed_values(db, "icypeas", INDUSTRY, [c.get("industry") for c in leads])
        except OperationalError:
            db.rollback()
        except Exception as e:  # noqa: BLE001 -- learning must never break a paid run
            logger.warning("taxonomy learning skipped: %s: %s", type(e).__name__, e)

        # SAMPLE VERIFICATION, phase 4 (2026-10-07). A search can return 200 OK, with rows, and be
        # completely wrong -- on 2026-10-03 this ICP produced 21 hospitals, law firms and a fire
        # department, and nothing noticed because the only thing checked was that the call
        # succeeded. Judge the rows, not the filter we believe we sent. Missing values never count
        # as violations (see verification.py for why that asymmetry is load-bearing).
        try:
            from app.gtm_os.sourcing.verification import verify_sample

            checkable = [a for a in icp_atoms.must_haves()
                         if a.key in (A_HEADCOUNT, A_REVENUE) or a.name in enforced_industry_names]
            verification = verify_sample(leads, icp_atoms, checkable_atoms=checkable)
            result["verification"] = verification.summary()
            if not verification.passed():
                result["stopped"] = (
                    f"quality: only {verification.match_rate:.0%} of a {verification.checked}-row "
                    f"sample matched the ICP ({verification.violations}). Stopping before buying "
                    f"more of the same.")
                result["outcome"] = OUTCOME_QUALITY_FAIL
                break
        except Exception as e:  # noqa: BLE001 -- verification must never break a paid run
            logger.warning("sample verification skipped: %s: %s", type(e).__name__, e)

        # Real bug found live 2026-09-28: a mid-run Neon connection drop (this codebase's own
        # documented recurring failure mode, see app/db/session.py) crashed the per-company loop
        # below AFTER this page was already paid for, but the cursor only used to save at the
        # very end of the whole function -- so the next run would re-pay $0.175+ to re-fetch the
        # exact same page for nothing. Saving the advanced token HERE, immediately once the paid
        # page is in hand, means a later crash in this page's processing never re-buys it.
        try:
            cursor.value = {"filters": fingerprint, "token": token}
            db.commit()
        except OperationalError:
            db.rollback()

        for co in leads:
            try:
                outcome = _process_icypeas_company(db, tenant_id, co, known_leads, pending,
                                                   department_atoms=icp_atoms.by_key(A_DEPARTMENT_HEADCOUNT))
            except OperationalError:
                # One company's DB work hit a dropped connection -- must not lose the rest of
                # an already-paid-for page. Retried on a future run (never added to known_leads
                # for real here, since that only happens inside the helper after a commit).
                db.rollback()
                outcome = "db_error_retry_later"
            if outcome is not None:  # None = deferred to the batched resolver below
                count(outcome)
                if outcome == "created":
                    result["created"] += 1

        if pending:
            # Only resolve decision makers for as many companies as the partner still needs.
            # The search page is already bought and every company on it is persisted either way,
            # but decision-maker resolution is a SEPARATE paid call ($0.07/page of names), so
            # resolving 25 when 3 are needed is the expensive half of over-fetching. The surplus
            # stays in `pending` and its companies remain in the database for a later run.
            still_needed = max(0, quota.remaining - result["created"])
            deferred = pending[still_needed:]
            pending = pending[:still_needed]
            if deferred:
                result["outcomes"]["deferred_over_quota"] = \
                    result["outcomes"].get("deferred_over_quota", 0) + len(deferred)

        if pending:
            try:
                titles = icp.get("decision_maker_titles") or ["Owner", "Founder", "CEO"]
                resolved = _resolve_decision_makers_batch(db, tenant_id, [c for _, c, _ in pending], titles)
            except DeeplineSpendBlocked as e:
                result["stopped"] = f"budget: {e}"
                resolved = {}
            for key, company, co in pending:
                try:
                    contact = resolved.get(company.id)
                    if contact is None:
                        count("no_decision_maker")
                        continue
                    _create_icypeas_lead(db, tenant_id, key, company, contact, contact.first_name, contact.last_name,
                                        normalize_linkedin_url(contact.linkedin_url), co)
                    result["created"] += 1
                    count("created")
                except OperationalError:
                    db.rollback()
                    count("db_error_retry_later")
            pending = []

        if result["stopped"]:
            break
        if not leads or not token:
            # Genuinely exhausted -- Icypeas itself says there is no next page. Cooling down
            # (see EXHAUSTION_COOLDOWN_DAYS above) rather than restarting from page 1, which
            # would just re-pay to re-see the same companies with zero new leads.
            result["exhausted"] = True
            break

    new_state = {"filters": fingerprint, "token": token}
    if result.get("exhausted"):
        new_state["exhausted_at"] = datetime.utcnow().isoformat()
    cursor.value = new_state
    db.commit()
    return result


def search(db: Session, tenant_id: int, icp: dict, pages: int = 1) -> dict:
    """Fallback -- HarvestAPI's LinkedIn-bucket people search. Kept for when Icypeas is
    unavailable; search_icypeas() above is the default (2026-09-28), for the real reasons in
    its own docstring. People search -> verified Company + Contact + lead, on the partner's
    tenant."""
    import hashlib

    from app import harvestapi
    from app.deepline_client import DeeplineError, DeeplineSpendBlocked
    from app.phases.company_profile_check import fetch_public_company_profile

    filters = search_filters(icp)
    fingerprint = hashlib.sha1(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:12]
    cursor = _cursor(db, tenant_id)
    state = dict(cursor.value or {})
    page = state.get("page", 1) if state.get("filters") == fingerprint else 1

    known_leads = {k for (k,) in db.query(GtmLead.lead_key).filter(GtmLead.tenant_id == tenant_id, GtmLead.play == PLAY)}
    lo, hi = icp.get("employee_min") or 0, icp.get("employee_max") or 10**9
    result = {"people": 0, "created": 0, "outcomes": {}, "stopped": None, "page": page}

    def count(outcome):
        result["outcomes"][outcome] = result["outcomes"].get(outcome, 0) + 1

    def _reject(key: str, person: dict, reason: str) -> None:
        known_leads.add(key)
        db.add(GtmLead(tenant_id=tenant_id, play=PLAY, lead_key=key, state=STATE_REJECTED,
                       person_name=f"{person.get('first_name') or ''} {person.get('last_name') or ''}".strip(),
                       qualifier_reason=reason))
        db.commit()

    checked: dict[str, dict | None] = {}
    for _ in range(pages):
        try:
            people = harvestapi.search_leads(page=page, **filters)
        except DeeplineSpendBlocked as e:
            result["stopped"] = f"budget: {e}"
            break
        except DeeplineError as e:
            result["stopped"] = f"search failed: {e}"
            break
        result["people"] += len(people)
        for person in people:
            universal = harvestapi.universal_name(person.get("company_linkedin_url"))
            linkedin = normalize_linkedin_url(person.get("linkedin_url"))
            if not universal or not linkedin:
                count("incomplete")
                continue
            key = f"company:{universal}"
            if key in known_leads:
                count("known")
                continue

            # FREE #1 -- an obvious vendor/agency/recruiter is rejected by its own name, no
            # lookup spent at all.
            company_name_guess = person.get("company_name") or ""
            if _looks_like_a_vendor(company_name_guess):
                _reject(key, person, f"company name matches a vendor/agency/recruiter pattern: {company_name_guess!r} -- free, no lookup spent")
                count("vendor_name_match")
                continue

            if universal not in checked:
                # FREE #2 -- the public LinkedIn company page's own declared size band. Only a
                # CONFIRMED mismatch is rejected here; an unreadable page or no declared band
                # falls through to the paid lookup below rather than guessing.
                try:
                    free_profile = fetch_public_company_profile(person.get("company_linkedin_url"))
                except Exception:  # noqa: BLE001 -- a free check failing must never block the paid fallback
                    free_profile = None
                if free_profile and _size_fits(free_profile.get("size_band"), lo, hi) is False:
                    _reject(key, person, f"company size band {free_profile['size_band']} outside {lo}-{hi} -- free public LinkedIn page, no paid lookup spent")
                    count("size_outside_icp_free")
                    continue

                # PAID -- reserved only for a company that survived both free checks above.
                try:
                    checked[universal] = harvestapi.get_company(universal)
                except DeeplineSpendBlocked as e:
                    result["stopped"] = f"budget: {e}"
                    break
                except DeeplineError:
                    checked[universal] = None
            facts = checked[universal]
            size = (facts or {}).get("employee_count")
            if not facts or size is None or not (lo <= size <= hi):
                _reject(key, person, f"company size {size} outside {lo}-{hi}" if facts else "company not found")
                count("size_outside_icp")
                continue

            company = (db.query(Company).join(Batch, Company.batch_id == Batch.id)
                       .filter(Batch.tenant_id == tenant_id, Company.linkedin_url.ilike(f"%/company/{universal}%")).first())
            if company is not None and db.query(CampaignPush.id).join(Contact, CampaignPush.contact_id == Contact.id).filter(
                    Contact.company_id == company.id).first():
                count("in_outreach")
                continue
            if company is None:
                website = facts.get("website")
                domain = (website or "").lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0] or None
                company = Company(batch_id=_batch(db, tenant_id).id, name=facts.get("name") or person.get("company_name"),
                                  domain=domain, linkedin_url=facts.get("linkedin_url") or person.get("company_linkedin_url"),
                                  industry=facts.get("industry"), employee_count=size, location=facts.get("hq_text") or None,
                                  source="harvestapi:icp_filter_search")
                db.add(company)
                db.commit()
            contact = Contact(company_id=company.id, first_name=person.get("first_name"), last_name=person.get("last_name"),
                              title=person.get("title"), linkedin_url=person.get("linkedin_url"),
                              thread_role="icp_filter_decision_maker", matched_title_reasoning="ICP filter people search")
            db.add(contact)
            db.commit()
            evidence = (
                f"Person: {contact.first_name} {contact.last_name or ''} -- {contact.title} ({person.get('location') or ''})\n"
                f"Profile summary: {person.get('headline') or ''}\n\n"
                f"Company: {company.name} | {facts.get('industry')} | {size} employees | HQ {facts.get('hq_text')} | "
                f"founded {facts.get('founded')} | {facts.get('website')}\nAbout: {facts.get('description')}"
            )
            db.add(GtmLead(tenant_id=tenant_id, play=PLAY, lead_key=key, company_id=company.id, contact_id=contact.id,
                           person_name=f"{contact.first_name or ''} {contact.last_name or ''}".strip(), person_linkedin_url=linkedin,
                           state=STATE_SIGNAL, evidence=evidence))
            db.commit()
            known_leads.add(key)
            result["created"] += 1
            count("created")
        if result["stopped"] or not people:
            break
        page += 1
    cursor.value = {"filters": fingerprint, "page": page if page <= 100 else 1}
    db.commit()
    return result


# LOOSENED, 2026-09-28, explicit instruction: this company already matches every hard, VERIFIED
# filter (exact headcount, decision-maker title, industry not already excluded) before the
# Qualifier ever sees it -- unlike the hiring/post_engagement plays, which read a genuine but
# ambiguous signal. Its old prompt still asked for a 0-100 "fit score" and gated on >=70, which
# let it reject on soft, unverifiable guesses ("likely already has a bigger sales team than
# 2-3 people" -- Unlimited Funds, 60 employees, rejected on pure speculation with no real
# evidence for or against). Now: reject ONLY for a hard, evidence-based disqualifier the search
# could not already catch (B2C, vendor/agency, non-profit/school, wrong person) -- anything
# that clears those is qualified. No score, no soft judgment call.
QUALIFIER_PROMPT = """You qualify a B2B target company for a partner who sells the services described below.
This company already matches every hard, VERIFIED filter (revenue/headcount, decision-maker title, industry).
Your only job is to catch a real, clear mismatch the filters could not -- never guess or speculate.

WHAT THE PARTNER SELLS AND TO WHOM:
{icp_notes}

ALREADY VERIFIED, DO NOT RE-JUDGE: {criteria}

THE PERSON AND COMPANY:
\"\"\"{evidence}\"\"\"

Reject ONLY if the evidence CLEARLY shows one of these -- never on a guess, an assumption, or what
seems "likely":
- the company sells sales, marketing, consulting, coaching, agency or recruiting services itself (a competitor/vendor)
- the company is B2C (sells directly to individual consumers), not B2B
- it is a non-profit, association, government body, school, or a one-person practice
- the person is clearly not the owner/founder/CEO (or equivalent top decision maker) of THIS company

If none of these clearly applies, QUALIFY it -- it already matches every real filter. Do not reject
for a guess about their sales team size, company maturity, or whether they "probably" already have
enough help; you have no real evidence for that, only for what's stated above.

Return ONLY this JSON:
{{
  "qualified": true or false,
  "reason": "one sentence: which disqualifier clearly applied, or why it clearly qualifies",
  "problem_statement": "the real problem this company likely has that the partner's offer addresses, one plain sentence, or null",
  "demand_statement": "what they'd likely want help with, one plain sentence, or null",
  "positioning_angle": "how the partner could open a conversation, one sentence, or null"
}}"""


def qualify(db: Session, tenant_id: int, icp: dict, limit: int = 25) -> dict:
    from app.gtm_os.plays.post_engagement import _write_opportunity
    from app.llm_budget import LlmBudgetExceeded
    from app.llm_client import generate_json

    criteria = {k: icp.get(k) for k in ("employee_min", "employee_max", "revenue_min_usd", "revenue_max_usd",
                                         "sales_team_size_min", "sales_team_size_max", "industries", "geographies")
                if icp.get(k) not in (None, [], "")}
    leads = (db.query(GtmLead).filter(GtmLead.tenant_id == tenant_id, GtmLead.play == PLAY, GtmLead.state == STATE_SIGNAL)
             .order_by(GtmLead.created_at).limit(limit).all())
    qualified = rejected = 0
    for lead in leads:
        prompt = QUALIFIER_PROMPT.format(icp_notes=icp.get("notes") or "", criteria=json.dumps(criteria), evidence=(lead.evidence or "")[:4000])
        try:
            verdict = generate_json(prompt, db, BILLING_TENANT_ID, max_tokens=500)
        except (SpendBlocked, LlmBudgetExceeded) as e:
            return {"qualified": qualified, "rejected": rejected, "stopped": f"budget: {e}"}
        except Exception as e:  # noqa: BLE001
            lead.last_error = f"qualifier: {type(e).__name__}: {e}"[:500]
            db.commit()
            continue
        # No score gate (2026-09-28, explicit instruction): every lead here already matches every
        # hard, verified filter before the Qualifier ever sees it, so a soft 0-100 "fit score" on
        # top of that only reintroduces the kind of unverifiable guessing the prompt above now
        # explicitly forbids. qualified=true/false is the one real decision.
        passes = bool(verdict.get("qualified"))
        lead.qualifier_reason, lead.qualifier_output = verdict.get("reason"), verdict
        if passes:
            # Real gap fixed 2026-09-28: this used to just flip the state, leaving a qualified
            # lead invisible to the Pipeline/Accounts dashboard -- no Opportunity/Strategy ever
            # existed for it. Reuses the SAME shared writer the hiring and post_engagement
            # plays already use, so a qualified partner lead shows up exactly where every other
            # qualified lead does.
            company = db.get(Company, lead.company_id)
            opportunity, _strategy = _write_opportunity(db, tenant_id, lead, company, play=PLAY,
                                                        objective="Open a conversation about the problem their team likely has",
                                                        source_note="from a firmographic filter match, no signal")
            lead.opportunity_id = opportunity.id
        # The search already found the decision maker, so a qualified lead is ready for outreach.
        lead.state = STATE_CONTACT_FOUND if passes else STATE_REJECTED
        db.commit()
        qualified += passes
        rejected += not passes
    return {"qualified": qualified, "rejected": rejected, "stopped": None}


def run_icp_filters(db: Session, tenant_id: int, pages: int = 1, run_cap_usd: float | None = 0.25) -> dict:
    """Real bug fixed 2026-09-28, before this play's first production run: this used to spend
    with no control-plane check at all, unlike every other play -- Elephant Edge's own pause
    would stop the hiring and post_engagement plays but silently NOT this one, even though its
    spend is reserved against the SAME Elephant Edge ledger (BILLING_TENANT_ID). Checked against
    the billing tenant, not `tenant_id` (the partner) -- a partner tenant has no control-plane
    config of its own, and pausing is Elephant Edge's own kill switch over its own spend."""
    from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run
    from app.phases.partner_icp import get_partner_icp
    from app.spend_ledger import spend_scope

    try:
        check_can_run(db, BILLING_TENANT_ID)
    except ControlPlaneHalted as e:
        return {"status": "skipped", "reason": str(e)}

    icp = get_partner_icp(db, tenant_id)
    if not icp:
        return {"status": "skipped", "reason": "no partner ICP configured"}
    with spend_scope(db, BILLING_TENANT_ID, f"{PLAY}:tenant_{tenant_id}", run_cap_usd=run_cap_usd) as scope:
        coverage = icp_coverage(icp)
        result = {"status": "completed", "play": PLAY, "tenant_id": tenant_id, "filters": icypeas_filters_for_icp(icp, db=db, tenant_id=tenant_id)}
        # Every ICP requirement, and what actually happened to it. Reported on every run so a
        # requirement can never again be stored, look configured, and be enforced by nothing --
        # "no dedicated marketing hire" sat in Majji's ICP for days in exactly that state.
        result["icp_coverage"] = {
            "enforced_by_provider": [a.name for a in coverage.enforced],
            "checked_after_fetch": [a.name for a in coverage.residual],
            "needs_provider_research": [a.name for a in coverage.unverified],
            "not_supported_here": [a.name for a in coverage.unsupported],
            "unenforced_must_haves": [a.name for a in coverage.must_have_gap],
        }
        # REAL GAP CLOSED, 2026-10-07: this used to call search_icypeas() directly, bypassing the
        # planner entirely -- phase 6's ranking/failover/circuit-breaking was built and tested but
        # never actually ran in production, and route_attempts stayed empty. Only one adapter
        # (icypeas) is executable today, so this is a same-behavior change right now; it is what
        # makes failover real the moment a second provider is registered, and what gives phase 8's
        # scorecards something real to aggregate.
        from app.gtm_os.sourcing.planner import execute as route_execute

        routed = route_execute(db, tenant_id, icp, pages=pages)
        result["search"] = routed.result or {"companies": 0, "created": 0, "outcomes": {},
                                             "stopped": routed.stopped}
        result["routing"] = {"provider": routed.provider, "attempts": routed.attempts,
                             "considered": routed.considered, "stopped": routed.stopped}
        result["qualify"] = qualify(db, tenant_id, icp)
        result["spent_usd"] = round(scope.spent_usd, 4)
    return result


# Register this play's Icypeas search as a routable adapter (2026-10-07). Adding another provider
# is now exactly this: write its search function and register it. The ranking, failover and
# recording in planner.py never change, and no call site learns a new provider's name.
def _register_sourcing_adapters() -> None:
    from app.gtm_os.sourcing.planner import register_adapter

    register_adapter("icypeas", lambda db, tenant_id, icp, **kw: search_icypeas(db, tenant_id, icp, **kw))


_register_sourcing_adapters()
