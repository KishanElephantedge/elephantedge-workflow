"""Play A -- LinkedIn post engagement (modelled on Quicklead's social signals).

    sense      problem-language keyword search over LinkedIn posts, then the commenters of the
               most engaged posts (existing sensing adapters, one scrape call per post)
    ingest     every new post author / commenter becomes one GtmLead (state "signal"); a person
               already known to this play is never ingested again
    qualify    ONE LLM call per lead: is this a real decision-maker at a company we fit, showing
               real intent? -> "qualified" or "rejected". Nothing is paid for before this says yes.
    contact    ONE Prospeo enrich-person call (LinkedIn URL -> verified email + company), only for
               qualified leads. Writes the Company/Contact and the Opportunity/Strategy rows the
               existing Messages, Pipeline and approval screens already read.
    draft      the existing message drafter, targeted at this exact person -> awaits approval

Every paid call runs inside spend_scope, so it is reserved against the tenant's one combined daily
cap (spend.daily_cap_usd) and this run's cap (spend.run_cap_usd) BEFORE it is made. A budget
refusal stops that step for the day without changing any lead's state -- the lead simply waits.
There is no outer loop and no backlog re-scan: each step takes the next N leads in its state.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import Batch, Company, Contact
from app.gtm_os.plays.lead import (
    STATE_CONTACT_FOUND, STATE_CONTACT_MISSING, STATE_DRAFTED, STATE_FAILED, STATE_QUALIFIED,
    STATE_REJECTED, STATE_SIGNAL, GtmLead, normalize_linkedin_url,
)
from app.spend_ledger import SpendBlocked

logger = logging.getLogger(__name__)

PLAY = "post_engagement"
PLAY_BATCH_NAME = "Play A -- LinkedIn post engagement"
SIGNAL_SOURCES = ("linkedin_engagement", "linkedin_post")
SIGNAL_LOOKBACK_DAYS = 14          # never ingest old backlog -- only recent signals
DEFAULT_MIN_FIT_SCORE = 70
QUALIFYING_INTENTS = {"buying", "pain", "seeking_help", "evaluating_tools", "hiring_sales_role"}


# ---------------------------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------------------------

def _lead_fields_from_signal(signal) -> dict | None:
    """Who this signal is about and what they said. None when it isn't a person we can reach."""
    from app.gtm_os.intelligence.engagement_intent import is_internal_hiring_post

    info = signal.extracted_info or {}
    if signal.source == "linkedin_engagement":
        url = info.get("author_profile_url") or signal.source_ref
        comment = (info.get("comment_text") or "").strip()
        post = (info.get("post_text") or "").strip()
        evidence = f"Comment: {comment}\n\nOn a post by {info.get('post_author_name') or 'someone'}: {post[:800]}"
    else:  # linkedin_post -- the post's author
        if info.get("author_type") == "Company":
            return None
        text = (info.get("text") or "").strip()
        if is_internal_hiring_post(text):
            return None  # a company recruiting for its own role, not a buyer
        url = info.get("author_profile_url")
        evidence = f"Post: {text[:1200]}\n\nAuthor headline: {info.get('headline') or ''}"

    url = normalize_linkedin_url(url)
    if not url or "/in/" not in url:
        return None  # a search URL, a company page, or nothing -- not a person
    return {"person_linkedin_url": url, "person_name": signal.person_name_raw, "evidence": evidence}


def ingest_new_signals(db: Session, tenant_id: int, limit: int = 100, now: datetime | None = None) -> dict:
    from app.gtm_os.intelligence.signal import GtmSignal

    now = now or datetime.utcnow()
    known = {
        row[0] for row in db.query(GtmLead.lead_key)
        .filter(GtmLead.tenant_id == tenant_id, GtmLead.play == PLAY).all()
    }
    signals = (
        db.query(GtmSignal)
        .filter(
            GtmSignal.tenant_id == tenant_id,
            GtmSignal.source.in_(SIGNAL_SOURCES),
            GtmSignal.captured_at >= now - timedelta(days=SIGNAL_LOOKBACK_DAYS),
        )
        .order_by(GtmSignal.captured_at.desc())
        .limit(limit * 5)
        .all()
    )
    created = skipped = 0
    for signal in signals:
        if created >= limit:
            break
        fields = _lead_fields_from_signal(signal)
        if fields is None or fields["person_linkedin_url"] in known:
            skipped += 1
            continue
        db.add(GtmLead(tenant_id=tenant_id, play=PLAY, lead_key=fields["person_linkedin_url"], signal_id=signal.id,
                       state=STATE_SIGNAL, **fields))
        known.add(fields["person_linkedin_url"])
        created += 1
    db.commit()
    return {"created": created, "skipped": skipped}


