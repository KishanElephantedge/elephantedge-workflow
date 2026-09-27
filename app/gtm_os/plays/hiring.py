"""Play B -- hiring signals (a company hiring for the problem we solve).

    sense      the existing Apify job discovery, once per ENABLED ICP's discovery profile, with a
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
  when revenue is unknown, judge it from employees, funding and what the company does
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
    scope = current_spend_scope()
    found = missing = reused = 0
    for lead in leads:
        company = db.get(Company, lead.company_id)
        # Someone we already know at this company costs nothing -- prefer one with an email.
        known = sorted((c for c in get_eligible_contacts(db, company.id) if _reachable(c, channels)),
                       key=lambda c: not c.linkedin_url if channels[0] == "linkedin" else not c.email)
        contact = known[0] if known else None
        if contact is not None:
            reused += 1
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
# sense -- the existing Apify job discovery, per enabled ICP, small and budget-checked
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
            result["sense"] = sense(db, tenant_id, postings_per_profile=postings_per_profile)
        result["ingest"] = ingest_new_signals(db, tenant_id)
        result["qualify"] = qualify_leads(db, tenant_id, limit=qualify_limit)
        result["contact"] = find_contacts(db, tenant_id, limit=contact_limit)
        result["draft"] = draft_messages(db, tenant_id, limit=draft_limit, play=PLAY)
        result["spent_usd"] = round(scope.spent_usd, 4)
    result["run_cap_usd"] = run_cap_usd
    return result
