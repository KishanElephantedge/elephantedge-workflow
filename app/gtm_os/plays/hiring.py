"""Play B -- hiring signals (a company hiring for the problem we solve).

    sense      preferred: one direct Prospeo search per ENABLED ICP (hiring-for + revenue + size +
               decision maker, with LinkedIn URL) -- see sense_prospeo. Fallback when no Prospeo
               key is set: the existing Apify job discovery, once per enabled ICP's profile, with a
               small posting limit. Every posting is kept as a GtmSignal linked to its company.
               The paid team-size lookup is skipped -- the Qualifier judges fit instead.
    ingest     every recently-hired-for company becomes one GtmLead (lead_key "company:<id>");
               a company already known to this play, or already in outreach, is never ingested
    qualify    ONE LLM call per company: reading its real job postings, does this hire show a
               problem one of our offerings solves, for a company that fits an ICP? Nothing is
               paid for before this says yes.
    contact    the existing decision-maker finder's free layer (leadership list + Google, the agent
               picks the person) and the per-result email waterfall -- no paid people search --
               for qualified companies only, one contact each
    draft      the existing message drafter -> awaits approval

Same guarantees as Play A: every paid call is reserved against the tenant's combined daily cap
and this run's cap before it is made, a budget refusal leaves the lead waiting in its state, and
each step takes the next N leads in its state -- no outer loop, no backlog re-scan.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.models import Batch, CampaignPush, Company, Contact
from app.gtm_os.plays.lead import (
    STATE_CONTACT_FOUND, STATE_CONTACT_MISSING, STATE_DRAFTED, STATE_FAILED, STATE_QUALIFIED,
    STATE_REJECTED, STATE_SIGNAL, GtmLead,
)
from app.gtm_os.plays.post_engagement import _qualifier_context, _write_opportunity, draft_messages
from app.spend_ledger import SpendBlocked, current_spend_scope

logger = logging.getLogger(__name__)

PLAY = "hiring"
PLAY_BATCH_SOURCE = "play_b"
SIGNAL_LOOKBACK_DAYS = 14
DEFAULT_MIN_FIT_SCORE = 70
POSTINGS_PER_LEAD = 3               # how many of a company's postings the Qualifier reads
OBJECTIVE = "Open a conversation about the hire they are making and the problem behind it"


def lead_key_for(company_id: int) -> str:
    return f"company:{company_id}"


# ---------------------------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------------------------

def _money(v) -> str | None:
    return f"${v / 1_000_000:.0f}M" if isinstance(v, (int, float)) and v else None


def _evidence(company: Company, signals: list) -> str:
    rev_lo, rev_hi = _money(company.estimated_revenue_lower_usd), _money(company.estimated_revenue_higher_usd)
    lines = [
        f"Company: {company.name} ({company.domain or 'no domain'})",
        f"LinkedIn: {company.linkedin_url or 'unknown'}",
        f"Industry: {company.industry or 'unknown'} | Employees: {company.employee_count or 'unknown'} | HQ: {company.location or 'unknown'}",
        f"Estimated revenue: {f'{rev_lo}-{rev_hi}' if rev_lo or rev_hi else 'unknown'}",
        f"Last funding: {company.last_funding_round_type or 'unknown'}",
        f"Open postings seen: {len(signals)}",
    ]
    about = next(((s.extracted_info or {}).get("organization_description") for s in signals
                  if (s.extracted_info or {}).get("organization_description")), None)
    if about:
        lines.append(f"About: {about[:500]}")
    for s in signals[:POSTINGS_PER_LEAD]:
        info = s.extracted_info or {}
        posted = s.observed_at.date().isoformat() if s.observed_at else "unknown date"
        lines.append(f"\n--- Job posting: {info.get('title')} ({info.get('seniority') or 'seniority unknown'}, posted {posted})")
        lines.append((info.get("description_text") or info.get("ai_requirements_summary") or "")[:1500])
    return "\n".join(lines)


def _already_in_outreach(db: Session, company_id: int) -> bool:
    """A company whose contacts were already pushed to a campaign is not a new lead."""
    return (
        db.query(CampaignPush.id).join(Contact, CampaignPush.contact_id == Contact.id)
        .filter(Contact.company_id == company_id).first() is not None
    )


def ingest_new_signals(db: Session, tenant_id: int, limit: int = 100, now: datetime | None = None) -> dict:
    from app.gtm_os.intelligence.signal import GtmSignal

    now = now or datetime.utcnow()
    known = {row[0] for row in db.query(GtmLead.lead_key).filter(GtmLead.tenant_id == tenant_id, GtmLead.play == PLAY)}
    signals = (
        db.query(GtmSignal)
        .filter(
            GtmSignal.tenant_id == tenant_id,
            GtmSignal.source == "linkedin_job",
            GtmSignal.company_id.isnot(None),
            GtmSignal.captured_at >= now - timedelta(days=SIGNAL_LOOKBACK_DAYS),
        )
        .order_by(GtmSignal.captured_at.desc())
        .all()
    )
    by_company: dict[int, list] = {}
    for s in signals:
        by_company.setdefault(s.company_id, []).append(s)

    created = skipped = 0
    for company_id, company_signals in by_company.items():
        if created >= limit:
            break
        key = lead_key_for(company_id)
        company = db.get(Company, company_id)
        if key in known or company is None or _already_in_outreach(db, company_id):
            skipped += 1
            continue
        db.add(GtmLead(tenant_id=tenant_id, play=PLAY, lead_key=key, company_id=company_id,
                       signal_id=company_signals[0].id, state=STATE_SIGNAL,
                       evidence=_evidence(company, company_signals)))
        known.add(key)
        created += 1
    db.commit()
    return {"created": created, "skipped": skipped}


# ---------------------------------------------------------------------------------------------
# qualify -- the Qualifier agent
# ---------------------------------------------------------------------------------------------

QUALIFIER_PROMPT = """You qualify B2B target companies for {company_name}, which sells the offerings below.
A company surfaced because it is hiring. A job posting tells you what a company is trying to fix
or build right now. Decide if this company is worth contacting. Be strict: most should be rejected.