# ---------------------------------------------------------------------------------------------
# qualify -- the Qualifier agent
# ---------------------------------------------------------------------------------------------

QUALIFIER_PROMPT = """You qualify B2B sales prospects for {company_name}, which sells the offerings below.
A person surfaced because of something they posted or commented on LinkedIn. Decide if they are
worth contacting. Be strict: most people should be rejected.

OUR OFFERINGS:
{offerings}

OUR IDEAL CUSTOMERS:
{icps}

THE PERSON:
Name: {person_name}
LinkedIn: {linkedin_url}
What they wrote / engaged with:
\"\"\"{evidence}\"\"\"

Reject if ANY of these is true:
- they sell sales services, consulting, agency or recruiting work themselves (a competitor or vendor)
- they are job-seeking, a recruiter, a student, or an individual contributor with no buying power
- their words show no real problem, need or buying interest (general opinion or engagement bait)
- their company clearly does not match our ideal customers

Qualify only a founder, CEO, or sales/revenue/growth leader at a plausible B2B company who shows
a real problem or buying interest that one of our offerings solves.

Return ONLY this JSON:
{{
  "qualified": true or false,
  "icp_fit_score": 0-100,
  "intent": one of "buying", "pain", "seeking_help", "evaluating_tools", "hiring_sales_role", "sharing_opinion", "selling_services", "job_seeking", "other",
  "role_guess": "their likely role",
  "company_guess": "their likely company or null",
  "matched_icp_id": one of {icp_ids} or null,
  "matched_offering": one of {offering_names} or null,
  "reason": "one or two sentences explaining the decision",
  "evidence_quote": "the exact words from their text that justify it (short)",
  "problem_statement": "their problem in one plain sentence, or null",
  "demand_statement": "what they appear to want, in one plain sentence, or null",
  "positioning_angle": "how to open a conversation with them, one sentence, or null"
}}"""


def _qualifier_context(db: Session, tenant_id: int) -> dict:
    from app.gtm_os.icp.icp_config import get_icp_config
    from app.gtm_os.opportunity.offering_config import get_offering_config

    icps = [i for i in get_icp_config(db, tenant_id) if i.get("enabled", True)]
    offerings = get_offering_config(db, tenant_id)
    return {
        "company_name": "Elephant Edge" if tenant_id == 2 else "our company",
        "offerings": "\n".join(f"- {o['name']}: {o.get('description') or ''}" for o in offerings),
        "icps": "\n".join(f"- {i['id']} ({i['name']}): {i.get('description') or ''}" for i in icps),
        "icp_ids": ", ".join(f'"{i["id"]}"' for i in icps),
        "offering_names": ", ".join(f'"{o["name"]}"' for o in offerings),
        "valid_icps": {i["id"]: i["name"] for i in icps},
        "valid_offerings": {o["name"] for o in offerings},
    }


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
        prompt = QUALIFIER_PROMPT.format(
            person_name=lead.person_name or "unknown", linkedin_url=lead.person_linkedin_url,
            evidence=(lead.evidence or "")[:2500], **{k: v for k, v in ctx.items() if not k.startswith("valid_")},
        )
        try:
            verdict = generate_json(prompt, db, tenant_id, max_tokens=600)
        except (SpendBlocked, LlmBudgetExceeded) as e:
            return {"qualified": qualified, "rejected": rejected, "stopped": f"budget: {e}"}
        except Exception as e:  # noqa: BLE001 -- one bad response must not stop the rest
            lead.last_error = f"qualifier: {type(e).__name__}: {e}"[:500]
            db.commit()
            continue

        score = verdict.get("icp_fit_score")
        score = int(score) if isinstance(score, (int, float)) else 0
        intent = verdict.get("intent")
        offering = verdict.get("matched_offering") if verdict.get("matched_offering") in ctx["valid_offerings"] else None
        passes = bool(verdict.get("qualified")) and score >= min_fit_score and intent in QUALIFYING_INTENTS and offering is not None

        lead.icp_fit_score = score
        lead.intent = intent
        lead.qualifier_reason = verdict.get("reason")
        lead.qualifier_output = verdict
        lead.state = STATE_QUALIFIED if passes else STATE_REJECTED
        db.commit()
        qualified += passes
        rejected += not passes
    return {"qualified": qualified, "rejected": rejected, "stopped": None}


# ---------------------------------------------------------------------------------------------
# contact -- one paid Prospeo lookup per qualified lead
# ---------------------------------------------------------------------------------------------

