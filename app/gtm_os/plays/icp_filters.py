"""Play F -- ICP filters only (no signal): for partners whose buyer is defined by firmographics.

    search     one HarvestAPI LinkedIn people search built from the partner's own ICP (company
               headcount band, decision-maker titles, geography) -- $0.07 a page of 25 people,
               each with a LinkedIn URL. A cursor continues from the next page on the next run.
    verify     FREE first: an obvious vendor/agency/recruiter is rejected by name alone (no
               lookup at all), then the free public LinkedIn company page decides size fit for
               everyone else. The PAID company lookup ($0.003) is only spent on a company that
               survives both free checks -- see the 2026-09-28 fix note below.
    qualify    ONE LLM call per company against the partner's ICP notes -> qualified / rejected
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
from datetime import datetime

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
DEFAULT_MIN_FIT_SCORE = 70
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


def search(db: Session, tenant_id: int, icp: dict, pages: int = 1) -> dict:
    """People search -> verified Company + Contact + lead, on the partner's tenant."""
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


QUALIFIER_PROMPT = """You qualify B2B target companies for a partner who sells the services described below.
The company and person were found by a filter search, so there is no buying signal -- judge fit only.
Be strict: reject anything that clearly does not match.

WHAT THE PARTNER SELLS AND TO WHOM:
{icp_notes}

HARD CRITERIA: {criteria}

THE PERSON AND COMPANY:
\"\"\"{evidence}\"\"\"

Reject if ANY of these is true:
- the company sells sales, marketing, consulting, coaching, agency or recruiting services itself
- it is a non-profit, association, government body, school, or a one-person practice
- the person is not the owner / founder / CEO (or equivalent top decision maker) of THIS company
- the company clearly does not fit what the partner sells

Return ONLY this JSON:
{{
  "qualified": true or false,
  "icp_fit_score": 0-100,
  "reason": "one or two sentences explaining the decision",
  "sales_team_guess": "what the data suggests about their sales team, or 'unknown'",
  "positioning_angle": "how the partner could open a conversation, one sentence, or null"
}}"""


def qualify(db: Session, tenant_id: int, icp: dict, limit: int = 25, min_fit_score: int = DEFAULT_MIN_FIT_SCORE) -> dict:
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
        score = verdict.get("icp_fit_score")
        score = int(score) if isinstance(score, (int, float)) else 0
        passes = bool(verdict.get("qualified")) and score >= min_fit_score
        lead.icp_fit_score, lead.qualifier_reason, lead.qualifier_output = score, verdict.get("reason"), verdict
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
        result = {"status": "completed", "play": PLAY, "tenant_id": tenant_id, "filters": search_filters(icp)}
        result["search"] = search(db, tenant_id, icp, pages=pages)
        result["qualify"] = qualify(db, tenant_id, icp)
        result["spent_usd"] = round(scope.spent_usd, 4)
    return result