OUR OFFERINGS:
{offerings}

OUR IDEAL CUSTOMERS:
{icps}

THE COMPANY AND ITS OPEN POSTINGS:
\"\"\"{evidence}\"\"\"

Reject if ANY of these is true:
- it is a staffing agency, recruiter, or consultancy hiring for a client, or it sells sales/GTM services itself
- the hire does not point to a problem one of our offerings solves
- the company clearly does not match any of our ideal customers (size, revenue, stage, or market);
  when revenue is unknown, judge it from employees, funding and what the company does. As a rough
  guide a B2B software company makes $100-250K revenue per employee, so a company under ~50
  employees rarely reaches $10M -- only qualify one if the data gives a concrete reason it does
- the posting is generic or unrelated to sales, revenue or go-to-market

Qualify only when the hire itself shows a real, current problem or initiative that one of our
offerings addresses, at a company that plausibly fits one of our ideal customers.

Return ONLY this JSON:
{{
  "qualified": true or false,
  "icp_fit_score": 0-100,
  "matched_icp_id": one of {icp_ids} or null,
  "matched_offering": one of {offering_names} or null,
  "hire_reading": "what this hire tells us about the company right now, one sentence",
  "reason": "one or two sentences explaining the decision",
  "evidence_quote": "the exact words from a posting that justify it (short)",
  "problem_statement": "their problem in one plain sentence, or null",
  "demand_statement": "what they appear to need, in one plain sentence, or null",
  "positioning_angle": "how to open a conversation with their leadership, one sentence, or null",
  "who_to_contact": "the role that owns this problem and would buy, e.g. CEO, Founder, VP Sales, CRO"
}}"""


def qualify_leads(db: Session, tenant_id: int, limit: int = 20, min_fit_score: int = DEFAULT_MIN_FIT_SCORE) -> dict:
    from app.llm_budget import LlmBudgetExceeded
    from app.llm_client import generate_json

    leads = (
        db.query(GtmLead)
        .filter(GtmLead.tenant_id == tenant_id, GtmLead.play == PLAY, GtmLead.state == STATE_SIGNAL)
        .order_by(GtmLead.created_at.desc())
        .limit(limit)
        .all()
    )
    if not leads:
        return {"qualified": 0, "rejected": 0, "stopped": None}
    ctx = _qualifier_context(db, tenant_id)
    qualified = rejected = 0
    for lead in leads:
        prompt = QUALIFIER_PROMPT.format(evidence=(lead.evidence or "")[:6000],
                                         **{k: v for k, v in ctx.items() if not k.startswith("valid_")})
        try:
            verdict = generate_json(prompt, db, tenant_id, max_tokens=700)
        except (SpendBlocked, LlmBudgetExceeded) as e:
            return {"qualified": qualified, "rejected": rejected, "stopped": f"budget: {e}"}
        except Exception as e:  # noqa: BLE001 -- one bad response must not stop the rest
            lead.last_error = f"qualifier: {type(e).__name__}: {e}"[:500]
            db.commit()
            continue

        score = verdict.get("icp_fit_score")
        score = int(score) if isinstance(score, (int, float)) else 0
        icp_id = verdict.get("matched_icp_id") if verdict.get("matched_icp_id") in ctx["valid_icps"] else None
        offering = verdict.get("matched_offering") if verdict.get("matched_offering") in ctx["valid_offerings"] else None
        passes = bool(verdict.get("qualified")) and score >= min_fit_score and icp_id is not None and offering is not None

        lead.icp_fit_score = score
        lead.intent = "hiring"
        lead.qualifier_reason = verdict.get("reason")
        lead.qualifier_output = verdict
        lead.state = STATE_QUALIFIED if passes else STATE_REJECTED
        db.commit()
        qualified += passes
        rejected += not passes
    return {"qualified": qualified, "rejected": rejected, "stopped": None}


# ---------------------------------------------------------------------------------------------
# contact -- the existing decision-maker finder, one contact per qualified company
# ---------------------------------------------------------------------------------------------

def _reachable(contact: Contact, channels: list[str]) -> bool:
    """Contactable on at least one channel the tenant's campaigns actually use."""
    return ("linkedin" in channels and bool(contact.linkedin_url)) or ("email" in channels and bool(contact.email))