def _find(obj, key):
    """First value for `key` anywhere in a nested dict/list (provider response shapes vary)."""
    if isinstance(obj, dict):
        if key in obj and obj[key] not in (None, "", [], {}):
            return obj[key]
        for v in obj.values():
            found = _find(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find(v, key)
            if found is not None:
                return found
    return None


def _parse_prospeo_person(response: dict) -> dict | None:
    """Pulls {first_name, last_name, title, email, company_name, company_domain,
    company_linkedin_url} out of a prospeo_enrich_person response, or None when no verified
    email came back."""
    person = _find(response, "person") or response
    company = _find(response, "company") or {}
    email_obj = person.get("email") if isinstance(person, dict) else None
    email = email_obj.get("email") if isinstance(email_obj, dict) else (email_obj if isinstance(email_obj, str) else None)
    status = (email_obj.get("status") if isinstance(email_obj, dict) else None) or ""
    if not email or (status and status.upper() not in ("VERIFIED", "VALID")):
        return None
    return {
        "first_name": person.get("first_name"),
        "last_name": person.get("last_name"),
        "title": person.get("current_job_title") or person.get("job_title") or person.get("title"),
        "email": email.lower(),
        "company_name": company.get("name") if isinstance(company, dict) else None,
        "company_domain": (company.get("domain") or company.get("website")) if isinstance(company, dict) else None,
        "company_linkedin_url": company.get("linkedin_url") if isinstance(company, dict) else None,
    }


def _play_batch(db: Session, tenant_id: int) -> Batch:
    batch = db.query(Batch).filter(Batch.tenant_id == tenant_id, Batch.name == PLAY_BATCH_NAME).first()
    if batch is None:
        batch = Batch(tenant_id=tenant_id, name=PLAY_BATCH_NAME, source="play_a", status="in_progress")
        db.add(batch)
        db.commit()
    return batch


def _company_for(db: Session, tenant_id: int, name: str, domain: str | None, linkedin_url: str | None) -> Company:
    """Reuse this tenant's existing company by domain when there is one -- never a duplicate row."""
    if domain:
        domain = domain.lower().replace("https://", "").replace("http://", "").replace("www.", "").strip("/")
        existing = (
            db.query(Company).join(Batch, Company.batch_id == Batch.id)
            .filter(Batch.tenant_id == tenant_id, Company.domain == domain).first()
        )
        if existing:
            return existing
    company = Company(batch_id=_play_batch(db, tenant_id).id, name=name, domain=domain, linkedin_url=linkedin_url)
    db.add(company)
    db.commit()
    return company


def _write_opportunity(db: Session, tenant_id: int, lead: GtmLead, company: Company, play: str = PLAY,
                       objective: str = "Open a conversation about the problem they raised on LinkedIn",
                       source_note: str = "on LinkedIn") -> tuple:
    """The Qualifier's one verdict, written as the Problem -> Demand -> Opportunity -> Strategy rows
    the existing drafter, Pipeline and Messages screens read. Replaces four LLM stages with one.
    Shared by every play; `play`/`objective`/`source_note` say where the lead came from."""
    from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
    from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis
    from app.gtm_os.opportunity.opportunity import Opportunity
    from app.gtm_os.strategy.strategy import GtmStrategy

    v = lead.qualifier_output or {}
    problem_text = v.get("problem_statement") or f"{company.name} shows a sales/growth problem {source_note}."
    demand_text = v.get("demand_statement") or f"{company.name} appears open to outside help with sales."
    evidence = {"play": play, "lead_id": lead.id, "evidence_quote": v.get("evidence_quote"), "intent": lead.intent,
                "icp_fit_score": lead.icp_fit_score, "reason": lead.qualifier_reason}

    problem = ProblemHypothesis(tenant_id=tenant_id, company_id=company.id, company_name_raw=company.name,
                                person_name_raw=lead.person_name, affected_function="sales",
                                problem_statement=problem_text, reasoning_note=lead.qualifier_reason,
                                confidence=evidence, first_observed_at=lead.created_at)
    db.add(problem)
    db.flush()
    demand = DemandHypothesis(tenant_id=tenant_id, company_id=company.id, company_name_raw=company.name,
                              problem_hypothesis_id=problem.id, affected_function="sales",
                              demand_statement=demand_text, reasoning_note=lead.qualifier_reason,
                              confidence=evidence, first_observed_at=lead.created_at)
    db.add(demand)
    db.flush()
    icp_id = v.get("matched_icp_id")
    opportunity = Opportunity(
        tenant_id=tenant_id, company_id=company.id, company_name_raw=company.name,
        demand_hypothesis_id=demand.id, problem_hypothesis_id=problem.id, affected_function="sales",
        opportunity_statement=f"{lead.person_name or 'A leader'} at {company.name}: {problem_text}",
        reasoning_note=lead.qualifier_reason, status="qualified", confidence=evidence,
        first_observed_at=lead.created_at,
        icp_context={"has_icp_match": bool(icp_id), "status": "matched" if icp_id else "no_match_recorded",
                     "matches": [{"icp_id": icp_id, "reasons": [lead.qualifier_reason]}] if icp_id else [],
                     "source": f"{play}_qualifier"},
    )
    db.add(opportunity)
    db.flush()
    strategy = GtmStrategy(
        tenant_id=tenant_id, opportunity_id=opportunity.id, strategy_type="consultative",
        recommended_approach=v.get("positioning_angle"), target_function="sales",
        positioning_angle=v.get("positioning_angle"), offering_fit_status="candidate_match",
        matched_offering_name=v.get("matched_offering"), evidence_basis=evidence,
        missing_information=[], decision_maker_known=True, recommended_next_step="prepare_message",
        action_plan=[{"action_type": "prepare_message", "objective": objective,
                      "target_function": "sales", "rationale": lead.qualifier_reason, "prerequisite": None, "status": "ready"}],
        reasoning_note=f"{play} qualifier verdict (lead {lead.id}).",
    )
    db.add(strategy)
    db.commit()
    return opportunity, strategy


def find_contacts(db: Session, tenant_id: int, limit: int = 10) -> dict:
    from app.deepline_client import DeeplineError, DeeplineSpendBlocked, execute_tool
    from app.spend_ledger import settle_spend

    leads = (
        db.query(GtmLead)
        .filter(GtmLead.tenant_id == tenant_id, GtmLead.play == PLAY, GtmLead.state == STATE_QUALIFIED)
        .order_by(GtmLead.icp_fit_score.desc(), GtmLead.created_at.desc())
        .limit(limit)
        .all()
    )
    found = missing = 0
    for lead in leads:
        url = f"https://www.{lead.person_linkedin_url}"
        try:
            response = execute_tool("prospeo_enrich_person", {"linkedin_url": url, "only_verified_email": True})
        except DeeplineSpendBlocked as e:
            return {"found": found, "missing": missing, "stopped": f"budget: {e}"}
        except DeeplineError as e:
            lead.last_error = f"prospeo: {e}"[:500]
            db.commit()
            continue

        ledger_id = response.get("_spend_ledger_id")
        person = _parse_prospeo_person(response)
        if person is None:
            settle_spend(db, ledger_id, 0.0)  # billed per verified result -- a miss costs nothing
            lead.state = STATE_CONTACT_MISSING
            db.commit()
            missing += 1
            continue

        lead.spend_usd = (lead.spend_usd or 0.0) + 0.055
        try:
            company = _company_for(db, tenant_id, person["company_name"] or (lead.qualifier_output or {}).get("company_guess") or "Unknown company",
                                   person["company_domain"], person["company_linkedin_url"])
            contact = (
                db.query(Contact)
                .filter(Contact.company_id == company.id, Contact.linkedin_url.ilike(f"%{lead.person_linkedin_url}%"))
                .first()
            )
            if contact is None:
                contact = Contact(company_id=company.id, first_name=person["first_name"], last_name=person["last_name"],
                                  title=person["title"], linkedin_url=url, thread_role="engagement_lead",
                                  matched_title_reasoning=lead.qualifier_reason)
                db.add(contact)
            contact.email = person["email"]
            contact.email_source = "prospeo"
            db.commit()
            opportunity, _strategy = _write_opportunity(db, tenant_id, lead, company)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            lead.state = STATE_FAILED
            lead.last_error = f"write rows: {type(e).__name__}: {e}"[:500]
            db.commit()
            continue

        lead.company_id, lead.contact_id, lead.opportunity_id = company.id, contact.id, opportunity.id
        lead.state = STATE_CONTACT_FOUND
        db.commit()
        found += 1
    return {"found": found, "missing": missing, "stopped": None}


# ---------------------------------------------------------------------------------------------
# draft -- the existing message drafter, targeted at this exact person
# ---------------------------------------------------------------------------------------------

def draft_messages(db: Session, tenant_id: int, limit: int = 10, play: str = PLAY) -> dict:
    from app.gtm_os.learning.message_draft import generate_message_draft
    from app.gtm_os.opportunity.opportunity import Opportunity
    from app.gtm_os.strategy.strategy import GtmStrategy
    from app.llm_budget import LlmBudgetExceeded

    leads = (
        db.query(GtmLead)
        .filter(GtmLead.tenant_id == tenant_id, GtmLead.play == play, GtmLead.state == STATE_CONTACT_FOUND)
        .order_by(GtmLead.icp_fit_score.desc())
        .limit(limit)
        .all()
    )
    drafted = 0
    for lead in leads:
        opportunity = db.get(Opportunity, lead.opportunity_id)
        strategy = (
            db.query(GtmStrategy).filter(GtmStrategy.opportunity_id == lead.opportunity_id)
            .order_by(GtmStrategy.id.desc()).first()
        )
        # Aim the draft at the lead's own contact, not whoever else we know at the company.
        others = [c.id for c in db.query(Contact.id).filter(Contact.company_id == lead.company_id, Contact.id != lead.contact_id)]
        try:
            draft = generate_message_draft(db, tenant_id, opportunity, strategy, exclude_contact_ids=others)
        except (SpendBlocked, LlmBudgetExceeded) as e:
            return {"drafted": drafted, "stopped": f"budget: {e}"}
        except Exception as e:  # noqa: BLE001
            db.rollback()
            lead.last_error = f"draft: {type(e).__name__}: {e}"[:500]
            db.commit()
            continue
        lead.message_draft_id = draft.id
        if draft.message_text:
            lead.state = STATE_DRAFTED
            drafted += 1
        else:
            lead.last_error = f"draft not ready: {draft.status} {draft.missing_information}"[:500]
        db.commit()
    return {"drafted": drafted, "stopped": None}


# ---------------------------------------------------------------------------------------------
# sense -- existing adapters, budget-checked per call
# ---------------------------------------------------------------------------------------------

def sense(db: Session, tenant_id: int, posts_to_harvest: int = 3, commenters_per_post: int = 20) -> dict:
    from app.apify_budget_guard import STATUS_ALLOWED, check_apify_budget
    from app.apify_client import LINKEDIN_ENGAGEMENT_COST_PER_ENGAGER_USD, LINKEDIN_POST_COST_PER_POST_USD
    from app.gtm_os.intelligence.engagement_intent import select_relevant_post_urls
    from app.gtm_os.intelligence.linkedin_search_config import get_linkedin_search_config
    from app.gtm_os.intelligence.sensing import sense_linkedin_post_engagement, sense_linkedin_post_search

    config = get_linkedin_search_config(db, tenant_id)
    worst_case = (config.get("max_phrases_per_cycle") or 0) * (config.get("posts_per_phrase") or 0) * LINKEDIN_POST_COST_PER_POST_USD
    budget = check_apify_budget(db, tenant_id, worst_case, operation=f"{PLAY}:post_search")
    if budget["status"] != STATUS_ALLOWED:
        return {"posts": 0, "posts_harvested": 0, "stopped": budget["reason"]}

    posts = sense_linkedin_post_search(db, tenant_id)
    harvested = 0
    for post_url in select_relevant_post_urls(posts, posts_to_harvest):
        budget = check_apify_budget(db, tenant_id, commenters_per_post * LINKEDIN_ENGAGEMENT_COST_PER_ENGAGER_USD,
                                    operation=f"{PLAY}:commenters")
        if budget["status"] != STATUS_ALLOWED:
            return {"posts": len(posts), "posts_harvested": harvested, "stopped": budget["reason"]}
        sense_linkedin_post_engagement(db, tenant_id, [post_url], max_results=commenters_per_post)
        harvested += 1
    return {"posts": len(posts), "posts_harvested": harvested, "stopped": None}


# ---------------------------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------------------------

def run_play_a(db: Session, tenant_id: int, run_cap_usd: float | None = None, do_sense: bool = True,
               qualify_limit: int = 20, contact_limit: int = 5, draft_limit: int = 5) -> dict:
    """One pass: sense -> ingest -> qualify -> contact -> draft. Each step works on the next N
    leads in its state; nothing loops back. Everything paid is reserved against the tenant's
    combined daily cap and `run_cap_usd` (defaults to the config's spend.run_cap_usd)."""
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
            result["sense"] = sense(db, tenant_id)
        result["ingest"] = ingest_new_signals(db, tenant_id)
        result["qualify"] = qualify_leads(db, tenant_id, limit=qualify_limit)
        result["contact"] = find_contacts(db, tenant_id, limit=contact_limit)
        result["draft"] = draft_messages(db, tenant_id, limit=draft_limit)
        result["spent_usd"] = round(scope.spent_usd, 4)
    result["run_cap_usd"] = run_cap_usd
    return result