DECISION_MAKER_TITLES = ["CEO", "Founder", "Co-Founder", "President", "Chief Revenue Officer", "Chief Operating Officer",
                         "VP Sales", "VP of Sales", "Head of Sales", "Head of Revenue"]
LEADS_PER_CALL = 50                 # HarvestAPI accepts up to 50 currentCompanies in one search


def _norm(name: str | None) -> str:
    import re

    n = re.sub(r"[^a-z0-9 ]", "", (name or "").lower())
    for suffix in (" inc", " llc", " ltd", " corp", " corporation", " co", " company", " technologies", " labs"):
        n = n.removesuffix(suffix)
    return n.strip()


def _harvest_decision_makers(db: Session, tenant_id: int, pairs: list) -> dict:
    """Phase 9, 2026-10-07: now a thin wrapper over the SHARED resolver
    (app/gtm_os/sourcing/decision_maker.py). This used to be a separate implementation that
    batched companies by LinkedIn URL -- a real, confirmed-live bug: batching MULTIPLE company
    URLs together in one currentCompanies request silently returns ZERO people, even for real,
    correctly-sized companies with active LinkedIn pages. icp_filters.py's own copy of this logic
    hit and fixed that exact bug on 2026-09-28/10-04 by switching to company NAME batching; this
    play's copy never got the fix, so it has almost certainly been returning 0 decision makers in
    production the same way icp_filters.py silently was before its fix. It also never adopted the
    OTHER fix made alongside that one: a budget/provider error on a later page used to crash the
    whole batch here, discarding whatever earlier pages had already been paid for and found.

    Returns {company_id: Contact or None}. Raises DeeplineSpendBlocked -- unlike the shared
    resolver, which never raises -- ONLY because this function's own caller (find_contacts, just
    below) already has an existing, tested contract of catching that exception itself and
    returning a clean {"stopped": "budget: ..."} response; changing that contract is out of scope
    for this fix. The shared resolver's own no-raise behavior already protects what matters most
    (earlier pages/chunks are never discarded by a later failure); this re-raises afterward purely
    to preserve find_contacts' existing external behavior unchanged."""
    from app.deepline_client import DeeplineSpendBlocked
    from app.gtm_os.sourcing.decision_maker import resolve_decision_makers_batch

    companies = [c for _, c in pairs]
    offering_name_for = {c.id: (lead.qualifier_output or {}).get("matched_offering") for lead, c in pairs}

    out = resolve_decision_makers_batch(
        db, tenant_id, companies, DECISION_MAKER_TITLES,
        default_thread_role="decision_maker", reasoning_label="HarvestAPI LinkedIn search",
        offering_name_for=offering_name_for,
    )
    # Preserve this function's existing contract: a budget refusal anywhere in the batch still
    # surfaces as DeeplineSpendBlocked to find_contacts, which already handles it. The shared
    # resolver itself never discards what it already found before that point -- only this
    # call site's external contract is being kept the same, not its internal safety.
    if len(out) < len(companies):
        raise DeeplineSpendBlocked("decision-maker batch stopped early (budget or provider error)")
    return out


def find_contacts(db: Session, tenant_id: int, limit: int = 10, channels: list[str] | None = None) -> dict:
    from app.gtm_os.orchestration.control import get_control_config, get_outreach_channels
    from app.gtm_os.sales.contact_discovery import get_eligible_contacts
    from app.phases.decision_maker import find_decision_makers

    channels = channels or get_outreach_channels(get_control_config(db, tenant_id))

    leads = (
        db.query(GtmLead)
        .filter(GtmLead.tenant_id == tenant_id, GtmLead.play == PLAY, GtmLead.state == STATE_QUALIFIED)
        .order_by(GtmLead.icp_fit_score.desc(), GtmLead.created_at.desc())
        .limit(limit)
        .all()
    )
    from app.deepline_client import DeeplineError, DeeplineSpendBlocked

    scope = current_spend_scope()
    found = missing = reused = 0

    def known_contact(company):
        # Someone we already know at this company costs nothing.
        known = sorted((c for c in get_eligible_contacts(db, company.id) if _reachable(c, channels)),
                       key=lambda c: not c.linkedin_url if channels[0] == "linkedin" else not c.email)
        return known[0] if known else None

    # LinkedIn-only: one batched LinkedIn people search covers every company with a LinkedIn page.
    harvested: dict = {}
    if channels == ["linkedin"]:
        pairs = [(lead, c) for lead in leads if (c := db.get(Company, lead.company_id)) is not None
                 and c.linkedin_url and known_contact(c) is None]
        if pairs:
            try:
                harvested = _harvest_decision_makers(db, tenant_id, pairs)
            except DeeplineSpendBlocked as e:
                return {"found": 0, "reused": 0, "missing": 0, "stopped": f"budget: {e}"}
            except DeeplineError as e:
                logger.warning("hiring: LinkedIn people search failed, falling back per company: %s", e)

    for lead in leads:
        company = db.get(Company, lead.company_id)
        contact = None if company.id in harvested else known_contact(company)
        if contact is not None:
            reused += 1
        elif company.id in harvested:
            contact = harvested[company.id]
            if contact is None:
                lead.state = STATE_CONTACT_MISSING
                db.commit()
                missing += 1
                continue
        else:
            spent_before = scope.spent_usd if scope else 0.0
            blocked_before = scope.blocked if scope else 0
            try:
                # Free leadership list + Google, the agent picks the person, then -- only when email is
                # an outreach channel -- the per-result email waterfall (icypeas $0.014 -> hunter ->
                # leadmagic, $0 on a miss). LinkedIn-only: ~$0.035 a company, all of it the LinkedIn lookup.
                # The paid search_contact fallback is off -- it bills $0.056 for every person it
                # returns (3 per call, up to 3 calls), which is what made a contact cost ~$0.25.
                new_contacts, _used_paid = find_decision_makers(company, db, tenant_id, allow_paid_fallback=False, max_contacts=1,
                                                                resolve_email="email" in channels)
            except SpendBlocked as e:
                return {"found": found, "reused": reused, "missing": missing, "stopped": f"budget: {e}"}
            except Exception as e:  # noqa: BLE001
                db.rollback()
                lead.last_error = f"decision makers: {type(e).__name__}: {e}"[:500]
                db.commit()
                continue
            if scope:
                lead.spend_usd = (lead.spend_usd or 0.0) + max(0.0, scope.spent_usd - spent_before)
            contact = next((c for c in new_contacts if _reachable(c, channels)), None)
            if contact is None and scope and scope.blocked > blocked_before:
                # The finder swallows a refused paid call as "nobody found". Nothing was bought,
                # so the lead waits for tomorrow's budget instead of being marked missing.
                db.commit()
                return {"found": found, "reused": reused, "missing": missing, "stopped": "budget: a paid lookup was refused"}
            if contact is None:
                lead.state = STATE_CONTACT_MISSING
                db.commit()
                missing += 1
                continue

        try:
            opportunity, _strategy = _write_opportunity(db, tenant_id, lead, company, play=PLAY, objective=OBJECTIVE,
                                                        source_note="in its open job postings")
        except Exception as e:  # noqa: BLE001
            db.rollback()
            lead.state = STATE_FAILED
            lead.last_error = f"write rows: {type(e).__name__}: {e}"[:500]
            db.commit()
            continue
        lead.contact_id, lead.opportunity_id = contact.id, opportunity.id
        lead.person_name = " ".join(p for p in (contact.first_name, contact.last_name) if p) or None
        lead.state = STATE_CONTACT_FOUND
        db.commit()
        found += 1
    return {"found": found, "reused": reused, "missing": missing, "stopped": None}


# ---------------------------------------------------------------------------------------------
# sense (default) -- HarvestAPI LinkedIn job search per ICP trigger title ($0.001 a page of 25),
# then exact company facts ($0.003) only for companies we have never seen, free size/US/industry
# checks, and the full job description ($0.001) only for the ones that pass.
# ---------------------------------------------------------------------------------------------

HARVEST_SEEN_KEY = "hiring_play_checked_companies"
NON_BUYER_INDUSTRIES = {"IT Services and IT Consulting", "Staffing and Recruiting", "Business Consulting and Services",
                        "Outsourcing and Offshoring Consulting", "Human Resources Services"}


def _seen_param(db: Session, tenant_id: int):
    from app.db.models import Parameter

    param = db.query(Parameter).filter(Parameter.tenant_id == tenant_id, Parameter.key == HARVEST_SEEN_KEY).first()
    if param is None:
        param = Parameter(tenant_id=tenant_id, key=HARVEST_SEEN_KEY, value={},
                          description="Play B: LinkedIn companies already checked (never paid for twice) -> outcome")
        db.add(param)
        db.commit()
    return param


def _known_company_names(db: Session, tenant_id: int) -> set[str]:
    from app.harvestapi import universal_name

    rows = db.query(Company.linkedin_url).join(Batch, Company.batch_id == Batch.id).filter(Batch.tenant_id == tenant_id).all()
    return {u for (url,) in rows if (u := universal_name(url))}


def sense_harvest(db: Session, tenant_id: int, max_companies: int = 25, posted: str = "week") -> dict:
    from app.deepline_client import DeeplineError, DeeplineSpendBlocked
    from app.gtm_os.icp.icp_config import get_icp_config
    from app.gtm_os.intelligence.signal import GtmSignal
    from app.gtm_os.orchestration.discovery_profiles import headcount_band_for_icp, titles_for_icp
    from app import harvestapi

    icps = [i for i in get_icp_config(db, tenant_id) if i.get("enabled", True)]
    result = {"source": "harvestapi", "postings": 0, "new_companies": 0, "checked": 0, "kept": 0,
              "rejected": {}, "stopped": None}

    # 1. postings, grouped by company, first ICP to find a company wins
    candidates: dict[str, dict] = {}
    try:
        for icp in icps:
            for title in titles_for_icp(icp):
                for job in harvestapi.search_jobs(title, posted=posted):
                    result["postings"] += 1
                    u = job["company_universal_name"]
                    if u:
                        candidates.setdefault(u, {"icp": icp, "jobs": []})["jobs"].append(job)
    except DeeplineSpendBlocked as e:
        result["stopped"] = f"budget: {e}"
    except DeeplineError as e:
        result["stopped"] = f"job search failed: {e}"

    seen = _seen_param(db, tenant_id)
    checked = dict(seen.value or {})
    known = _known_company_names(db, tenant_id)
    new = [(u, c) for u, c in candidates.items() if u not in known and u not in checked]
    result["new_companies"] = len(new)

    for u, cand in new[:max_companies]:
        if result["stopped"]:
            break
        icp = cand["icp"]
        try:
            facts = harvestapi.get_company(u)
        except DeeplineSpendBlocked as e:
            result["stopped"] = f"budget: {e}"
            break
        except DeeplineError:
            continue
        result["checked"] += 1
        lo, hi = headcount_band_for_icp(icp)
        size = (facts or {}).get("employee_count")
        reason = None
        if not facts:
            reason = "not_found"
        elif size is None or not (lo <= size <= hi):
            reason = "size_outside_icp"
        elif facts.get("hq_country") and facts["hq_country"].upper() not in ("US", "USA", "UNITED STATES"):
            reason = "non_us_hq"
        elif facts.get("industry") in NON_BUYER_INDUSTRIES:
            reason = "services_industry"
        checked[u] = reason or "kept"
        try:
            # Saved per company, not at the end: a dropped connection later in the run must not
            # lose lookups already paid for (they would be bought again next run).
            seen.value = dict(checked)
            db.commit()
        except OperationalError:
            db.rollback()
        if reason:
            result["rejected"][reason] = result["rejected"].get(reason, 0) + 1
            continue
        try:
            _keep_company(db, tenant_id, u, cand, facts, size, result)
        except OperationalError as e:
            # One company's write failed (connection dropped mid-run); the rest carry on.
            db.rollback()
            result.setdefault("write_errors", []).append(f"{u}: {str(e)[:120]}")
            checked.pop(u, None)  # not saved -- let the next run pick it up again
    try:
        seen.value = dict(checked)
        db.commit()
    except OperationalError:
        db.rollback()
    return result


def _keep_company(db: Session, tenant_id: int, u: str, cand: dict, facts: dict, size: int, result: dict) -> None:
    from app.deepline_client import DeeplineError, DeeplineSpendBlocked
    from app.gtm_os.intelligence.signal import GtmSignal
    from app import harvestapi

    batch = (db.query(Batch).filter(Batch.tenant_id == tenant_id, Batch.source == PLAY_BATCH_SOURCE,
                                    Batch.name == f"Play B -- hiring (LinkedIn) {datetime.utcnow().date().isoformat()}").first())

    if batch is None:
        batch = Batch(tenant_id=tenant_id, name=f"Play B -- hiring (LinkedIn) {datetime.utcnow().date().isoformat()}",
                      source=PLAY_BATCH_SOURCE, status="in_progress")
        db.add(batch)
        db.commit()
    website = facts.get("website")
    company = Company(batch_id=batch.id, name=facts.get("name") or cand["jobs"][0]["company_name"] or u,
                      domain=_domain(website), linkedin_url=facts.get("linkedin_url") or f"https://www.linkedin.com/company/{u}",
                      industry=facts.get("industry"), employee_count=size, location=facts.get("hq_text") or None,
                      source="harvestapi:linkedin_jobs", active_job_title=cand["jobs"][0]["title"],
                      hiring_signal_posting_count=len(cand["jobs"]))
    db.add(company)
    db.commit()
    for i, job in enumerate(cand["jobs"][:POSTINGS_PER_LEAD]):
        description = None
        if i == 0 and job["job_id"]:
            try:
                description = (harvestapi.get_job(job["job_id"]) or {}).get("descriptionText")
            except DeeplineSpendBlocked as e:
                result["stopped"] = f"budget: {e}"
            except DeeplineError:
                pass
        ref = job["job_id"] or job["url"]
        if not ref or db.query(GtmSignal.id).filter(GtmSignal.tenant_id == tenant_id, GtmSignal.source == "linkedin_job",
                                                   GtmSignal.source_ref == ref).first():
            continue
        db.add(GtmSignal(
            tenant_id=tenant_id, source="linkedin_job", source_ref=ref, signal_type="job_posting",
            observed_at=_parse_iso(job["posted_at"]), company_id=company.id, company_name_raw=company.name,
            raw_evidence=job, dedup_key=f"linkedin_job:{ref}",
            extracted_info={"title": job["title"], "location": job["location"], "description_text": (description or "")[:6000],
                            "organization_domain": website, "organization_headcount": size,
                            "organization_industry": facts.get("industry"), "organization_description": facts.get("description")},
            company_resolution_status="resolved", company_resolution_method="explicit",
            company_resolution_reason="linked at discovery: HarvestAPI job posting names this company",
            company_resolved_at=datetime.utcnow()))
    db.commit()
    result["kept"] += 1


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------
# sense (preferred) -- one direct Prospeo search per enabled ICP: companies hiring the ICP's
# trigger roles, in its revenue and size band, with their decision maker's LinkedIn URL.
# ~$0.0004 a person. Used whenever a prospeo_api_key credential exists.
# ---------------------------------------------------------------------------------------------

PROSPEO_CURSOR_KEY = "hiring_play_prospeo_cursor"
DECISION_MAKER_SENIORITY = ["Founder/Owner", "C-Suite", "Vice President", "Head"]
DECISION_MAKER_DEPARTMENTS = ["C-Suite", "Sales"]
PEOPLE_PER_COMPANY = 2              # the Qualifier / drafter pick between them, at no extra cost


def prospeo_filters_for_icp(icp: dict) -> dict:
    """Built from the ICP config itself, so editing an ICP changes the search -- nothing hand-typed."""
    from app.gtm_os.orchestration.discovery_profiles import ICP_INDUSTRY_FILTER, headcount_band_for_icp, titles_for_icp
    from app.prospeo_client import revenue_filter

    lo, hi = headcount_band_for_icp(icp)
    filters = {
        "company_job_posting_hiring_for": {"include": titles_for_icp(icp), "match_type": "contains"},
        "company_headcount_custom": {"min": lo, "max": hi},
        "company_industry": {"include": list(ICP_INDUSTRY_FILTER)},
        "company_location_search": {"include": ["United States"]},
        "person_seniority": {"include": DECISION_MAKER_SENIORITY},
        "person_department": {"include": DECISION_MAKER_DEPARTMENTS},
        "max_person_per_company": PEOPLE_PER_COMPANY,
    }
    revenue = revenue_filter(icp.get("revenue_min_usd"), icp.get("revenue_max_usd"))
    if revenue:
        filters["company_revenue"] = revenue
    return filters


def _cursor(db: Session, tenant_id: int):
    from app.db.models import Parameter

    param = db.query(Parameter).filter(Parameter.tenant_id == tenant_id, Parameter.key == PROSPEO_CURSOR_KEY).first()
    if param is None:
        param = Parameter(tenant_id=tenant_id, key=PROSPEO_CURSOR_KEY, value={},
                          description="Play B: next Prospeo search page per ICP (reset when the ICP's filters change)")
        db.add(param)
        db.commit()
    return param


def _domain(value: str | None) -> str | None:
    if not value:
        return None
    d = value.lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].strip()
    return d or None


def _prospeo_evidence(icp: dict, person: dict, company: dict) -> str:
    import json

    keep = {k: v for k, v in company.items() if v not in (None, "", [], {}) and "logo" not in k}
    return (
        f"Found by searching for companies hiring for: {', '.join(prospeo_filters_for_icp(icp)['company_job_posting_hiring_for']['include'])} "
        f"(ICP {icp['id']}: {icp.get('name')})\n"
        f"Person: {person.get('full_name') or ''} -- {person.get('current_job_title') or person.get('job_title') or ''} "
        f"({person.get('linkedin_url') or ''})\nHeadline: {person.get('headline') or ''}\n\n"
        f"Company data:\n{json.dumps(keep, default=str)[:4000]}"
    )


def _ingest_prospeo_result(db: Session, tenant_id: int, batch: Batch, icp: dict, result: dict, known: set) -> str:
    """One search hit -> Company + Contact + signal + lead. Returns what happened."""
    from app.gtm_os.intelligence.signal import GtmSignal
    from app.gtm_os.plays.lead import normalize_linkedin_url

    person, company_data = result.get("person") or {}, result.get("company") or {}
    linkedin = normalize_linkedin_url(person.get("linkedin_url"))
    domain = _domain(company_data.get("domain") or company_data.get("website"))
    if not linkedin or not domain:
        return "incomplete"

    company = (
        db.query(Company).join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id, Company.domain == domain).first()
    )
    if company is None:
        company = Company(batch_id=batch.id, name=company_data.get("name") or domain, domain=domain,
                          linkedin_url=company_data.get("linkedin_url"), industry=company_data.get("industry"),
                          employee_count=company_data.get("employee_count") if isinstance(company_data.get("employee_count"), int) else None,
                          source="prospeo:search_person")
        db.add(company)
        db.commit()
    elif _already_in_outreach(db, company.id):
        return "in_outreach"

    contact = (db.query(Contact).filter(Contact.company_id == company.id, Contact.linkedin_url.ilike(f"%{linkedin}%")).first())
    if contact is None:
        contact = Contact(company_id=company.id, first_name=person.get("first_name"), last_name=person.get("last_name"),
                          title=person.get("current_job_title") or person.get("job_title"),
                          linkedin_url=f"https://www.{linkedin}", thread_role="hiring_play_decision_maker",
                          matched_title_reasoning=f"Prospeo search for ICP {icp['id']}")
        db.add(contact)
        db.commit()

    ref = str(person.get("person_id") or person.get("id") or linkedin)
    if not db.query(GtmSignal.id).filter(GtmSignal.tenant_id == tenant_id, GtmSignal.source == "prospeo_search",
                                         GtmSignal.source_ref == ref).first():
        db.add(GtmSignal(tenant_id=tenant_id, source="prospeo_search", source_ref=ref, signal_type="hiring_search_match",
                         company_id=company.id, company_name_raw=company.name, contact_id=contact.id,
                         person_name_raw=person.get("full_name"), raw_evidence=result,
                         extracted_info={"icp_id": icp["id"], "hiring_for": titles_for(icp)},
                         dedup_key=f"prospeo_search:{ref}", company_resolution_status="resolved",
                         company_resolution_method="explicit"))
        db.commit()

    key = lead_key_for(company.id)
    if key in known:
        return "known"
    db.add(GtmLead(tenant_id=tenant_id, play=PLAY, lead_key=key, company_id=company.id, contact_id=contact.id,
                   person_name=person.get("full_name"), person_linkedin_url=linkedin, state=STATE_SIGNAL,
                   evidence=_prospeo_evidence(icp, person, company_data)))
    db.commit()
    known.add(key)
    return "created"


def titles_for(icp: dict) -> list[str]:
    from app.gtm_os.orchestration.discovery_profiles import titles_for_icp
    return titles_for_icp(icp)


def sense_prospeo(db: Session, tenant_id: int, pages_per_icp: int = 1) -> dict:
    import hashlib
    import json

    from app.gtm_os.icp.icp_config import get_icp_config
    from app.prospeo_client import ProspeoError, search_person

    icps = [i for i in get_icp_config(db, tenant_id) if i.get("enabled", True)]
    batch = Batch(tenant_id=tenant_id, name=f"Play B -- hiring (Prospeo) {datetime.utcnow().date().isoformat()}",
                  source=PLAY_BATCH_SOURCE, status="in_progress")
    db.add(batch)
    db.commit()

    cursor = _cursor(db, tenant_id)
    state = dict(cursor.value or {})
    known = {row[0] for row in db.query(GtmLead.lead_key).filter(GtmLead.tenant_id == tenant_id, GtmLead.play == PLAY)}
    result = {"source": "prospeo", "batch_id": batch.id, "people": 0, "outcomes": {}, "icps": {}, "stopped": None}
    for icp in icps:
        filters = prospeo_filters_for_icp(icp)
        fingerprint = hashlib.sha1(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:12]
        entry = state.get(icp["id"]) if (state.get(icp["id"]) or {}).get("filters") == fingerprint else None
        page = (entry or {}).get("next_page", 1)
        for _ in range(pages_per_icp):
            try:
                found = search_person(db, tenant_id, filters, page=page)
            except SpendBlocked as e:
                result["stopped"] = f"budget: {e}"
                break
            except ProspeoError as e:
                result["icps"][icp["id"]] = {"error": str(e), "code": e.code}
                break
            people = found["results"]
            result["people"] += len(people)
            for r in people:
                outcome = _ingest_prospeo_result(db, tenant_id, batch, icp, r, known)
                result["outcomes"][outcome] = result["outcomes"].get(outcome, 0) + 1
            total_pages = (found.get("pagination") or {}).get("total_page") or 0
            result["icps"][icp["id"]] = {"page": page, "people": len(people), "total_pages": total_pages}
            page = page + 1 if people and page < total_pages else 1   # wrap around once exhausted
            if not people:
                break
        state[icp["id"]] = {"filters": fingerprint, "next_page": page}
        cursor.value = dict(state)
        db.commit()
        if result["stopped"]:
            break
    return result


# ---------------------------------------------------------------------------------------------
# sense (fallback) -- the existing Apify job discovery, per enabled ICP, small and budget-checked
# ---------------------------------------------------------------------------------------------

def sense(db: Session, tenant_id: int, postings_per_profile: int = 20) -> dict:
    from app.gtm_os.icp.icp_config import get_icp_config
    from app.gtm_os.orchestration.discovery_profiles import get_enabled_discovery_profiles
    from app.phases.apify_discovery import run_apify_discovery
    from app.apify_client import billing_tenant_id

    enabled_icps = {i["id"] for i in get_icp_config(db, tenant_id) if i.get("enabled", True)}
    profiles = [p for p in get_enabled_discovery_profiles(db, tenant_id) if p.get("id") in enabled_icps]
    if not profiles:
        return {"companies": 0, "postings": 0, "stopped": "no discovery profile for an enabled ICP"}

    batch = Batch(tenant_id=tenant_id, name=f"Play B -- hiring {datetime.utcnow().date().isoformat()}",
                  source=PLAY_BATCH_SOURCE, status="in_progress")
    db.add(batch)
    db.commit()

    result = {"batch_id": batch.id, "companies": 0, "postings": 0, "profiles": {}, "stopped": None}
    for profile in profiles:
        r = run_apify_discovery(
            batch.id, db, tenant_id, target=postings_per_profile, limit=postings_per_profile,
            time_range=profile.get("time_range") or "7d", title_search=profile.get("title_search"),
            employee_min=profile.get("employee_min"), employee_max=profile.get("employee_max"),
            industry_filter=profile.get("industry_filter"), location_search=profile.get("location_search"),
            budget_tenant_id=billing_tenant_id(db, tenant_id), assess_team=False,
        )
        result["companies"] += r.get("companies_discovered") or 0
        result["postings"] += r.get("postings_checked") or 0
        result["profiles"][profile["id"]] = {k: r.get(k) for k in ("companies_discovered", "postings_checked", "rejection_breakdown", "api_error")}
        if r.get("budget_stopped_early"):
            result["stopped"] = r.get("api_error")
            break
    return result


# ---------------------------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------------------------

def run_play_b(db: Session, tenant_id: int, run_cap_usd: float | None = None, do_sense: bool = True,
               postings_per_profile: int = 20, qualify_limit: int = 20, contact_limit: int = 5, draft_limit: int = 5) -> dict:
    """One pass: sense -> ingest -> qualify -> contact -> draft. Everything paid is reserved against
    the tenant's combined daily cap and `run_cap_usd` (defaults to the config's spend.run_cap_usd)."""
    from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run, get_control_config
    from app.spend_ledger import spend_scope

    try:
        check_can_run(db, tenant_id)
    except ControlPlaneHalted as e:
        return {"status": "skipped", "reason": str(e)}

    if run_cap_usd is None:
        run_cap_usd = (get_control_config(db, tenant_id).get("spend") or {}).get("run_cap_usd")

    result: dict = {"status": "completed", "play": PLAY}
    with spend_scope(db, tenant_id, PLAY, run_cap_usd=run_cap_usd) as scope:
        if do_sense:
            from app.prospeo_client import get_api_key as prospeo_key

            if prospeo_key(db, tenant_id):
                result["sense"] = sense_prospeo(db, tenant_id)
            else:
                result["sense"] = sense_harvest(db, tenant_id)
        result["ingest"] = ingest_new_signals(db, tenant_id)
        result["qualify"] = qualify_leads(db, tenant_id, limit=qualify_limit)
        result["contact"] = find_contacts(db, tenant_id, limit=contact_limit)
        result["draft"] = draft_messages(db, tenant_id, limit=draft_limit, play=PLAY)
        result["spent_usd"] = round(scope.spent_usd, 4)
    result["run_cap_usd"] = run_cap_usd
    return result
