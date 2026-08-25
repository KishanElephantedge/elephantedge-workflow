import base64
import csv
import io
import json
from collections import Counter
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import func, or_

from app.cache import active_keys, bump_batch_version, cache_get, cache_set, get_batch_version, mark_active
from app.claude_client import ClaudeError, call_claude_messages
from app.db.models import AutonomousRun, Batch, CalendarBooking, CampaignEvent, CampaignPush, ChatConversation, ChatMessage, Company, Contact, Credential, DailyReview, LinkedinMonitorProfile, LinkedinMonitorSignal, Notification, Parameter, PartnerCompanyRecommendation, PartnerRecommendationMessage, PersonalizedMessage, ReverseDiscoveryCandidate, ReviewComment, Score
from app.notifications import delete_expired_notifications
from app.google_calendar_client import GoogleCalendarError
from app.phases.hiring_signal import has_qualifying_hiring_signal
from app.db.session import get_db
from app.deepline_client import DeeplineError, get_credit_balance_usd
from app.heyreach_client import HeyReachError
from app.hubspot_client import HubSpotError
from app.outreach.selector import get_outreach_channel
from app.phases.autonomous_orchestrator import cancel_run, get_autonomous_discovery_source, get_autonomous_message_style, get_autonomous_schedule_utc, get_daily_budget_usd, get_daily_company_cap, is_autonomous_enabled, recover_run_to_awaiting_approval, resend_approval_notification, resume_pending_approvals, run_daily_autonomous_cycle
from app.phases.buying_signal import run_buying_signal_check
from app.phases.campaign_execution import run_campaign_execution
from app.phases.decision_maker import run_decision_maker_id
from app.phases.discovery import run_discovery
from app.phases.jd_first_discovery import run_jd_first_discovery
from app.phases.jobo_discovery import run_jobo_discovery
from app.phases.apify_discovery import run_apify_discovery
from app.apify_client import ApifyError
from app.jobo_client import JoboError
from app.phases.hubspot_sync import sync_to_hubspot
from app.phases.calendar_sync import sync_calendar_bookings
from app.phases.personalized_outreach import generate_personalized_message
from app.phases.scoring import run_scoring
from app.phases.tech_stack import run_tech_stack_check
from app.salesrobot_client import SalesRobotError, add_single_prospect, get_campaign_prospects, list_campaigns, send_message_to_prospect
from app.smartlead_client import SmartleadError
from app.smartlead_client import add_lead as smartlead_add_lead
from app.smartlead_client import get_campaign_analytics as smartlead_get_campaign_analytics
from app.smartlead_client import list_campaigns as smartlead_list_campaigns
from app.smartlead_client import get_campaign_leads as smartlead_get_campaign_leads

router = APIRouter()

# Elephant Edge's tenant_id in the shared tenants table (see synefi's Tenant row for
# slug="elephant-edge"). Hardcoded here deliberately: this backend only ever serves this one
# tenant -- it is not a second multi-tenant backend, it IS Elephant Edge's dedicated backend.
# If Elephant Edge's tenant_id ever changes, update this constant.
ELEPHANT_EDGE_TENANT_ID = 2


def _is_company_qualified(company: Company) -> bool:
    """"Qualified" has to mean more than has_qualifying_hiring_signal alone -- found live
    (2026-08-06) that companies discovered through the older company-first flow or manual/
    Jobo research (e.g. Rogo: score 75, tier "jobo_manual") pass their OWN real qualification
    gate and get a real decision-maker searched for them, but never touch hiring_signal_role
    at all, so the newer flows' gate alone undercounted real qualified companies. A company
    counts as qualified if EITHER gate says so."""
    if has_qualifying_hiring_signal(company):
        return True
    return bool(company.score and company.score.tier and company.score.tier != "excluded")


# ---- Batches ----
# ICP (Phase 2) is confirmed -- see phase2-icp.md. Discovery/Qualification/Signal/Scoring
# endpoints below implement that confirmed spec; Phase 4 (Qualification) has no endpoint of its
# own, per its own finding (absorbed into Discovery's query filters and Phase 6's thread_role).

@router.post("/batches")
def create_batch(name: str, source: str = "deepline", db: Session = Depends(get_db)):
    if source not in ("deepline", "jobo"):
        raise HTTPException(status_code=400, detail="source must be 'deepline' or 'jobo'")
    batch = Batch(tenant_id=ELEPHANT_EDGE_TENANT_ID, name=name, source=source)
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return {"id": batch.id, "name": batch.name, "source": batch.source, "status": batch.status}


def _normalize_company_source(raw: str | None) -> str:
    """Company.source is a free-text provenance string (e.g. "jd_first:theirstack_job_search+
    crustdata_v3_company_identify", "apify:fantastic-jobs_advanced-linkedin-job-search-api",
    "jobo", "deepline", "crustdata_companydb_search", or null for a manual import). Batch.source
    itself is only ever "deepline" or "jobo" (a dashboard-tab grouping, not a real pipeline
    label -- jd_first and apify are both tagged "deepline" at the batch level, see
    autonomous_orchestrator.py). The real "where did this come from" answer lives per-company,
    not per-batch -- this collapses that into a short display label."""
    if not raw:
        return "manual"
    if raw.startswith("jd_first"):
        return "jd_first"
    if raw.startswith("apify"):
        return "apify"
    if raw.startswith("jobo") or raw == "jobo":
        return "jobo"
    if raw.startswith("manual_entry"):
        return "manual"
    return "deepline"  # crustdata_companydb_search, "deepline", or any other legacy value


@router.get("/batches")
def list_batches(page: int = 1, page_size: int = 10, source: str | None = None, db: Session = Depends(get_db)):
    """`source` here filters on the real, per-company-derived label (jd_first/jobo/apify/
    deepline/manual) -- NOT Batch.source, which is too coarse to answer "where did this batch's
    companies actually come from" (see _normalize_company_source)."""
    batches = (
        db.query(Batch)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .order_by(Batch.created_at.desc())
        .all()
    )

    companies = (
        db.query(Company.batch_id, Company.source)
        .filter(Company.batch_id.in_([b.id for b in batches]))
        .all()
    )
    sources_by_batch: dict[int, Counter] = {}
    for batch_id, comp_source in companies:
        bucket = sources_by_batch.setdefault(batch_id, Counter())
        bucket[_normalize_company_source(comp_source)] += 1

    rows = []
    for b in batches:
        bucket = sources_by_batch.get(b.id)
        dominant_source = bucket.most_common(1)[0][0] if bucket else "manual"
        rows.append({
            "id": b.id,
            "name": b.name,
            "source": dominant_source,
            "created_at": b.created_at,
            "current_phase": b.current_phase,
            "status": b.status,
            "company_count": sum(bucket.values()) if bucket else 0,
        })

    if source:
        rows = [r for r in rows if r["source"] == source]

    total = len(rows)
    page = max(page, 1)
    page_size = max(1, min(page_size, 100))
    page_rows = rows[(page - 1) * page_size: page * page_size]

    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
        "batches": page_rows,
    }


CACHE_TTL_SECONDS = 600  # generous -- the background refresher (main.py) keeps active
# batches' cache fresh well before this expires; it's just a ceiling for abandoned pages.


def _build_batch_payload(batch_id: int, page: int, page_size: int, db: Session) -> dict | None:
    """The actual DB work behind GET /batches/{id} -- factored out so both the request
    handler (cache miss / ?fresh=true) and the background refresher (main.py) can produce
    the exact same shape without duplicating the query logic."""
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        return None

    # Summary counts computed over the WHOLE batch via SQL aggregates, independent of which
    # page is loaded -- the step-progress flags (has scoring run? has a decision maker been
    # found for anyone?) need to see the whole batch, not just whatever page happens to be
    # showing. See synefi/app/routes/api.py's get_batch for the same reasoning.
    total_companies = db.query(func.count(Company.id)).filter(Company.batch_id == batch.id).scalar()
    scored_count = (
        db.query(func.count(Score.id))
        .join(Company, Company.id == Score.company_id)
        .filter(Company.batch_id == batch.id)
        .scalar()
    )
    contacts_count = (
        db.query(func.count(Contact.id))
        .join(Company, Company.id == Contact.company_id)
        .filter(Company.batch_id == batch.id)
        .scalar()
    )

    companies = (
        db.query(Company)
        .filter(Company.batch_id == batch.id)
        .options(
            selectinload(Company.score),
            selectinload(Company.contacts).selectinload(Contact.personalized_message),
        )
        .order_by(Company.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "page": page,
        "page_size": page_size,
        "total_companies": total_companies,
        "summary": {"scored_count": scored_count, "contacts_count": contacts_count},
        "id": batch.id,
        "name": batch.name,
        "source": batch.source,
        "current_phase": batch.current_phase,
        "status": batch.status,
        "companies": [
            {
                "id": c.id,
                "name": c.name,
                "domain": c.domain,
                "source": c.source,
                "linkedin_url": c.linkedin_url,
                "contact_count": len(c.contacts),
                "decision_maker_searched": c.decision_maker_searched_at is not None,
                "active_head_of_sales_posting": c.active_head_of_sales_posting,
                "score": c.score.total_score if c.score else None,
                "tier": c.score.tier if c.score else None,
                "score_breakdown": c.score.breakdown if c.score else None,
                "sales_headcount_percent": c.sales_headcount_percent,
                "marketing_headcount_percent": c.marketing_headcount_percent,
                "geography_tier": c.geography_tier,
                "industry_classification": c.industry_classification,
                "hiring_signal_role": c.hiring_signal_role,
                "hiring_signal_hire_type": c.hiring_signal_hire_type,
                "hiring_signal_strength": c.hiring_signal_strength,
                "hiring_signal_reasoning": c.hiring_signal_reasoning,
                "product_fit_jd_categories": c.product_fit_jd_categories,
                "has_outbound_tooling": c.has_outbound_tooling,
                "has_ai_sdr_tool": c.has_ai_sdr_tool,
                "team_fit_tier": c.team_fit_tier,
                "team_fit_reasoning": c.team_fit_reasoning,
                "contacts": [
                    {
                        "id": ct.id,
                        "first_name": ct.first_name,
                        "last_name": ct.last_name,
                        "title": ct.title,
                        "linkedin_url": ct.linkedin_url,
                        "message_status": ct.personalized_message.status if ct.personalized_message else None,
                        "excluded_from_push": ct.excluded_from_push,
                    }
                    for ct in c.contacts
                ],
            }
            for c in companies
        ],
    }


def _batch_cache_key(batch_id: int, page: int, page_size: int) -> str:
    version = get_batch_version(batch_id)
    return f"batch:{batch_id}:v{version}:p{page}:s{page_size}"


def refresh_active_batch_caches(db: Session):
    """Runs every few minutes (see main.py's scheduler) -- re-fetches and re-caches every
    batch page anyone actually loaded recently, so a real user's next click almost always
    hits a warm cache instead of racing a cold DB query. Cheap: only touches pages someone
    looked at in the last 30 minutes (see app/cache.py's active_keys)."""
    for logical_key in active_keys():
        try:
            batch_id_str, page_str, page_size_str = logical_key.split(":")
            batch_id, page, page_size = int(batch_id_str), int(page_str), int(page_size_str)
        except ValueError:
            continue
        payload = _build_batch_payload(batch_id, page, page_size, db)
        if payload is None:
            continue
        cache_set(_batch_cache_key(batch_id, page, page_size), payload, CACHE_TTL_SECONDS)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int, page: int = 1, page_size: int = 50, fresh: bool = False, db: Session = Depends(get_db)):
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    mark_active(f"{batch_id}:{page}:{page_size}")

    if not fresh:
        cached = cache_get(_batch_cache_key(batch_id, page, page_size))
        if cached is not None:
            return cached

    payload = _build_batch_payload(batch_id, page, page_size, db)
    if payload is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    cache_set(_batch_cache_key(batch_id, page, page_size), payload, CACHE_TTL_SECONDS)
    return payload


# ---- Manual company import (hand-picked companies, Discovery/Qualification skipped) ----
# For a batch like the 10 companies the team lead sent directly -- these are already
# hand-vetted, so Phase 2 (Discovery) and Phase 3 (Qualification) don't apply. Companies
# are seeded straight in, ready for Phase 6 (Decision Maker) to run against them.

class CompanyImport(BaseModel):
    name: str
    domain: str


@router.post("/batches/{batch_id}/companies/import")
def import_companies(batch_id: int, companies: list[CompanyImport], db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    created = []
    for c in companies:
        company = Company(batch_id=batch_id, name=c.name, domain=c.domain)
        db.add(company)
        created.append(company)
    batch.current_phase = "companies_imported"
    db.commit()
    bump_batch_version(batch_id)
    for c in created:
        db.refresh(c)
    return {"imported": len(created), "companies": [{"id": c.id, "name": c.name, "domain": c.domain} for c in created]}


class ContactImport(BaseModel):
    first_name: str
    last_name: str | None = None
    title: str | None = None
    linkedin_url: str | None = None


@router.post("/companies/{company_id}/contacts/import")
def import_contact(company_id: int, contact: ContactImport, db: Session = Depends(get_db)):
    """Manually add a contact to a company, bypassing Decision Maker search entirely -- for
    cases like testing the pipeline on a known contact (e.g. our own team) where an automated
    search isn't the point."""
    company = (
        db.query(Company)
        .join(Batch)
        .filter(Company.id == company_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    new_contact = Contact(
        company_id=company_id,
        first_name=contact.first_name,
        last_name=contact.last_name,
        title=contact.title,
        linkedin_url=contact.linkedin_url,
        matched_title_reasoning="Manually added, not found via Decision Maker search",
    )
    db.add(new_contact)
    company.decision_maker_searched_at = datetime.utcnow()
    db.commit()
    db.refresh(new_contact)
    bump_batch_version(company.batch_id)
    return {"id": new_contact.id, "first_name": new_contact.first_name, "last_name": new_contact.last_name}


# ---- Phase 3: Company Discovery ----

def _require_batch_source(batch: Batch, expected: str) -> None:
    """Hard guard so a Deepline-only phase can never run against a Jobo batch and vice
    versa -- the two pipelines are fully independent (separate credit systems, separate
    company/decision-maker logic), and running the wrong one against a batch would silently
    mix sources or double-charge the wrong credit account."""
    if batch.source != expected:
        raise HTTPException(
            status_code=400,
            detail=f"This batch's source is {batch.source!r}, not {expected!r} -- this phase can't run against it.",
        )


@router.post("/batches/{batch_id}/phases/discovery")
def execute_discovery(batch_id: int, target: int = 10, db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    _require_batch_source(batch, "deepline")
    result = run_discovery(batch_id, db, ELEPHANT_EDGE_TENANT_ID, target=target)
    batch.current_phase = "discovery_done"
    db.commit()
    bump_batch_version(batch_id)
    return result


# ---- JD-First Discovery (flow inversion -- job postings first, firmographics second) ----

@router.post("/batches/{batch_id}/phases/discovery-jd-first")
def execute_jd_first_discovery(batch_id: int, target: int = 10, db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    _require_batch_source(batch, "deepline")
    result = run_jd_first_discovery(batch_id, db, ELEPHANT_EDGE_TENANT_ID, target=target)
    batch.current_phase = "discovery_done"
    db.commit()
    bump_batch_version(batch_id)
    return result


# ---- Apify Discovery -- LinkedIn Jobs Scraper, real second discovery source validated live
# 2026-08-05/06 (8/8 and 5/5 real qualifying companies vs. jd_first's 2/5 the same week) ----

@router.post("/batches/{batch_id}/phases/discovery-apify")
def execute_apify_discovery(batch_id: int, target: int = 5, db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    _require_batch_source(batch, "deepline")
    try:
        result = run_apify_discovery(batch_id, db, ELEPHANT_EDGE_TENANT_ID, target=target)
    except ApifyError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if result.get("api_error"):
        raise HTTPException(status_code=502, detail=result["api_error"])
    batch.current_phase = "discovery_done"
    db.commit()
    bump_batch_version(batch_id)
    return result


# ---- Jobo Discovery -- fully independent pipeline, own credit system, own gates ----

@router.post("/batches/{batch_id}/phases/jobo-discovery")
def execute_jobo_discovery(batch_id: int, target: int = 5, budget_usd: float = 1.5, db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    _require_batch_source(batch, "jobo")
    try:
        result = run_jobo_discovery(batch_id, db, ELEPHANT_EDGE_TENANT_ID, target=target, budget_usd=budget_usd)
    except JoboError as e:
        raise HTTPException(status_code=502, detail=str(e))
    batch.current_phase = "discovery_done"
    db.commit()
    bump_batch_version(batch_id)
    return result


# ---- Phase 5/8: Buying Signal Intelligence ----

@router.post("/batches/{batch_id}/phases/buying-signal")
def execute_buying_signal_check(batch_id: int, db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    _require_batch_source(batch, "deepline")
    result = run_buying_signal_check(batch_id, db)
    batch.current_phase = "buying_signal_done"
    db.commit()
    bump_batch_version(batch_id)
    return result


# ---- Tech Stack / Outbound Maturity check (Signal Framework v2) ----

@router.post("/batches/{batch_id}/phases/tech-stack")
def execute_tech_stack_check(batch_id: int, db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    _require_batch_source(batch, "deepline")
    result = run_tech_stack_check(batch_id, db)
    batch.current_phase = "tech_stack_done"
    db.commit()
    bump_batch_version(batch_id)
    return result


# ---- Phase 9: Opportunity Scoring ----

@router.post("/batches/{batch_id}/phases/scoring")
def execute_scoring(batch_id: int, db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    _require_batch_source(batch, "deepline")
    result = run_scoring(batch_id, db)
    batch.current_phase = "scoring_done"
    db.commit()
    bump_batch_version(batch_id)
    return result


# ---- Phase 6: Decision Maker Intelligence ----

@router.post("/batches/{batch_id}/phases/decision-maker")
def execute_decision_maker_id(batch_id: int, retry_company_ids: list[int] | None = Body(default=None), db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    _require_batch_source(batch, "deepline")
    result = run_decision_maker_id(batch_id, db, ELEPHANT_EDGE_TENANT_ID, retry_company_ids=retry_company_ids)
    batch.current_phase = "decision_maker_done"
    db.commit()
    bump_batch_version(batch_id)
    return result


@router.post("/batches/{batch_id}/hubspot-backfill")
def backfill_hubspot_sync(batch_id: int, db: Session = Depends(get_db)):
    """Syncs any contact in this batch found before the HubSpot integration existed and
    never got pushed. Reuses the same sync_to_hubspot logic (dedup-by-domain, per-contact
    tracking) as the live Decision Maker phase -- not a separate one-off path."""
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    contacts = (
        db.query(Contact)
        .join(Company)
        .filter(Company.batch_id == batch_id)
        .filter(Contact.hubspot_synced_at.is_(None))
        .all()
    )
    synced = 0
    errors = []
    for contact in contacts:
        try:
            sync_to_hubspot(contact.company, contact, db, ELEPHANT_EDGE_TENANT_ID)
            synced += 1
        except HubSpotError as e:
            errors.append(f"{contact.company.name}: {e}")

    return {"contacts_checked": len(contacts), "synced": synced, "errors": errors}


# ---- Phase 13: Personalized Outreach Message Synthesis ----
# Company research (free scrape + Claude) -> contact/LinkedIn research (Aviato + Claude) ->
# fit analysis (Claude) -> message synthesis (Claude). Every module's raw output is returned
# so a human can review/edit any stage, not just the final message.

@router.post("/contacts/{contact_id}/generate-message")
def generate_message(contact_id: int, style: str = "pitch", db: Session = Depends(get_db)):
    """style: "pitch" (default, pitches our value proposition) or "curiosity" (the lead's
    real, tested no-pitch alternative -- see personalized_outreach.py)."""
    contact = (
        db.query(Contact)
        .join(Company)
        .filter(Contact.id == contact_id)
        .filter(Company.batch_id.in_(db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)))
        .first()
    )
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    pm = generate_personalized_message(contact_id, db, ELEPHANT_EDGE_TENANT_ID, style=style)
    bump_batch_version(contact.company.batch_id)
    return {
        "contact_id": contact_id,
        "status": pm.status,
        "company_research": pm.company_research,
        "contact_research": pm.contact_research,
        "fit_analysis": pm.fit_analysis,
        "generated_message": pm.generated_message,
        "contact_email": contact.email,
        "contact_email_source": contact.email_source,
        "email_subject": pm.email_subject,
        "email_body": pm.email_body,
        "error_message": pm.error_message,
        "generated_at": pm.generated_at,
    }


@router.get("/contacts/{contact_id}/message")
def get_message(contact_id: int, db: Session = Depends(get_db)):
    contact = (
        db.query(Contact)
        .join(Company)
        .filter(Contact.id == contact_id)
        .filter(Company.batch_id.in_(db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)))
        .first()
    )
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    pm = contact.personalized_message
    if not pm:
        return {"contact_id": contact_id, "status": "not_generated"}
    return {
        "contact_id": contact_id,
        "status": pm.status,
        "company_research": pm.company_research,
        "contact_research": pm.contact_research,
        "fit_analysis": pm.fit_analysis,
        "generated_message": pm.generated_message,
        "contact_email": contact.email,
        "contact_email_source": contact.email_source,
        "email_subject": pm.email_subject,
        "email_body": pm.email_body,
        "error_message": pm.error_message,
        "generated_at": pm.generated_at,
    }


class MessageEdit(BaseModel):
    generated_message: str | None = None
    email_subject: str | None = None
    email_body: str | None = None
    status: str | None = None


@router.post("/contacts/{contact_id}/message/edit")
def edit_message(contact_id: int, body: MessageEdit, db: Session = Depends(get_db)):
    contact = (
        db.query(Contact)
        .join(Company)
        .filter(Contact.id == contact_id)
        .filter(Company.batch_id.in_(db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)))
        .first()
    )
    if not contact or not contact.personalized_message:
        raise HTTPException(status_code=404, detail="No generated message for this contact yet")
    pm = contact.personalized_message
    if body.generated_message is not None:
        pm.generated_message = body.generated_message
    if body.email_subject is not None:
        pm.email_subject = body.email_subject
    if body.email_body is not None:
        pm.email_body = body.email_body
    if body.status is not None:
        if body.status not in ("draft", "approved", "rejected"):
            raise HTTPException(status_code=400, detail="status must be draft, approved, or rejected")
        pm.status = body.status
    db.commit()
    bump_batch_version(contact.company.batch_id)
    return {
        "contact_id": contact_id,
        "status": pm.status,
        "generated_message": pm.generated_message,
        "contact_email": contact.email,
        "contact_email_source": contact.email_source,
        "email_subject": pm.email_subject,
        "email_body": pm.email_body,
    }


@router.post("/contacts/{contact_id}/exclude-from-push")
def set_contact_excluded_from_push(contact_id: int, excluded: bool, db: Session = Depends(get_db)):
    """Separate from PersonalizedMessage.status -- this controls whether the contact gets
    pushed to the outreach campaign at all, not just whether a message gets attached. Can be
    set/unset any time before the approval window resumes (run_campaign_execution checks it
    at push time)."""
    contact = (
        db.query(Contact)
        .join(Company)
        .filter(Contact.id == contact_id)
        .filter(Company.batch_id.in_(db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)))
        .first()
    )
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    contact.excluded_from_push = excluded
    db.commit()
    bump_batch_version(contact.company.batch_id)
    return {"contact_id": contact_id, "excluded_from_push": contact.excluded_from_push}


# ---- SalesRobot outcome webhook ----
# SalesRobot pushes events to us (confirmed live -- no pull/status API exists); this receives
# them. The secret path segment is the only protection available, since SalesRobot's webhook
# config has no signing-secret field. The exact payload shape is unknown until a real event
# arrives, so this stores the FULL raw body unconditionally and only best-effort matches it to
# a known contact by scanning for a linkedin.com URL anywhere in the payload -- refine the
# matching logic once a real payload has been inspected, but never lose data in the meantime.
SALESROBOT_WEBHOOK_SECRET = "xD9rl4qgzeVc9vaZAkJr8vdDXxs0xX3u"


def _find_linkedin_url_in_payload(obj) -> str | None:
    if isinstance(obj, str):
        return obj if "linkedin.com" in obj else None
    if isinstance(obj, dict):
        for value in obj.values():
            found = _find_linkedin_url_in_payload(value)
            if found:
                return found
    if isinstance(obj, list):
        for item in obj:
            found = _find_linkedin_url_in_payload(item)
            if found:
                return found
    return None


def _get_smartlead_campaign_id(db: Session) -> int:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Parameter.key == "smartlead_campaign_id")
        .first()
    )
    if not param or not param.value:
        raise HTTPException(status_code=400, detail="smartlead_campaign_id parameter is not set")
    value = param.value
    return int(value.get("value") if isinstance(value, dict) else value)


def _get_salesrobot_linkedin_account_uuid(db: Session) -> str:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Parameter.key == "salesrobot_linkedin_account_uuid")
        .first()
    )
    if not param or not param.value:
        raise HTTPException(status_code=400, detail="salesrobot_linkedin_account_uuid parameter is not set")
    return param.value.get("value") if isinstance(param.value, dict) else param.value


def _get_our_campaign_uuids(db: Session) -> list[str]:
    """The connected LinkedIn account is shared -- found live that GET /campaigns returns
    EVERY campaign on that account, including ones that belong to other work entirely (same
    reason the SalesRobot webhook is deliberately scoped to named campaigns only, not "All
    Campaigns"). This is an explicit allowlist of campaign UUIDs that are actually ours, so we
    never display someone else's campaign data. Falls back to just the configured production
    campaign_uuid if the allowlist itself was never set."""
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Parameter.key == "salesrobot_our_campaign_uuids")
        .first()
    )
    if param and param.value and isinstance(param.value, dict) and param.value.get("uuids"):
        return param.value["uuids"]
    prod_param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Parameter.key == "salesrobot_campaign_uuid")
        .first()
    )
    if prod_param and prod_param.value:
        val = prod_param.value.get("value") if isinstance(prod_param.value, dict) else prod_param.value
        return [val] if val else []
    return []


# Moved to app/linkedin_utils.py (Step 11A) so the gtm_os intelligence layer can reuse it
# without importing from app/routes/* -- kept under this module's original name here so every
# existing call site below is unchanged.
from app.linkedin_utils import normalize_linkedin_url as _normalize_linkedin_url  # noqa: E402


@router.get("/salesrobot/campaigns")
def get_salesrobot_campaigns(db: Session = Depends(get_db)):
    """Level 1 of the Campaigns -> Leads -> Activity view -- real campaign list per
    SalesRobot's own docs.salesrobot.co/reference/getcampaigns, filtered to only OUR
    campaigns (see _get_our_campaign_uuids)."""
    account_uuid = _get_salesrobot_linkedin_account_uuid(db)
    our_uuids = set(_get_our_campaign_uuids(db))
    try:
        result = list_campaigns(account_uuid, db, ELEPHANT_EDGE_TENANT_ID)
    except SalesRobotError as e:
        raise HTTPException(status_code=502, detail=str(e))
    campaigns = result.get("data", {}).get("data", [])
    return [c for c in campaigns if c.get("uuid") in our_uuids]


@router.get("/salesrobot/campaigns/{campaign_uuid}/leads")
def get_salesrobot_campaign_leads(campaign_uuid: str, db: Session = Depends(get_db)):
    """Level 2 -- real per-prospect status (lastActivity, status) for a campaign, per
    docs.salesrobot.co/reference/getprospectsforcampaign. Trimmed to the fields the UI
    actually shows; the full raw record is still available for the Activity drill-down."""
    account_uuid = _get_salesrobot_linkedin_account_uuid(db)
    try:
        result = get_campaign_prospects(campaign_uuid, account_uuid, db, ELEPHANT_EDGE_TENANT_ID)
    except SalesRobotError as e:
        raise HTTPException(status_code=502, detail=str(e))
    prospects = result.get("data", {}).get("data", [])
    return [
        {
            "prospectUuid": p.get("prospectUuid"),
            "fullName": p.get("fullName"),
            "jobTitle": p.get("jobTitle"),
            "companyName": p.get("companyName"),
            "profileUrl": p.get("profileUrl"),
            "lastActivity": p.get("lastActivity"),
            "status": p.get("status"),
            "raw": p,
        }
        for p in prospects
    ]


@router.get("/salesrobot/leads/activity")
def get_lead_activity(profile_url: str, db: Session = Depends(get_db)):
    """Level 3 -- historical events for one lead, matched by scanning received webhook
    payloads for their LinkedIn profile URL (same best-effort approach as the webhook
    receiver's own contact matching). The live status snapshot (lastActivity/status) comes
    from the leads endpoint above; this is the timestamped history on top of that."""
    profile_key = profile_url.rstrip("/").lower()
    events = db.query(CampaignEvent).order_by(CampaignEvent.received_at.desc()).limit(500).all()
    matching = [e for e in events if profile_key in json.dumps(e.raw_payload).lower()]
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "received_at": e.received_at,
            "raw_payload": e.raw_payload,
        }
        for e in matching
    ]


def _fetch_our_salesrobot_prospects(db: Session) -> dict[str, dict]:
    """Pulls real prospect status from every one of OUR campaigns (not the whole account --
    see _get_our_campaign_uuids) and returns a dict keyed by normalized LinkedIn URL. Used to
    enrich our own DB-driven lead list with live SalesRobot status, rather than the other way
    around -- that way the lead list still works (just without live status) if SalesRobot is
    briefly unreachable, and includes leads not yet pushed to any campaign at all."""
    try:
        account_uuid = _get_salesrobot_linkedin_account_uuid(db)
    except HTTPException:
        return {}
    by_url: dict[str, dict] = {}
    for campaign_uuid in _get_our_campaign_uuids(db):
        try:
            result = get_campaign_prospects(campaign_uuid, account_uuid, db, ELEPHANT_EDGE_TENANT_ID)
        except SalesRobotError:
            continue
        for p in result.get("data", {}).get("data", []):
            key = _normalize_linkedin_url(p.get("profileUrl"))
            if key:
                by_url[key] = p
    return by_url


@router.get("/companies")
def list_companies(page: int = 1, page_size: int = 25, search: str = "", qualified: str = "", account_filter: str = "", db: Session = Depends(get_db)):
    """Cross-batch company list -- previously the only way to see companies at all was per-
    batch (BatchDetail), with no single "everything we've researched" view. `qualified`
    ("true"/"false") filters by the exact same has_qualifying_hiring_signal check that's the
    real production gate before Decision Maker ever runs (see hiring_signal.py), so this
    matches production behavior rather than an approximated definition. That check has no SQL
    column of its own (it's role-is-set OR product-fit-categories-is-set), so it's applied
    Python-side after a bounded DB fetch -- acceptable at today's real company volume (low
    thousands), would need a computed/indexed column if that grows an order of magnitude.

    `account_filter` (V2 Jobs-to-Be-Done wiring, 2026-08-19): "hot_leads" | "no_contact" |
    "missing_email" -- the SAME real conditions app/gtm_os/jobs/jobs_to_be_done.py already uses
    for those exact categories, reused verbatim (not re-derived) so a Jobs page count and its
    "View all"/action link always agree on what they mean. Added because the Jobs page's CTAs
    used to link to this same route unfiltered, which meant "View all 99" actually showed all
    706 companies -- a real UX gap, not a display bug, fixed at the smallest necessary layer
    (one query param here) rather than a new page."""
    query = (
        db.query(Company)
        .join(Batch)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
    )
    if search.strip():
        like = f"%{search.strip()}%"
        query = query.filter(or_(Company.name.ilike(like), Company.domain.ilike(like), Company.industry.ilike(like)))

    companies = query.order_by(Company.created_at.desc()).all()
    # Hot leads float to the top of this same list -- no separate tab/filter (per explicit
    # instruction). Stable sort keeps created_at-desc ordering within each group.
    companies.sort(key=lambda c: not c.hot_lead)

    if qualified == "true":
        companies = [c for c in companies if _is_company_qualified(c)]
    elif qualified == "false":
        companies = [c for c in companies if not _is_company_qualified(c)]

    if account_filter == "hot_leads":
        companies = [c for c in companies if c.hot_lead]
    elif account_filter in ("no_contact", "missing_email"):
        # Same OUTER company set as jobs_to_be_done.py's _contacts_to_find() -- confirmed live
        # (2026-08-19) that omitting this decision_maker_searched_at gate over-counted
        # "missing_email" by 25 companies (95 vs. the real 70): some Contact rows exist for
        # companies whose decision-maker search was never actually run, which
        # _contacts_to_find() correctly excludes and this filter must too, to keep the Jobs
        # page's count and this filtered list in agreement.
        companies = [c for c in companies if c.decision_maker_searched_at is not None]
        company_ids = [c.id for c in companies]
        ids_with_contacts = {row[0] for row in db.query(Contact.company_id).filter(Contact.company_id.in_(company_ids)).distinct().all()} if company_ids else set()
        if account_filter == "no_contact":
            companies = [c for c in companies if c.id not in ids_with_contacts]
        else:
            ids_missing_email = {row[0] for row in db.query(Contact.company_id).filter(Contact.company_id.in_(company_ids), Contact.email_source.is_(None)).distinct().all()} if company_ids else set()
            companies = [c for c in companies if c.id in ids_missing_email]

    total = len(companies)
    page = max(page, 1)
    page_size = max(1, min(page_size, 100))
    page_items = companies[(page - 1) * page_size: page * page_size]

    # V2 Accounts list enrichment (2026-08-19) -- real GTM-OS state per row (account_status,
    # signal_count, opportunity_count), scoped to only this page's companies via
    # list_account_states()'s bulk-query pattern (six fixed queries, not one per company). See
    # that function's own docstring for why this is safe at list-page cost.
    from app.gtm_os.account_agent.account_agent import list_account_states

    states_by_id = list_account_states(db, ELEPHANT_EDGE_TENANT_ID, [c.id for c in page_items])

    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
        "companies": [
            {
                "id": c.id,
                "name": c.name,
                "domain": c.domain,
                "industry": c.industry,
                "source": c.source,
                "batch_id": c.batch_id,
                "qualified": _is_company_qualified(c),
                "hiring_signal_role": c.hiring_signal_role,
                "hiring_signal_strength": c.hiring_signal_strength,
                "team_fit_tier": c.team_fit_tier,
                "linkedin_url": c.linkedin_url,
                "created_at": c.created_at,
                "hot_lead": c.hot_lead,
                "hot_lead_reasoning": c.hot_lead_reasoning,
                **states_by_id.get(c.id, {"account_status": "insufficient_context", "signal_count": 0, "opportunity_count": 0}),
            }
            for c in page_items
        ],
    }


def _get_smartlead_sent_emails(db: Session) -> set[str]:
    """The provider's own send record, not a local push flag -- email sends have so far only
    gone out via the manual test-push endpoint (no automated campaign_execution wiring yet),
    so there is no DB row to check locally. Mirrors the SalesRobot live-status enrichment
    pattern (_fetch_our_salesrobot_prospects) already used just below."""
    try:
        campaign_id = _get_smartlead_campaign_id(db)
        result = smartlead_get_campaign_leads(campaign_id, db, ELEPHANT_EDGE_TENANT_ID, limit=300)
    except (HTTPException, SmartleadError):
        return set()
    emails = set()
    for row in result.get("data", []):
        email = (row.get("lead") or {}).get("email")
        if email:
            emails.add(email.strip().lower())
    return emails


def _lead_dict(c: Contact, live: dict | None, sent_emails: set[str] | None = None) -> dict:
    pm = c.personalized_message
    push = c.campaign_pushes[-1] if c.campaign_pushes else None
    email_sent = bool(c.email and sent_emails and c.email.strip().lower() in sent_emails)
    return {
        "contact_id": c.id,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "title": c.title,
        "linkedin_url": c.linkedin_url,
        "company_name": c.company.name if c.company else None,
        "company_linkedin_url": c.company.linkedin_url if c.company else None,
        "message_status": pm.status if pm else None,
        "push_status": push.status if push else None,
        "pushed_at": push.pushed_at if push else None,
        "salesrobot_last_activity": live.get("lastActivity") if live else None,
        "salesrobot_status": live.get("status") if live else None,
        "email": c.email,
        "email_status": "sent" if email_sent else None,
    }


@router.get("/leads")
def list_leads(page: int = 1, page_size: int = 25, search: str = "", message_status: str = "", activity: str = "", pipeline_only: bool = True, db: Session = Depends(get_db)):
    """Unified lead list -- our own DB (the source of truth for who we've researched/messaged)
    enriched with live SalesRobot status where a match exists. Deliberately DB-first, not
    SalesRobot-first: a lead we've generated a message for but haven't pushed yet still shows
    up, and the whole endpoint degrades gracefully (just missing live status) if SalesRobot is
    down, rather than failing outright.

    Paginated, and search/status filtering happen at the query level (not "fetch everything,
    filter in Python") -- so SalesRobot enrichment (a handful of real API calls) only ever
    runs for the leads actually being shown on this page, not the whole table, regardless of
    how large it grows.

    `activity` (e.g. "CONNECTED", "REPLIED", "NO_REPLY_YET", or "NOT_SENT") is the one
    exception -- live SalesRobot status has no DB column to filter/paginate on at the SQL
    level, so that path fetches the full matching set once and paginates in Python. Fine at
    today's real lead volume (dozens-low hundreds); would need a synced status column if this
    grows an order of magnitude.

    `pipeline_only` (default True, matches original/Campaign-tab behavior): a raw decision-
    maker match with no message and no push isn't a "lead" for that view. Set False for the
    Overview tab's "Decision-Makers Found" drill-down, which means every Contact regardless
    of pipeline stage."""
    query = (
        db.query(Contact)
        .join(Company)
        .filter(Company.batch_id.in_(db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)))
    )
    if pipeline_only:
        query = query.filter(or_(Contact.personalized_message.has(), Contact.campaign_pushes.any()))
    if search.strip():
        like = f"%{search.strip()}%"
        query = query.filter(or_(
            Contact.first_name.ilike(like),
            Contact.last_name.ilike(like),
            Contact.title.ilike(like),
            Company.name.ilike(like),
        ))
    if message_status:
        query = query.filter(Contact.personalized_message.has(PersonalizedMessage.status == message_status))

    page = max(page, 1)
    page_size = max(1, min(page_size, 100))

    if activity:
        contacts = (
            query
            .options(selectinload(Contact.personalized_message), selectinload(Contact.campaign_pushes), selectinload(Contact.company))
            .order_by(Contact.id.desc())
            .all()
        )
        live_by_url = _fetch_our_salesrobot_prospects(db)
        sent_emails = _get_smartlead_sent_emails(db)
        matched = []
        for c in contacts:
            live = live_by_url.get(_normalize_linkedin_url(c.linkedin_url))
            last_activity = (live.get("lastActivity") if live else None) or ""
            if activity.upper() == "NOT_SENT":
                if not live and not c.campaign_pushes:
                    matched.append((c, live))
            elif activity.upper() == "SENT":
                # Any live SalesRobot status at all means a connection request actually went
                # out -- CONNECTED/REPLIED/NO_REPLY_YET are all "sent", just at different
                # outcomes past that point.
                if live:
                    matched.append((c, live))
            elif last_activity.upper() == activity.upper():
                matched.append((c, live))
        total = len(matched)
        page_slice = matched[(page - 1) * page_size: page * page_size]
        return {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
            "leads": [_lead_dict(c, live, sent_emails) for c, live in page_slice],
        }

    total = query.count()
    contacts = (
        query
        .options(selectinload(Contact.personalized_message), selectinload(Contact.campaign_pushes), selectinload(Contact.company))
        .order_by(Contact.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    live_by_url = _fetch_our_salesrobot_prospects(db)
    sent_emails = _get_smartlead_sent_emails(db)
    leads = [_lead_dict(c, live_by_url.get(_normalize_linkedin_url(c.linkedin_url)), sent_emails) for c in contacts]
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
        "leads": leads,
    }


@router.get("/leads/stats")
def get_leads_stats(db: Session = Depends(get_db)):
    """KPI cards for the lead dashboard. `connections_sent`/accepted/replied come from
    SalesRobot's own per-campaign aggregates -- the real, authoritative confirmation of what
    was actually sent (per explicit direction: our own DB's pushed count undercounts, since it
    misses prospects added directly in SalesRobot outside this app). `emails_sent`/opened/
    replied are the same pattern for the email channel, added 2026-08-12 -- Smartlead's own
    campaign analytics, not our local push-attempt count, so this was genuinely missing full-
    funnel visibility until now (LinkedIn was tracked, email wasn't).

    `researched` here means "contacts with a message drafted" (any status), NOT "companies
    researched" -- a real naming collision with the Overview tab's much larger "Companies
    Researched" figure, which counts every company discovered regardless of whether a
    decision-maker or message exists yet. Kept as its own field since the frontend already
    reads it; UI label says "Messages Drafted", not "Researched"."""
    account_uuid = None
    try:
        account_uuid = _get_salesrobot_linkedin_account_uuid(db)
    except HTTPException:
        pass

    sent = accepted = replied = 0
    if account_uuid:
        our_uuids = set(_get_our_campaign_uuids(db))
        try:
            result = list_campaigns(account_uuid, db, ELEPHANT_EDGE_TENANT_ID)
            for c in result.get("data", {}).get("data", []):
                if c.get("uuid") in our_uuids:
                    sent += c.get("connectionRequestSentCount") or 0
                    accepted += c.get("connectionRequestAcceptedCount") or 0
                    replied += c.get("repliedCount") or 0
        except SalesRobotError:
            pass

    emails_sent = emails_opened = emails_replied = 0
    try:
        smartlead_campaign_id = _get_smartlead_campaign_id(db)
        email_analytics = smartlead_get_campaign_analytics(smartlead_campaign_id, db, ELEPHANT_EDGE_TENANT_ID)
        emails_sent = int(email_analytics.get("sent_count") or 0)
        emails_opened = int(email_analytics.get("unique_open_count") or email_analytics.get("open_count") or 0)
        emails_replied = int(email_analytics.get("reply_count") or 0)
    except (HTTPException, SmartleadError):
        pass

    batch_ids = db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)

    contacts = (
        db.query(Contact)
        .join(Company)
        .filter(Company.batch_id.in_(batch_ids))
        .options(selectinload(Contact.personalized_message))
        .all()
    )
    researched = sum(1 for c in contacts if c.personalized_message)
    approved = sum(1 for c in contacts if c.personalized_message and c.personalized_message.status == "approved")

    return {
        "researched": researched,
        "approved": approved,
        "connections_sent": sent,
        "connections_accepted": accepted,
        "replied": replied,
        "acceptance_rate": round(accepted / sent, 3) if sent else None,
        "reply_rate": round(replied / sent, 3) if sent else None,
        "emails_sent": emails_sent,
        "emails_opened": emails_opened,
        "emails_replied": emails_replied,
        "email_open_rate": round(emails_opened / emails_sent, 3) if emails_sent else None,
        "email_reply_rate": round(emails_replied / emails_sent, 3) if emails_sent else None,
    }


@router.get("/smartlead/campaigns")
def list_smartlead_campaigns_route(db: Session = Depends(get_db)):
    """Email-channel equivalent of GET /salesrobot/campaigns -- the Campaign tab was only
    showing LinkedIn (SalesRobot) campaigns before this; this lets it show Smartlead's real
    campaign list too."""
    try:
        campaigns = smartlead_list_campaigns(db, ELEPHANT_EDGE_TENANT_ID)
    except SmartleadError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return [
        {"id": c.get("id"), "name": c.get("name"), "status": c.get("status"), "created_at": c.get("created_at")}
        for c in campaigns
    ]


@router.get("/smartlead/campaigns/{campaign_id}/leads")
def list_smartlead_campaign_leads_route(campaign_id: int, db: Session = Depends(get_db)):
    """Email-channel equivalent of GET /salesrobot/campaigns/{uuid}/leads -- lets a Smartlead
    campaign be drilled into from the Campaign tab the same way a SalesRobot one already can."""
    try:
        result = smartlead_get_campaign_leads(campaign_id, db, ELEPHANT_EDGE_TENANT_ID, limit=300)
    except SmartleadError as e:
        raise HTTPException(status_code=502, detail=str(e))
    leads = []
    for row in result.get("data", []):
        lead = row.get("lead") or {}
        leads.append({
            "id": lead.get("id"),
            "first_name": lead.get("first_name"),
            "last_name": lead.get("last_name"),
            "email": lead.get("email"),
            "status": row.get("status"),
            "created_at": row.get("created_at"),
            "email_subject": (lead.get("custom_fields") or {}).get("email_subject"),
            "email_body": (lead.get("custom_fields") or {}).get("email_body"),
        })
    return {"total": int(result.get("total_leads") or 0), "leads": leads}


@router.get("/overview/stats")
def get_overview_stats(start_date: str | None = None, end_date: str | None = None, db: Session = Depends(get_db)):
    """The full funnel, not just the outreach half of it -- /leads/stats (above) only covers
    from "message approved" onward. This adds the two stages before it (companies researched,
    companies qualified) and the one stage after replies that nothing else surfaces
    (meetings booked), so the Overview tab can show one real funnel end to end.

    start_date/end_date (YYYY-MM-DD) filter every DB-sourced count by created_at -- the
    SalesRobot-sourced counts (sent/accepted/replied) are that provider's own campaign-level
    aggregates, which have no per-event timestamp to filter by (see /leads/stats's own
    docstring), so a date range only narrows the DB-native stages. Documented in the response
    itself via `date_filtered_stages` so the frontend doesn't have to guess which numbers moved."""
    batch_ids = db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)

    def _parse_date(value: str | None):
        if not value:
            return None
        return datetime.strptime(value, "%Y-%m-%d")

    start_dt = _parse_date(start_date)
    end_dt = _parse_date(end_date)
    if end_dt:
        end_dt = end_dt + timedelta(days=1)  # inclusive of the whole end date

    def _apply_date_filter(query, column):
        if start_dt:
            query = query.filter(column >= start_dt)
        if end_dt:
            query = query.filter(column < end_dt)
        return query

    companies_query = db.query(Company).filter(Company.batch_id.in_(batch_ids))
    companies_query = _apply_date_filter(companies_query, Company.created_at)
    companies = companies_query.all()
    companies_researched = len(companies)
    companies_qualified = sum(1 for c in companies if _is_company_qualified(c))

    contacts_query = db.query(Contact).join(Company).filter(Company.batch_id.in_(batch_ids))
    contacts_query = _apply_date_filter(contacts_query, Contact.created_at)
    decision_makers_found = contacts_query.count()

    bookings_query = db.query(CalendarBooking)
    bookings_query = _apply_date_filter(bookings_query, CalendarBooking.start_time)
    meetings_booked = bookings_query.filter(CalendarBooking.status != "cancelled").count()

    # Reused, not recomputed, so this and the Campaign tab always show the exact same numbers
    # for connections sent/accepted/replied (SalesRobot's own campaign-level aggregates).
    lead_stats = get_leads_stats(db)

    return {
        "companies_researched": companies_researched,
        "companies_qualified": companies_qualified,
        "decision_makers_found": decision_makers_found,
        "messages_approved": lead_stats["approved"],
        "connections_sent": lead_stats["connections_sent"],
        "connections_accepted": lead_stats["connections_accepted"],
        "replied": lead_stats["replied"],
        "meetings_booked": meetings_booked,
        "emails_sent": lead_stats["emails_sent"],
        "emails_opened": lead_stats["emails_opened"],
        "emails_replied": lead_stats["emails_replied"],
        "date_filtered_stages": ["companies_researched", "companies_qualified", "decision_makers_found", "meetings_booked", "connections_sent"],
    }


@router.get("/overview/recent-activity")
def get_overview_recent_activity(limit: int = 8, db: Session = Depends(get_db)):
    """A real activity feed for the Overview tab -- three genuine event types merged and
    sorted by real timestamp, not a synthetic log. No new table: batches (discovery
    completing), approved messages, and pushed connections are all already timestamped."""
    batch_ids = db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
    items = []

    recent_batches = (
        db.query(Batch)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .order_by(Batch.created_at.desc())
        .limit(limit)
        .all()
    )
    for b in recent_batches:
        company_count = db.query(Company).filter(Company.batch_id == b.id).count()
        if company_count == 0:
            continue
        items.append({
            "type": "discovery",
            "text": f"Discovered {company_count} compan{'y' if company_count == 1 else 'ies'} in \"{b.name}\"",
            "timestamp": b.created_at,
        })

    recent_messages = (
        db.query(PersonalizedMessage)
        .join(Contact)
        .join(Company)
        .filter(Company.batch_id.in_(batch_ids))
        .filter(PersonalizedMessage.status == "approved")
        .order_by(PersonalizedMessage.generated_at.desc())
        .limit(limit)
        .all()
    )
    for m in recent_messages:
        contact = m.contact
        name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or "a contact"
        company_name = contact.company.name if contact.company else ""
        items.append({
            "type": "message",
            "text": f"Message approved for {name}" + (f" @ {company_name}" if company_name else ""),
            "timestamp": m.generated_at,
        })

    recent_pushes = (
        db.query(CampaignPush)
        .join(Contact)
        .join(Company)
        .filter(Company.batch_id.in_(batch_ids))
        .filter(CampaignPush.status == "pushed")
        .order_by(CampaignPush.pushed_at.desc())
        .limit(limit)
        .all()
    )
    for p in recent_pushes:
        contact = p.contact
        name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or "a contact"
        company_name = contact.company.name if contact.company else ""
        items.append({
            "type": "outreach",
            "text": f"Connection request sent to {name}" + (f" @ {company_name}" if company_name else ""),
            "timestamp": p.pushed_at,
        })

    items = [i for i in items if i["timestamp"] is not None]
    items.sort(key=lambda i: i["timestamp"], reverse=True)
    return items[:limit]


@router.get("/overview/trend")
def get_overview_trend(days: int = 14, db: Session = Depends(get_db)):
    """Daily companies-discovered count for the last N days -- the one trend that matters
    without touching cost/spend (kept backend-only/out of the UI per explicit direction)."""
    days = max(1, min(days, 90))
    since = datetime.utcnow() - timedelta(days=days - 1)
    since = since.replace(hour=0, minute=0, second=0, microsecond=0)

    companies = (
        db.query(Company)
        .join(Batch)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Company.created_at >= since)
        .all()
    )
    counts: dict[str, int] = {}
    for c in companies:
        day_key = c.created_at.strftime("%Y-%m-%d")
        counts[day_key] = counts.get(day_key, 0) + 1

    series = []
    for i in range(days):
        day = since + timedelta(days=i)
        day_key = day.strftime("%Y-%m-%d")
        series.append({"date": day_key, "companies_found": counts.get(day_key, 0)})
    return series


@router.get("/leads/{contact_id}")
def get_lead_detail(contact_id: int, db: Session = Depends(get_db)):
    """Full detail for one lead -- our own research/message data, live SalesRobot status if
    matched, and historical webhook event timeline. Merges what previously required three
    separate calls (message, campaign leads, activity) into one."""
    contact = (
        db.query(Contact)
        .join(Company)
        .filter(Contact.id == contact_id)
        .filter(Company.batch_id.in_(db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)))
        .first()
    )
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    pm = contact.personalized_message
    push = contact.campaign_pushes[-1] if contact.campaign_pushes else None
    live_by_url = _fetch_our_salesrobot_prospects(db)
    live = live_by_url.get(_normalize_linkedin_url(contact.linkedin_url))
    sent_emails = _get_smartlead_sent_emails(db)
    email_sent = bool(contact.email and contact.email.strip().lower() in sent_emails)

    profile_key = _normalize_linkedin_url(contact.linkedin_url) or ""
    events = db.query(CampaignEvent).order_by(CampaignEvent.received_at.desc()).limit(500).all()
    matching_events = [e for e in events if profile_key and profile_key in json.dumps(e.raw_payload).lower()]

    return {
        "contact_id": contact.id,
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "title": contact.title,
        "linkedin_url": contact.linkedin_url,
        "company_name": contact.company.name if contact.company else None,
        "company_linkedin_url": contact.company.linkedin_url if contact.company else None,
        "message_status": pm.status if pm else None,
        "generated_message": pm.generated_message if pm else None,
        "company_research": pm.company_research if pm else None,
        "contact_research": pm.contact_research if pm else None,
        "fit_analysis": pm.fit_analysis if pm else None,
        "push_status": push.status if push else None,
        "pushed_at": push.pushed_at if push else None,
        "salesrobot_last_activity": live.get("lastActivity") if live else None,
        "salesrobot_status": live.get("status") if live else None,
        "salesrobot_raw": live,
        "email": contact.email,
        "email_status": "sent" if email_sent else None,
        "activity_history": [
            {"id": e.id, "event_type": e.event_type, "received_at": e.received_at}
            for e in matching_events
        ],
    }


@router.post("/salesrobot/send-message")
def send_salesrobot_message(thread_id: str, prospect_uuid: str, message: str, db: Session = Depends(get_db)):
    """Sends directly into an existing conversation thread via the real, documented
    POST /sendAMessage endpoint -- independent of campaign-sequence timing/status. For
    delivering a message to a prospect stuck in a state (e.g. status "STOPPED") where the
    campaign's own Step 2 won't fire on its own."""
    account_uuid = _get_salesrobot_linkedin_account_uuid(db)
    try:
        result = send_message_to_prospect(thread_id, prospect_uuid, account_uuid, message, db, ELEPHANT_EDGE_TENANT_ID)
    except SalesRobotError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return result


@router.post("/salesrobot/test-push")
def test_salesrobot_push(campaign_uuid: str, linkedin_url: str, personalized_message: str, first_name: str | None = None, connection_note: str | None = None, email: str | None = None, email_subject: str | None = None, email_body: str | None = None, db: Session = Depends(get_db)):
    """One-off test utility -- pushes a single synthetic prospect to any campaign UUID with
    known customMap values, so we can confirm live whether a SalesRobot campaign's message
    templates actually use API-supplied custom field values (vs. SalesRobot's own "AI
    Variable" generation) before trusting the real pipeline's push. Not tied to a real
    Contact row -- doesn't touch CampaignPush/scoring/any real pipeline state.

    connection_note mirrors the real push path (app/outreach/salesrobot.py): a separate
    short field for the connection-request step, distinct from personalizedMessage (the
    full follow-up message) -- defaults to a generic line if not given, same shape as
    production, so this test reflects what the real system will actually do.

    email/email_subject/email_body added 2026-08-10: routed through Smartlead instead of
    SalesRobot's own email step (SalesRobot's emailId/customMap email rendering was never
    verified live and their email account has no active trial -- Smartlead is the confirmed,
    working email channel as of 2026-08-10, see app/outreach/smartlead.py)."""
    account_uuid = _get_salesrobot_linkedin_account_uuid(db)
    note = connection_note or f"Hi {first_name or 'there'}, I'd love to connect."
    prospect = {
        "profileUrl": linkedin_url,
        "customMap": {"connectionNote": note, "personalizedMessage": personalized_message},
    }
    if first_name:
        prospect["firstName"] = first_name

    try:
        result = add_single_prospect(campaign_uuid, account_uuid, prospect, db, ELEPHANT_EDGE_TENANT_ID)
    except SalesRobotError as e:
        raise HTTPException(status_code=502, detail=str(e))

    email_result = None
    if email and email_subject and email_body:
        try:
            email_result = smartlead_add_lead(
                _get_smartlead_campaign_id(db), email, first_name, email_subject, email_body, db, ELEPHANT_EDGE_TENANT_ID
            )
        except SmartleadError as e:
            email_result = {"error": str(e)}

    return {"sent_prospect": prospect, "salesrobot_response": result, "email_result": email_result}


@router.post("/webhooks/salesrobot/{secret}")
async def salesrobot_webhook(secret: str, request: Request, db: Session = Depends(get_db)):
    if secret != SALESROBOT_WEBHOOK_SECRET:
        raise HTTPException(status_code=404, detail="Not found")

    payload = await request.json()
    linkedin_url = _find_linkedin_url_in_payload(payload)
    contact = None
    if linkedin_url:
        contact = (
            db.query(Contact)
            .filter(Contact.linkedin_url.ilike(f"%{linkedin_url.rstrip('/').split('/')[-1]}%"))
            .first()
        )

    event_type = None
    for key in ("event", "event_type", "type", "action"):
        if isinstance(payload, dict) and payload.get(key):
            event_type = str(payload[key])
            break

    event = CampaignEvent(
        contact_id=contact.id if contact else None,
        event_type=event_type,
        raw_payload=payload,
    )
    db.add(event)
    db.commit()
    return {"ok": True}


@router.get("/campaign-events")
def list_campaign_events(db: Session = Depends(get_db)):
    events = (
        db.query(CampaignEvent)
        .join(Contact, CampaignEvent.contact_id == Contact.id, isouter=True)
        .order_by(CampaignEvent.received_at.desc())
        .limit(200)
        .all()
    )
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "contact_id": e.contact_id,
            "contact_name": f"{e.contact.first_name} {e.contact.last_name}" if e.contact else None,
            "company_name": e.contact.company.name if e.contact and e.contact.company else None,
            "raw_payload": e.raw_payload,
            "received_at": e.received_at,
        }
        for e in events
    ]


# ---- Google Calendar appointment-booking sync ----
# Pulled, not pushed -- Calendar has no webhook for Appointment Schedule bookings specifically,
# so this is a periodic pull (see main.py's scheduler) plus a manual trigger for testing.

@router.get("/calendar-bookings")
def list_calendar_bookings(page: int = 1, page_size: int = 25, search: str = "", db: Session = Depends(get_db)):
    query = db.query(CalendarBooking)
    if search.strip():
        like = f"%{search.strip()}%"
        query = query.filter(or_(CalendarBooking.booker_name.ilike(like), CalendarBooking.booker_email.ilike(like)))

    total = query.count()
    page = max(page, 1)
    page_size = max(1, min(page_size, 100))
    bookings = (
        query
        .order_by(CalendarBooking.start_time.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
        "bookings": [
            {
                "id": b.id,
                "booker_name": b.booker_name,
                "booker_email": b.booker_email,
                "start_time": b.start_time,
                "end_time": b.end_time,
                "status": b.status,
                "raw_payload": b.raw_payload,
                "synced_at": b.synced_at,
                # Meeting outcome (V2 Revenue Pace) -- additive fields, ignored by V1's
                # Meetings.jsx since it doesn't reference them. See
                # app/gtm_os/revenue/revenue_pace.py.
                "outcome_status": b.outcome_status,
                "outcome_company_id": b.outcome_company_id,
                "outcome_company_name": b.outcome_company.name if b.outcome_company else None,
                "outcome_opportunity_id": b.outcome_opportunity_id,
                "outcome_offering_name": b.outcome_offering_name,
                "outcome_amount_usd": b.outcome_amount_usd,
                "outcome_reason": b.outcome_reason,
                "outcome_notes": b.outcome_notes,
                "outcome_icp_snapshot": b.outcome_icp_snapshot,
                "outcome_recorded_at": b.outcome_recorded_at,
                "outcome_recorded_by": b.outcome_recorded_by,
            }
            for b in bookings
        ],
    }


@router.post("/calendar-bookings/sync")
def trigger_calendar_sync(db: Session = Depends(get_db)):
    try:
        return sync_calendar_bookings(db, ELEPHANT_EDGE_TENANT_ID)
    except GoogleCalendarError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.patch("/calendar-bookings/{booking_id}/outcome")
def patch_calendar_booking_outcome(booking_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """V2 Meetings tab -- records (or clears) a human-judged deal outcome for one booked meeting.
    Thin wrapper over record_meeting_outcome() (app/gtm_os/revenue/revenue_pace.py); all
    validation lives there. Same authorization note as every other write route in this file:
    session-cookie auth via the gateway's reverse proxy is the existing mechanism (no
    finer-grained role system exists in this codebase to gate on). `recorded_by` is supplied by
    the frontend from the real, already-authenticated user (same pattern as Phase 7's
    reviewed_by) -- this backend has no independent identity channel of its own."""
    from app.gtm_os.revenue.revenue_pace import record_meeting_outcome
    from app.gtm_os.opportunity.opportunity import Opportunity

    try:
        booking = record_meeting_outcome(
            db,
            ELEPHANT_EDGE_TENANT_ID,
            booking_id,
            status=body.get("status"),
            company_id=body.get("company_id"),
            offering_name=body.get("offering_name"),
            amount_usd=body.get("amount_usd"),
            reason=body.get("reason"),
            notes=body.get("notes"),
            recorded_by=body.get("recorded_by"),
            opportunity_id=body.get("opportunity_id"),
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    opportunity = db.get(Opportunity, booking.outcome_opportunity_id) if booking.outcome_opportunity_id else None
    return {
        "id": booking.id,
        "outcome_status": booking.outcome_status,
        "outcome_company_id": booking.outcome_company_id,
        "outcome_company_name": booking.outcome_company.name if booking.outcome_company else None,
        "outcome_opportunity_id": booking.outcome_opportunity_id,
        "outcome_opportunity_statement": opportunity.opportunity_statement if opportunity else None,
        "outcome_offering_name": booking.outcome_offering_name,
        "outcome_amount_usd": booking.outcome_amount_usd,
        "outcome_reason": booking.outcome_reason,
        "outcome_notes": booking.outcome_notes,
        "outcome_icp_snapshot": booking.outcome_icp_snapshot,
        "outcome_recorded_at": booking.outcome_recorded_at,
        "outcome_recorded_by": booking.outcome_recorded_by,
    }


@router.get("/gtm-os/revenue-pace")
def get_gtm_os_revenue_pace(month: str | None = None, db: Session = Depends(get_db)):
    """V2 Revenue Pace page -- read-only wrapper over get_revenue_pace()
    (app/gtm_os/revenue/revenue_pace.py), itself a pure aggregation over meeting outcomes
    recorded via PATCH /calendar-bookings/{id}/outcome above plus business_context's own
    revenue_goal. Returns target_configured=False (not a guess) when no numeric target is set.
    No cost-per-meeting, no forecast, no generated narrative -- see that module's own docstring
    for why."""
    from app.gtm_os.revenue.revenue_pace import get_revenue_pace

    return get_revenue_pace(db, ELEPHANT_EDGE_TENANT_ID, month=month)


@router.get("/gtm-os/revenue-pace/diagnosis")
def get_gtm_os_revenue_pace_diagnosis(month: str | None = None, db: Session = Depends(get_db)):
    """V2 Revenue Pace Diagnosis -- read-only composition over get_revenue_pace() (unchanged
    source of truth), get_jobs_to_be_done() (blocked opportunities / missing decision-makers),
    get_pipeline_item() (strategy/offering context), and get_overrides_evals() (confirmed
    patterns only -- candidates are never treated as knowledge). See
    app/gtm_os/revenue/revenue_pace_diagnosis.py for the full rule set. No LLM calls; every
    diagnosis_summary clause and the single primary_constraint are produced by fixed,
    documented, deterministic rules over real data."""
    from app.gtm_os.revenue.revenue_pace_diagnosis import get_revenue_pace_diagnosis

    return get_revenue_pace_diagnosis(db, ELEPHANT_EDGE_TENANT_ID, month=month)


# ---- Daily Review (calendar) ----
# A human tracking/communication layer, not a control surface -- lets two people in different
# places (or timezones) independently look at a given day's automated activity and leave a
# verdict + comment for each other. Deliberately disconnected from the real pipeline: approving
# or rejecting a day here never touches PersonalizedMessage.status, CampaignPush, or anything
# that actually sends. review_date is always a "YYYY-MM-DD" string, matching the date-string
# convention already used by /overview/stats' start_date/end_date.

def _parse_review_date(date_str: str) -> datetime:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")


def _day_detail_payload(date_str: str, db: Session) -> dict:
    """Flat, purpose-built shape for the Daily Review page -- not batch-grouped, since a
    reviewer cares about "what happened today" (a timeline + decision-makers found + their
    messages), not which internal batch row a company happened to land in.

    Filters by each entity's OWN timestamp (Company.created_at, Contact.created_at,
    PersonalizedMessage.generated_at), not by Batch.created_at -- fixed 2026-08-10 after a
    real bug: a batch created on day N commonly has decision-maker/message work happen on
    day N+1 (batches get reused across days, not recreated daily), so filtering by the
    parent batch's creation date silently attributed a whole day's real activity to the
    wrong day, leaving the actual day looking empty. Each section below now queries
    independently by its own real timestamp, joined back to Batch only to scope by tenant."""
    day_start = _parse_review_date(date_str)
    day_end = day_start + timedelta(days=1)

    tenant_batch_ids = db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)

    companies_discovered_today = (
        db.query(Company)
        .filter(Company.batch_id.in_(tenant_batch_ids))
        .filter(Company.created_at >= day_start)
        .filter(Company.created_at < day_end)
        .options(selectinload(Company.contacts).selectinload(Contact.personalized_message))
        .order_by(Company.id)
        .all()
    )

    contacts_found_today = (
        db.query(Contact)
        .join(Company)
        .filter(Company.batch_id.in_(tenant_batch_ids))
        .filter(Contact.created_at >= day_start)
        .filter(Contact.created_at < day_end)
        .options(selectinload(Contact.personalized_message), selectinload(Contact.company))
        .order_by(Contact.id)
        .all()
    )

    messages_drafted_today = (
        db.query(PersonalizedMessage)
        .join(Contact)
        .join(Company)
        .filter(Company.batch_id.in_(tenant_batch_ids))
        .filter(PersonalizedMessage.generated_at >= day_start)
        .filter(PersonalizedMessage.generated_at < day_end)
        .options(selectinload(PersonalizedMessage.contact).selectinload(Contact.company))
        .all()
    )

    timeline = []
    for c in companies_discovered_today:
        timeline.append({"type": "discovery", "timestamp": c.created_at, "text": f"Discovered {c.name}"})
    for ct in contacts_found_today:
        ct_name = f"{ct.first_name or ''} {ct.last_name or ''}".strip() or "a decision-maker"
        timeline.append({
            "type": "decision_maker",
            "timestamp": ct.created_at,
            "text": f"Found {ct_name} at {ct.company.name}",
        })
    for pm in messages_drafted_today:
        ct = pm.contact
        timeline.append({
            "type": "message",
            "timestamp": pm.generated_at,
            "text": f"Drafted a message for {ct.first_name or ''} {ct.last_name or ''} at {ct.company.name}".strip(),
        })
    timeline = [t for t in timeline if t["timestamp"] is not None]
    timeline.sort(key=lambda t: t["timestamp"])

    decision_makers = []
    for ct in contacts_found_today:
        pm = ct.personalized_message
        decision_makers.append({
            "contact_id": ct.id,
            "name": f"{ct.first_name or ''} {ct.last_name or ''}".strip(),
            "title": ct.title,
            "company_name": ct.company.name,
            "linkedin_url": ct.linkedin_url,
            "message_status": pm.status if pm else None,
            "generated_message": pm.generated_message if pm else None,
            "contact_email": ct.email,
            "contact_email_source": ct.email_source,
            "email_subject": pm.email_subject if pm else None,
            "email_body": pm.email_body if pm else None,
        })

    # Companies tab shows every company "touched" today -- discovered today OR had a
    # decision-maker found today -- not just companies discovered today. A company found on
    # day N whose decision-maker search happens on day N+1 (routine, since batches/companies
    # get worked on over multiple days) should still show up in day N+1's Companies tab, or a
    # reviewer looking at "what happened today" sees decision-makers with no companies listed
    # at all -- confirmed live as a real, confusing gap (2026-08-11).
    companies_touched_today = {c.id: c for c in companies_discovered_today}
    for ct in contacts_found_today:
        companies_touched_today[ct.company.id] = ct.company

    company_rows = []
    for c in companies_touched_today.values():
        primary_contact = c.contacts[0] if c.contacts else None
        company_rows.append({
            "id": c.id,
            "name": c.name,
            "domain": c.domain,
            "linkedin_url": c.linkedin_url,
            "qualified": _is_company_qualified(c),
            "hiring_signal_role": c.hiring_signal_role,
            "contact_count": len(c.contacts),
            "primary_contact_name": f"{primary_contact.first_name or ''} {primary_contact.last_name or ''}".strip() if primary_contact else None,
            "primary_contact_title": primary_contact.title if primary_contact else None,
        })
    company_rows.sort(key=lambda c: (not c["qualified"], c["name"] or ""))  # qualified first, then alphabetical

    return {
        "summary": {
            "companies_discovered": len(companies_discovered_today),
            "companies_qualified": sum(1 for c in companies_discovered_today if _is_company_qualified(c)),
            "decision_makers_found": len(decision_makers),
        },
        "timeline": timeline,
        "companies": company_rows,
        "decision_makers": decision_makers,
    }


PIPELINE_STEP_NAMES = ["Discovery", "Decision-Maker Search", "Message Drafting", "Approval Window", "Push to Campaigns"]


def _compute_autonomous_steps(run: AutonomousRun | None, decision_makers_today: list[dict]) -> list[dict]:
    """Per the lead's spec: each of the 5 real pipeline stages shows completed / pending /
    failed, with the real reason attached when failed. Message Drafting's success/failure
    isn't tracked as a distinct AutonomousRun field (a drafting failure there gets caught and
    folded into notification_error, not run.status='failed' -- see
    autonomous_orchestrator.py's approval-notification try/except), so that step is judged
    from the real decision_makers list (did any actually get a generated_message) rather
    than from the run row directly."""
    steps = [{"name": name, "status": "pending", "detail": None} for name in PIPELINE_STEP_NAMES]
    if run is None:
        return steps

    if run.status == "failed" and not run.companies_discovered:
        steps[0] = {"name": "Discovery", "status": "failed", "detail": run.error_message}
        return steps
    steps[0] = {"name": "Discovery", "status": "completed", "detail": f"{run.companies_discovered or 0} companies found"}

    if run.status == "failed" and not run.contacts_found:
        steps[1] = {"name": "Decision-Maker Search", "status": "failed", "detail": run.error_message}
        return steps
    steps[1] = {"name": "Decision-Maker Search", "status": "completed", "detail": f"{run.contacts_found or 0} decision-makers found"}

    drafted = sum(1 for dm in decision_makers_today if dm.get("generated_message"))
    if drafted > 0:
        steps[2] = {"name": "Message Drafting", "status": "completed", "detail": f"{drafted} messages drafted"}
    elif run.contacts_found:
        steps[2] = {"name": "Message Drafting", "status": "failed", "detail": "No messages were drafted for the decision-makers found"}
    else:
        return steps  # nothing found to draft for -- rest stay pending

    reviewed = sum(1 for dm in decision_makers_today if dm.get("message_status") in ("approved", "rejected"))
    if run.status == "completed" or reviewed > 0:
        steps[3] = {"name": "Approval Window", "status": "completed", "detail": f"{reviewed} reviewed" if reviewed else None}
    elif run.status == "awaiting_approval":
        until = run.awaiting_approval_until.isoformat() + "Z" if run.awaiting_approval_until else None
        steps[3] = {"name": "Approval Window", "status": "pending", "detail": f"Awaiting review until {until}" if until else "Awaiting review"}
    else:
        return steps

    if run.contacts_pushed:
        steps[4] = {"name": "Push to Campaigns", "status": "completed", "detail": f"{run.contacts_pushed} pushed"}
    # else stays pending -- not yet pushed

    return steps


def _autonomous_day_payload(date_str: str, decision_makers_today: list[dict], db: Session) -> dict:
    """The lead's "what's the plan for the next 24h / what's the outcome" request -- one
    section covering both: the tenant's current autonomous configuration (what it's set to
    do), plus the real run(s) for this specific date and their step-by-step status."""
    day_start = _parse_review_date(date_str)
    day_end = day_start + timedelta(days=1)

    runs = (
        db.query(AutonomousRun)
        .filter(AutonomousRun.run_date >= day_start)
        .filter(AutonomousRun.run_date < day_end)
        .order_by(AutonomousRun.run_date.asc())
        .all()
    )
    latest_run = runs[-1] if runs else None
    hour, minute = get_autonomous_schedule_utc(db, ELEPHANT_EDGE_TENANT_ID)

    return {
        "enabled": is_autonomous_enabled(db, ELEPHANT_EDGE_TENANT_ID),
        "scheduled_time_utc": f"{hour:02d}:{minute:02d}",
        "discovery_source": get_autonomous_discovery_source(db, ELEPHANT_EDGE_TENANT_ID),
        "daily_company_cap": get_daily_company_cap(db, ELEPHANT_EDGE_TENANT_ID),
        "daily_budget_usd": get_daily_budget_usd(db, ELEPHANT_EDGE_TENANT_ID),
        "run_count": len(runs),
        "latest_run": {
            "status": latest_run.status,
            "companies_discovered": latest_run.companies_discovered,
            "contacts_found": latest_run.contacts_found,
            "contacts_pushed": latest_run.contacts_pushed,
            "credits_spent_usd": latest_run.credits_spent_usd,
            "error_message": latest_run.error_message,
        } if latest_run else None,
        "steps": _compute_autonomous_steps(latest_run, decision_makers_today),
    }


@router.get("/calendar/month")
def get_calendar_month(year: int, month: int, db: Session = Depends(get_db)):
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="month must be 1-12")
    month_start = datetime(year, month, 1)
    month_end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)

    # Same fix as _day_detail_payload: real per-entity timestamps, not Batch.created_at (a
    # batch created on day N routinely has real activity on day N+1+, since batches get
    # reused across days rather than recreated daily).
    tenant_batch_ids = db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
    activity_days = set()
    company_dates = (
        db.query(Company.created_at)
        .filter(Company.batch_id.in_(tenant_batch_ids))
        .filter(Company.created_at >= month_start)
        .filter(Company.created_at < month_end)
        .all()
    )
    activity_days |= {row.created_at.strftime("%Y-%m-%d") for row in company_dates if row.created_at}
    contact_dates = (
        db.query(Contact.created_at)
        .join(Company)
        .filter(Company.batch_id.in_(tenant_batch_ids))
        .filter(Contact.created_at >= month_start)
        .filter(Contact.created_at < month_end)
        .all()
    )
    activity_days |= {row.created_at.strftime("%Y-%m-%d") for row in contact_dates if row.created_at}
    message_dates = (
        db.query(PersonalizedMessage.generated_at)
        .join(Contact)
        .join(Company)
        .filter(Company.batch_id.in_(tenant_batch_ids))
        .filter(PersonalizedMessage.generated_at >= month_start)
        .filter(PersonalizedMessage.generated_at < month_end)
        .all()
    )
    activity_days |= {row.generated_at.strftime("%Y-%m-%d") for row in message_dates if row.generated_at}

    reviews = (
        db.query(DailyReview)
        .filter(DailyReview.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(DailyReview.review_date >= month_start.strftime("%Y-%m-%d"))
        .filter(DailyReview.review_date < month_end.strftime("%Y-%m-%d"))
        .all()
    )
    review_by_date = {r.review_date: r.status for r in reviews}

    comments = (
        db.query(ReviewComment.review_date, func.count(ReviewComment.id))
        .filter(ReviewComment.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(ReviewComment.review_date >= month_start.strftime("%Y-%m-%d"))
        .filter(ReviewComment.review_date < month_end.strftime("%Y-%m-%d"))
        .group_by(ReviewComment.review_date)
        .all()
    )
    comment_count_by_date = {date: count for date, count in comments}

    days = []
    for d in sorted(activity_days | set(review_by_date.keys()) | set(comment_count_by_date.keys())):
        days.append({
            "date": d,
            "has_activity": d in activity_days,
            "status": review_by_date.get(d, "pending"),
            "comment_count": comment_count_by_date.get(d, 0),
        })
    return {"year": year, "month": month, "days": days}


@router.get("/calendar/{date}")
def get_calendar_day(date: str, db: Session = Depends(get_db)):
    _parse_review_date(date)  # validates format, raises 400 if malformed
    review = (
        db.query(DailyReview)
        .filter(DailyReview.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(DailyReview.review_date == date)
        .first()
    )
    comments = (
        db.query(ReviewComment)
        .filter(ReviewComment.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(ReviewComment.review_date == date)
        .order_by(ReviewComment.created_at.asc())
        .all()
    )
    detail = _day_detail_payload(date, db)
    # created_at/timestamp are naive UTC (datetime.utcnow()) -- appending "Z" explicitly so the
    # frontend's Date parsing treats them as UTC instead of local time. Found live: a comment
    # posted seconds ago showed "6h ago" because a Z-less ISO string like
    # "2026-08-08T13:06:00" gets parsed by JS as LOCAL time, not UTC, silently shifting it by
    # the browser's own UTC offset.
    for item in detail["timeline"]:
        if item["timestamp"] is not None:
            item["timestamp"] = item["timestamp"].isoformat() + "Z"
    return {
        "date": date,
        "status": review.status if review else "pending",
        "summary": detail["summary"],
        "timeline": detail["timeline"],
        "companies": detail["companies"],
        "decision_makers": detail["decision_makers"],
        "comments": [{"id": c.id, "comment": c.comment, "created_at": c.created_at.isoformat() + "Z"} for c in comments],
        "autonomous": _autonomous_day_payload(date, detail["decision_makers"], db),
    }


class DailyReviewStatus(BaseModel):
    status: str


@router.post("/calendar/{date}/review")
def set_calendar_day_review(date: str, body: DailyReviewStatus, db: Session = Depends(get_db)):
    _parse_review_date(date)
    if body.status not in ("pending", "approved", "rejected"):
        raise HTTPException(status_code=400, detail="status must be pending, approved, or rejected")
    review = (
        db.query(DailyReview)
        .filter(DailyReview.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(DailyReview.review_date == date)
        .first()
    )
    if review:
        review.status = body.status
    else:
        review = DailyReview(tenant_id=ELEPHANT_EDGE_TENANT_ID, review_date=date, status=body.status)
        db.add(review)
    db.commit()
    return {"date": date, "status": review.status}


class ReviewCommentIn(BaseModel):
    comment: str


@router.post("/calendar/{date}/comments")
def add_calendar_day_comment(date: str, body: ReviewCommentIn, db: Session = Depends(get_db)):
    _parse_review_date(date)
    if not body.comment.strip():
        raise HTTPException(status_code=400, detail="comment cannot be empty")
    comment = ReviewComment(tenant_id=ELEPHANT_EDGE_TENANT_ID, review_date=date, comment=body.comment.strip())
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return {"id": comment.id, "comment": comment.comment, "created_at": comment.created_at.isoformat() + "Z"}


@router.delete("/calendar/{date}/comments/{comment_id}")
def delete_calendar_day_comment(date: str, comment_id: int, db: Session = Depends(get_db)):
    comment = (
        db.query(ReviewComment)
        .filter(ReviewComment.id == comment_id)
        .filter(ReviewComment.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(ReviewComment.review_date == date)
        .first()
    )
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    db.delete(comment)
    db.commit()
    return {"deleted": True}


# ---- AI Chat Widget ----
# Internal-only (Elephant Edge's own team, not customer-facing) -- so tool actions execute
# directly with no per-action confirmation step, EXCEPT pushing to a SalesRobot campaign,
# which is deliberately not exposed as a tool at all: the autonomous cycle already owns real
# sends end to end, and every manual push this session has been a one-off testing/tuning
# action, not something that should be one chat message away from happening by accident.
#
# Conversations are tenant-scoped, not per-user -- this backend has no user identity concept
# anywhere (auth lives entirely in the gateway), matching every other table here.

CHAT_SYSTEM_PROMPT_TEMPLATE = """You are the AI assistant embedded in Elephant Edge's Sales \
Operating System dashboard -- an internal tool used only by Elephant Edge's own team to run \
outbound company discovery, decision-maker research, personalized message drafting, and \
LinkedIn outreach via SalesRobot.

Today's date is {today}.

Answer questions about the platform's data (companies found, decision-makers, messages, \
connections, replies, meetings, notifications, batches, etc.) using the tools available -- \
always call a tool to get real numbers rather than guessing or estimating. When asked "how \
many X yesterday/this week/last month", compute the actual date range yourself from today's \
date and pass it to the relevant tool.

You can also take actions on the user's behalf: approve/reject a drafted message, exclude a \
contact from being pushed, mark notifications read, and generate a downloadable CSV of any \
company or lead list. Since this is an internal team tool, take these actions directly when \
asked -- no need to ask for confirmation first.

You CANNOT push contacts to a SalesRobot campaign (send connection requests or LinkedIn \
messages) -- that capability is deliberately not available here. If asked, explain that \
real sends happen through the autonomous pipeline or a manual push in the dashboard, not \
through chat.

The platform's pipeline, stage by stage: company discovery (via TheirStack/Crustdata job \
postings, Jobo, or Apify's LinkedIn Jobs Scraper) -> qualification (hiring-signal + team-fit \
scoring) -> decision-maker search -> personalized message drafting (curiosity-style, no \
pitch) -> human approval -> push to a SalesRobot LinkedIn campaign (connection request + \
follow-up message, or direct InMail for Open Profile members) -> reply/meeting tracking. \
There's also an autonomous mode that runs discovery through decision-maker search on a \
daily schedule, with a human approval window before anything gets pushed.

Be concise and direct -- this is a working tool for a small team, not a customer support \
chat. Use real numbers from tool calls, never invent or estimate them."""


CHAT_TOOLS = [
    {
        "name": "get_funnel_stats",
        "description": "Full outbound funnel counts: companies researched, companies qualified, decision-makers found, messages approved, connections sent/accepted, replied, meetings booked. Optionally date-filtered.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            },
        },
    },
    {
        "name": "get_leads_stats",
        "description": "Messages drafted/approved counts, and SalesRobot's own aggregate connections sent/accepted/replied counts with acceptance and reply rates.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_activity",
        "description": "Recent real events feed: discoveries, message approvals, connection pushes, most recent first.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "default 8, max 50"}},
        },
    },
    {
        "name": "get_companies_found_trend",
        "description": "Daily count of companies discovered for the last N days -- use this for \"how many companies were found on/since <date>\" style questions.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "default 14, max 90"}},
        },
    },
    {
        "name": "search_companies",
        "description": "Search/filter companies across all batches. Returns up to `limit` matching rows plus the real total match count (which may be larger than what's returned).",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "matches name, domain, or industry"},
                "qualified": {"type": "string", "enum": ["true", "false"]},
                "source": {"type": "string", "enum": ["jd_first", "jobo", "apify", "deepline", "manual"]},
                "created_after": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                "created_before": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                "limit": {"type": "integer", "description": "default 20, max 100"},
            },
        },
    },
    {
        "name": "get_company_detail",
        "description": "Full detail for one company by name or domain, including its contacts and their message/push status.",
        "input_schema": {
            "type": "object",
            "properties": {"name_or_domain": {"type": "string"}},
            "required": ["name_or_domain"],
        },
    },
    {
        "name": "search_leads",
        "description": "Search/filter decision-maker contacts (leads) across all batches, including message and push status and live SalesRobot activity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "matches contact name, title, or company name"},
                "message_status": {"type": "string", "enum": ["draft", "approved", "rejected"]},
                "activity": {"type": "string", "enum": ["CONNECTED", "REPLIED", "NO_REPLY_YET", "NOT_SENT"]},
                "pipeline_only": {"type": "boolean", "description": "default true -- only contacts with a drafted message or a push attempt; set false to include every decision-maker found regardless of stage"},
                "limit": {"type": "integer", "description": "default 20, max 100"},
            },
        },
    },
    {
        "name": "list_batches",
        "description": "List discovery batches (a batch = one discovery run) with phase/status/company count.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["jd_first", "jobo", "apify", "deepline", "manual"]},
                "limit": {"type": "integer", "description": "default 20, max 100"},
            },
        },
    },
    {
        "name": "get_batch_detail",
        "description": "Full detail for one batch: every company in it, their qualification/scoring, and their contacts.",
        "input_schema": {
            "type": "object",
            "properties": {"batch_id": {"type": "integer"}},
            "required": ["batch_id"],
        },
    },
    {
        "name": "list_notifications",
        "description": "Recent in-app notifications (run failed, decision-makers found, meeting booked, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "unread_only": {"type": "boolean"},
                "limit": {"type": "integer", "description": "default 10, max 50"},
            },
        },
    },
    {
        "name": "approve_message",
        "description": "Approve a contact's drafted outreach message so it's eligible to be pushed.",
        "input_schema": {
            "type": "object",
            "properties": {"contact_id": {"type": "integer"}},
            "required": ["contact_id"],
        },
    },
    {
        "name": "reject_message",
        "description": "Reject a contact's drafted outreach message.",
        "input_schema": {
            "type": "object",
            "properties": {"contact_id": {"type": "integer"}},
            "required": ["contact_id"],
        },
    },
    {
        "name": "set_contact_excluded_from_push",
        "description": "Include or exclude a contact from being pushed to an outreach campaign.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer"},
                "excluded": {"type": "boolean"},
            },
            "required": ["contact_id", "excluded"],
        },
    },
    {
        "name": "mark_notification_read",
        "description": "Mark one notification as read.",
        "input_schema": {
            "type": "object",
            "properties": {"notification_id": {"type": "integer"}},
            "required": ["notification_id"],
        },
    },
    {
        "name": "mark_all_notifications_read",
        "description": "Mark every unread notification as read.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "export_csv",
        "description": "Generate a downloadable CSV of a filtered company or lead list. Use this whenever the user asks for a CSV, spreadsheet, export, or list to download.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "enum": ["companies", "leads"]},
                "search": {"type": "string"},
                "qualified": {"type": "string", "enum": ["true", "false"], "description": "companies only"},
                "source": {"type": "string", "enum": ["jd_first", "jobo", "apify", "deepline", "manual"], "description": "companies only"},
                "message_status": {"type": "string", "enum": ["draft", "approved", "rejected"], "description": "leads only"},
                "created_after": {"type": "string", "description": "YYYY-MM-DD"},
                "created_before": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["entity"],
        },
    },
]


def _chat_parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d")


def _chat_query_companies(db: Session, search: str = "", qualified: str = "", source: str = "",
                           created_after: str = "", created_before: str = "") -> list[Company]:
    query = db.query(Company).join(Batch).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
    if search.strip():
        like = f"%{search.strip()}%"
        query = query.filter(or_(Company.name.ilike(like), Company.domain.ilike(like), Company.industry.ilike(like)))
    after_dt = _chat_parse_date(created_after)
    before_dt = _chat_parse_date(created_before)
    if after_dt:
        query = query.filter(Company.created_at >= after_dt)
    if before_dt:
        query = query.filter(Company.created_at < before_dt + timedelta(days=1))
    companies = query.order_by(Company.created_at.desc()).all()
    if source:
        companies = [c for c in companies if _normalize_company_source(c.source) == source]
    if qualified == "true":
        companies = [c for c in companies if _is_company_qualified(c)]
    elif qualified == "false":
        companies = [c for c in companies if not _is_company_qualified(c)]
    return companies


def _execute_chat_tool(name: str, tool_input: dict, db: Session) -> dict:
    """Dispatches one Claude tool_use call to real DB queries/mutations. Every branch is
    wrapped by the caller in a try/except -- a failed tool call becomes an error string fed
    back to Claude, not a crashed chat turn."""
    if name == "get_funnel_stats":
        return get_overview_stats(start_date=tool_input.get("start_date"), end_date=tool_input.get("end_date"), db=db)

    if name == "get_leads_stats":
        return get_leads_stats(db=db)

    if name == "get_recent_activity":
        limit = min(tool_input.get("limit", 8), 50)
        return {"activity": get_overview_recent_activity(limit=limit, db=db)}

    if name == "get_companies_found_trend":
        days = min(tool_input.get("days", 14), 90)
        return {"trend": get_overview_trend(days=days, db=db)}

    if name == "search_companies":
        limit = min(tool_input.get("limit", 20), 100)
        companies = _chat_query_companies(
            db, search=tool_input.get("search", ""), qualified=tool_input.get("qualified", ""),
            source=tool_input.get("source", ""), created_after=tool_input.get("created_after", ""),
            created_before=tool_input.get("created_before", ""),
        )
        return {
            "total_matches": len(companies),
            "companies": [
                {
                    "id": c.id, "name": c.name, "domain": c.domain, "source": _normalize_company_source(c.source),
                    "qualified": _is_company_qualified(c), "hiring_signal_role": c.hiring_signal_role,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in companies[:limit]
            ],
        }

    if name == "get_company_detail":
        needle = tool_input["name_or_domain"].strip()
        like = f"%{needle}%"
        company = (
            db.query(Company).join(Batch).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
            .filter(or_(Company.name.ilike(like), Company.domain.ilike(like)))
            .order_by(Company.created_at.desc()).first()
        )
        if not company:
            return {"error": f"No company matching '{needle}' found"}
        return {
            "id": company.id, "name": company.name, "domain": company.domain, "industry": company.industry,
            "source": _normalize_company_source(company.source), "qualified": _is_company_qualified(company),
            "hiring_signal_role": company.hiring_signal_role, "hiring_signal_reasoning": company.hiring_signal_reasoning,
            "batch_id": company.batch_id, "created_at": company.created_at.isoformat() if company.created_at else None,
            "contacts": [
                {
                    "id": ct.id, "name": f"{ct.first_name or ''} {ct.last_name or ''}".strip(), "title": ct.title,
                    "message_status": ct.personalized_message.status if ct.personalized_message else None,
                    "excluded_from_push": ct.excluded_from_push,
                }
                for ct in company.contacts
            ],
        }

    if name == "search_leads":
        limit = min(tool_input.get("limit", 20), 100)
        result = list_leads(
            page=1, page_size=limit, search=tool_input.get("search", ""),
            message_status=tool_input.get("message_status", ""), activity=tool_input.get("activity", ""),
            pipeline_only=tool_input.get("pipeline_only", True), db=db,
        )
        return result

    if name == "list_batches":
        limit = min(tool_input.get("limit", 20), 100)
        return list_batches(page=1, page_size=limit, source=tool_input.get("source"), db=db)

    if name == "get_batch_detail":
        payload = _build_batch_payload(tool_input["batch_id"], page=1, page_size=200, db=db)
        if payload is None:
            return {"error": f"No batch with id {tool_input['batch_id']}"}
        return payload

    if name == "list_notifications":
        limit = min(tool_input.get("limit", 10), 50)
        return list_notifications(page=1, page_size=limit, unread_only=tool_input.get("unread_only", False), db=db)

    if name == "approve_message":
        contact = db.query(Contact).join(Company).filter(Contact.id == tool_input["contact_id"]).filter(
            Company.batch_id.in_(db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID))).first()
        if not contact or not contact.personalized_message:
            return {"error": "No generated message for this contact"}
        contact.personalized_message.status = "approved"
        db.commit()
        bump_batch_version(contact.company.batch_id)
        return {"contact_id": contact.id, "status": "approved"}

    if name == "reject_message":
        contact = db.query(Contact).join(Company).filter(Contact.id == tool_input["contact_id"]).filter(
            Company.batch_id.in_(db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID))).first()
        if not contact or not contact.personalized_message:
            return {"error": "No generated message for this contact"}
        contact.personalized_message.status = "rejected"
        db.commit()
        bump_batch_version(contact.company.batch_id)
        return {"contact_id": contact.id, "status": "rejected"}

    if name == "set_contact_excluded_from_push":
        contact = db.query(Contact).join(Company).filter(Contact.id == tool_input["contact_id"]).filter(
            Company.batch_id.in_(db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID))).first()
        if not contact:
            return {"error": "Contact not found"}
        contact.excluded_from_push = tool_input["excluded"]
        db.commit()
        bump_batch_version(contact.company.batch_id)
        return {"contact_id": contact.id, "excluded_from_push": contact.excluded_from_push}

    if name == "mark_notification_read":
        return mark_notification_read(tool_input["notification_id"], db=db)

    if name == "mark_all_notifications_read":
        return mark_all_notifications_read(db=db)

    if name == "export_csv":
        entity = tool_input["entity"]
        if entity == "companies":
            companies = _chat_query_companies(
                db, search=tool_input.get("search", ""), qualified=tool_input.get("qualified", ""),
                source=tool_input.get("source", ""), created_after=tool_input.get("created_after", ""),
                created_before=tool_input.get("created_before", ""),
            )
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["name", "domain", "industry", "source", "qualified", "hiring_signal_role", "created_at"])
            for c in companies:
                writer.writerow([c.name, c.domain, c.industry, _normalize_company_source(c.source),
                                  _is_company_qualified(c), c.hiring_signal_role,
                                  c.created_at.isoformat() if c.created_at else ""])
            row_count = len(companies)
        elif entity == "leads":
            # Direct query, not list_leads() -- that route caps page_size at 100 internally,
            # which would silently truncate an export past today's real ~80 total contacts as
            # the dataset grows. No live SalesRobot enrichment needed for a CSV, so this skips
            # the extra API calls list_leads makes for the `activity` filter entirely.
            query = (
                db.query(Contact).join(Company)
                .filter(Company.batch_id.in_(db.query(Batch.id).filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)))
                .options(selectinload(Contact.personalized_message), selectinload(Contact.campaign_pushes), selectinload(Contact.company))
            )
            search = tool_input.get("search", "")
            if search.strip():
                like = f"%{search.strip()}%"
                query = query.filter(or_(Contact.first_name.ilike(like), Contact.last_name.ilike(like),
                                          Contact.title.ilike(like), Company.name.ilike(like)))
            message_status = tool_input.get("message_status", "")
            if message_status:
                query = query.filter(Contact.personalized_message.has(PersonalizedMessage.status == message_status))
            after_dt = _chat_parse_date(tool_input.get("created_after"))
            before_dt = _chat_parse_date(tool_input.get("created_before"))
            if after_dt:
                query = query.filter(Contact.created_at >= after_dt)
            if before_dt:
                query = query.filter(Contact.created_at < before_dt + timedelta(days=1))
            contacts = query.order_by(Contact.created_at.desc()).all()

            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["first_name", "last_name", "title", "company_name", "linkedin_url", "message_status", "push_status"])
            for ct in contacts:
                push = ct.campaign_pushes[-1] if ct.campaign_pushes else None
                writer.writerow([ct.first_name, ct.last_name, ct.title, ct.company.name if ct.company else "",
                                  ct.linkedin_url, ct.personalized_message.status if ct.personalized_message else "",
                                  push.status if push else ""])
            row_count = len(contacts)
        else:
            return {"error": "entity must be 'companies' or 'leads'"}

        filename = f"{entity}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        return {"_csv_filename": filename, "_csv_content": buf.getvalue(), "row_count": row_count}

    return {"error": f"Unknown tool '{name}'"}


CHAT_HISTORY_WINDOW = 20  # prior turns fed back to Claude as context -- not unbounded, so
# cost per message stays flat as a conversation ages, matching the real per-call cost
# already logged in claude_client.py.
CHAT_MAX_TOOL_ITERATIONS = 6


def _run_chat_turn(conversation_id: int, user_text: str, db: Session) -> dict:
    prior = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(CHAT_HISTORY_WINDOW)
        .all()
    )
    prior.reverse()

    db.add(ChatMessage(conversation_id=conversation_id, role="user", content=user_text))
    db.commit()

    messages = [{"role": m.role, "content": m.content} for m in prior]
    messages.append({"role": "user", "content": user_text})

    system = CHAT_SYSTEM_PROMPT_TEMPLATE.format(today=datetime.utcnow().strftime("%Y-%m-%d"))
    tools_used: list[str] = []
    csv_attachment = None

    for _ in range(CHAT_MAX_TOOL_ITERATIONS):
        try:
            response = call_claude_messages(messages, db, ELEPHANT_EDGE_TENANT_ID, system=system, tools=CHAT_TOOLS)
        except ClaudeError as e:
            reply = f"I hit an error talking to Claude: {e}"
            db.add(ChatMessage(conversation_id=conversation_id, role="assistant", content=reply, tools_used=tools_used))
            db.commit()
            return {"reply": reply, "tools_used": tools_used, "csv": None}

        content_blocks = response.get("content", [])
        if response.get("stop_reason") != "tool_use":
            reply = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text").strip()
            if not reply:
                reply = "I wasn't able to generate a response for that."
            db.add(ChatMessage(conversation_id=conversation_id, role="assistant", content=reply, tools_used=tools_used))
            conv = db.query(ChatConversation).filter(ChatConversation.id == conversation_id).first()
            if conv:
                conv.updated_at = datetime.utcnow()
            db.commit()
            return {"reply": reply, "tools_used": tools_used, "csv": csv_attachment}

        messages.append({"role": "assistant", "content": content_blocks})
        tool_results = []
        for block in content_blocks:
            if block.get("type") != "tool_use":
                continue
            tool_name = block["name"]
            tools_used.append(tool_name)
            try:
                result = _execute_chat_tool(tool_name, block.get("input", {}), db)
            except Exception as e:
                result = {"error": str(e)}

            if isinstance(result, dict) and "_csv_content" in result:
                csv_attachment = {"filename": result["_csv_filename"], "content": result["_csv_content"]}
                result = {"status": "csv_generated", "filename": result["_csv_filename"], "row_count": result["row_count"]}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(result, default=str)[:8000],
            })
        messages.append({"role": "user", "content": tool_results})

    reply = "That took more steps than I could complete in one go -- try narrowing the question."
    db.add(ChatMessage(conversation_id=conversation_id, role="assistant", content=reply, tools_used=tools_used))
    db.commit()
    return {"reply": reply, "tools_used": tools_used, "csv": csv_attachment}


def _get_or_create_latest_conversation(db: Session) -> ChatConversation:
    conv = (
        db.query(ChatConversation)
        .filter(ChatConversation.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .order_by(ChatConversation.updated_at.desc())
        .first()
    )
    if conv:
        return conv
    conv = ChatConversation(tenant_id=ELEPHANT_EDGE_TENANT_ID)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


@router.get("/chat/latest")
def get_latest_chat_conversation(db: Session = Depends(get_db)):
    conv = _get_or_create_latest_conversation(db)
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conv.id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    return {
        "conversation_id": conv.id,
        "messages": [{"role": m.role, "content": m.content, "tools_used": m.tools_used, "created_at": m.created_at} for m in messages],
    }


@router.post("/chat/new")
def start_new_chat_conversation(db: Session = Depends(get_db)):
    conv = ChatConversation(tenant_id=ELEPHANT_EDGE_TENANT_ID)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return {"conversation_id": conv.id, "messages": []}


class ChatMessageIn(BaseModel):
    message: str


@router.post("/chat/conversations/{conversation_id}/messages")
def send_chat_message(conversation_id: int, body: ChatMessageIn, db: Session = Depends(get_db)):
    conv = (
        db.query(ChatConversation)
        .filter(ChatConversation.id == conversation_id)
        .filter(ChatConversation.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message cannot be empty")

    result = _run_chat_turn(conversation_id, body.message.strip(), db)
    csv_payload = None
    if result["csv"]:
        csv_payload = {
            "filename": result["csv"]["filename"],
            "content_base64": base64.b64encode(result["csv"]["content"].encode("utf-8")).decode("ascii"),
        }
    return {"reply": result["reply"], "tools_used": result["tools_used"], "csv": csv_payload}


# ---- In-app notifications -- see app/notifications.py for where these get created ----

@router.get("/notifications")
def list_notifications(page: int = 1, page_size: int = 20, unread_only: bool = False, db: Session = Depends(get_db)):
    """Auto-deletes anything past the 30-day retention window on every read (see
    delete_expired_notifications) -- no separate scheduled job needed at this volume."""
    delete_expired_notifications(db, ELEPHANT_EDGE_TENANT_ID)

    query = db.query(Notification).filter(Notification.tenant_id == ELEPHANT_EDGE_TENANT_ID)
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))

    total = query.count()
    unread_count = (
        db.query(Notification)
        .filter(Notification.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Notification.read_at.is_(None))
        .count()
    )
    page = max(page, 1)
    page_size = max(1, min(page_size, 100))
    items = (
        query
        .order_by(Notification.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
        "unread_count": unread_count,
        "notifications": [
            {
                "id": n.id,
                "type": n.type,
                "severity": n.severity,
                "title": n.title,
                "message": n.message,
                "batch_id": n.batch_id,
                "run_id": n.run_id,
                "read": n.read_at is not None,
                "created_at": n.created_at,
            }
            for n in items
        ],
    }


class NotificationIds(BaseModel):
    ids: list[int]


@router.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int, db: Session = Depends(get_db)):
    notif = (
        db.query(Notification)
        .filter(Notification.id == notification_id)
        .filter(Notification.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notif.read_at is None:
        notif.read_at = datetime.utcnow()
        db.commit()
    return {"id": notification_id, "read": True}


@router.post("/notifications/read-all")
def mark_all_notifications_read(db: Session = Depends(get_db)):
    updated = (
        db.query(Notification)
        .filter(Notification.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Notification.read_at.is_(None))
        .update({"read_at": datetime.utcnow()})
    )
    db.commit()
    return {"marked_read": updated}


@router.post("/notifications/bulk-delete")
def bulk_delete_notifications(body: NotificationIds, db: Session = Depends(get_db)):
    deleted = (
        db.query(Notification)
        .filter(Notification.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Notification.id.in_(body.ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"deleted": deleted}


@router.delete("/notifications/{notification_id}")
def delete_notification(notification_id: int, db: Session = Depends(get_db)):
    notif = (
        db.query(Notification)
        .filter(Notification.id == notification_id)
        .filter(Notification.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    db.delete(notif)
    db.commit()
    return {"deleted": True}


# ---- Phase 12: Campaign Execution ----
# The channel is chosen by get_outreach_channel (tenant's own "outreach_channel" Parameter,
# defaults to HeyReach) -- run_campaign_execution itself has no idea which channel it's using.

@router.post("/batches/{batch_id}/phases/outreach")
def execute_outreach_push(batch_id: int, db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    channel = get_outreach_channel(db, ELEPHANT_EDGE_TENANT_ID)
    try:
        result = run_campaign_execution(batch_id, db, channel)
    except (HeyReachError, SalesRobotError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    batch.current_phase = "outreach_done"
    db.commit()
    bump_batch_version(batch_id)
    return result


# ---- Credentials (Settings page) ----

@router.get("/credentials")
def list_credentials(db: Session = Depends(get_db)):
    creds = db.query(Credential).filter(Credential.tenant_id == ELEPHANT_EDGE_TENANT_ID).all()
    return [{"name": c.name, "is_set": bool(c.value), "updated_at": c.updated_at} for c in creds]


@router.post("/credentials")
def upsert_credential(name: str, value: str, db: Session = Depends(get_db)):
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Credential.name == name)
        .first()
    )
    if cred:
        cred.value = value
    else:
        cred = Credential(tenant_id=ELEPHANT_EDGE_TENANT_ID, name=name, value=value)
        db.add(cred)
    db.commit()
    return {"name": name, "is_set": True}


@router.delete("/credentials/{name}")
def delete_credential(name: str, db: Session = Depends(get_db)):
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Credential.name == name)
        .first()
    )
    if cred:
        db.delete(cred)
        db.commit()
    return {"deleted": name}


# ---- Parameters (autonomous_enabled, daily_credit_budget_usd, daily_company_cap) ----

@router.get("/parameters")
def list_parameters(db: Session = Depends(get_db)):
    params = db.query(Parameter).filter(Parameter.tenant_id == ELEPHANT_EDGE_TENANT_ID).all()
    return [{"key": p.key, "value": p.value, "description": p.description} for p in params]


@router.post("/parameters")
def upsert_parameter(key: str, value: dict, description: str = "", db: Session = Depends(get_db)):
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Parameter.key == key)
        .first()
    )
    if param:
        param.value = value
        param.description = description or param.description
    else:
        param = Parameter(tenant_id=ELEPHANT_EDGE_TENANT_ID, key=key, value=value, description=description)
        db.add(param)
    db.commit()
    return {"key": key, "value": value}


# ---- Autonomous system control ----
# Same generic control surface as Synefi's (status/toggle/runs/weekly-report/trigger-now) --
# reused because it's tenant-agnostic scheduling/reporting infrastructure, not business logic.

@router.get("/autonomous/status")
def get_autonomous_status(db: Session = Depends(get_db)):
    last_run = (
        db.query(AutonomousRun)
        .join(Batch)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .order_by(AutonomousRun.started_at.desc())
        .first()
    )
    return {
        "enabled": is_autonomous_enabled(db, ELEPHANT_EDGE_TENANT_ID),
        "daily_budget_usd": get_daily_budget_usd(db, ELEPHANT_EDGE_TENANT_ID),
        "daily_company_cap": get_daily_company_cap(db, ELEPHANT_EDGE_TENANT_ID),
        "discovery_source": get_autonomous_discovery_source(db, ELEPHANT_EDGE_TENANT_ID),
        "message_style": get_autonomous_message_style(db, ELEPHANT_EDGE_TENANT_ID),
        "schedule_utc": dict(zip(("hour", "minute"), get_autonomous_schedule_utc(db, ELEPHANT_EDGE_TENANT_ID))),
        "last_run": {
            "id": last_run.id,
            "batch_id": last_run.batch_id,
            "run_date": last_run.run_date,
            "status": last_run.status,
            "companies_selected": last_run.companies_selected,
            "contacts_pushed": last_run.contacts_pushed,
            "awaiting_approval_until": last_run.awaiting_approval_until,
        } if last_run else None,
    }


@router.post("/autonomous/toggle")
def toggle_autonomous(enabled: bool, db: Session = Depends(get_db)):
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Parameter.key == "autonomous_enabled")
        .first()
    )
    if param:
        param.value = {"enabled": enabled}
    else:
        param = Parameter(tenant_id=ELEPHANT_EDGE_TENANT_ID, key="autonomous_enabled", value={"enabled": enabled},
                           description="Start/pause the daily autonomous cycle")
        db.add(param)
    db.commit()
    return {"enabled": enabled}


@router.post("/autonomous/schedule")
def set_autonomous_schedule(hour: int, minute: int, db: Session = Depends(get_db)):
    """Fixed daily UTC time the autonomous cycle fires at. Applies immediately (no redeploy
    needed) -- reschedule_autonomous_job calls scheduler.reschedule_job on the live
    APScheduler instance. Import is deferred to avoid a circular import (main.py imports
    this router; this route needs something back from main.py)."""
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        raise HTTPException(status_code=400, detail="hour must be 0-23, minute must be 0-59")
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Parameter.key == "autonomous_schedule_utc")
        .first()
    )
    if param:
        param.value = {"hour": hour, "minute": minute}
    else:
        param = Parameter(tenant_id=ELEPHANT_EDGE_TENANT_ID, key="autonomous_schedule_utc", value={"hour": hour, "minute": minute},
                           description="Fixed daily UTC time the autonomous cycle fires at")
        db.add(param)
    db.commit()

    from app.main import reschedule_autonomous_job
    reschedule_autonomous_job(hour, minute)
    return {"hour": hour, "minute": minute}


@router.post("/autonomous/discovery-source")
def set_autonomous_discovery_source(source: str, db: Session = Depends(get_db)):
    """Which pipeline the daily autonomous trigger uses -- validated here (not just in the
    generic /parameters endpoint) so a typo can't silently disable the source check in
    run_daily_autonomous_cycle and fall through to the deepline default unexpectedly."""
    if source not in ("deepline", "jobo", "jd_first", "apify"):
        raise HTTPException(status_code=400, detail="source must be 'deepline', 'jobo', 'jd_first', or 'apify'")
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Parameter.key == "autonomous_discovery_source")
        .first()
    )
    if param:
        param.value = {"source": source}
    else:
        param = Parameter(tenant_id=ELEPHANT_EDGE_TENANT_ID, key="autonomous_discovery_source", value={"source": source},
                           description="Which pipeline (deepline or jobo) the daily autonomous trigger uses")
        db.add(param)
    db.commit()
    return {"discovery_source": source}


@router.post("/autonomous/message-style")
def set_autonomous_message_style(style: str, db: Session = Depends(get_db)):
    """Which Phase 13 prompt the daily autonomous trigger uses -- "pitch" or "curiosity" (see
    autonomous_orchestrator.py's get_autonomous_message_style docstring for why this exists:
    the real autonomous cycle was silently always using "pitch", never actually wired to the
    curiosity-style prompt built and tested separately)."""
    if style not in ("pitch", "curiosity"):
        raise HTTPException(status_code=400, detail="style must be 'pitch' or 'curiosity'")
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Parameter.key == "autonomous_message_style")
        .first()
    )
    if param:
        param.value = {"style": style}
    else:
        param = Parameter(tenant_id=ELEPHANT_EDGE_TENANT_ID, key="autonomous_message_style", value={"style": style},
                           description="Which Phase 13 prompt (pitch or curiosity) the daily autonomous trigger uses")
        db.add(param)
    db.commit()
    return {"message_style": style}


@router.get("/autonomous/runs")
def list_autonomous_runs(db: Session = Depends(get_db)):
    runs = (
        db.query(AutonomousRun)
        .join(Batch)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .order_by(AutonomousRun.started_at.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "run_date": r.run_date,
            "batch_id": r.batch_id,
            "status": r.status,
            "companies_discovered": r.companies_discovered,
            "companies_selected": r.companies_selected,
            "contacts_found": r.contacts_found,
            "contacts_pushed": r.contacts_pushed,
            "credits_spent_usd": r.credits_spent_usd,
            "budget_stopped_early": r.budget_stopped_early,
            "error_message": r.error_message,
        }
        for r in runs
    ]


@router.get("/autonomous/weekly-report")
def get_weekly_report(db: Session = Depends(get_db)):
    runs = (
        db.query(AutonomousRun)
        .join(Batch)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(AutonomousRun.status == "completed")
        .all()
    )
    return {
        "days_run": len(runs),
        "total_companies_discovered": sum(r.companies_discovered for r in runs),
        "total_companies_selected": sum(r.companies_selected for r in runs),
        "total_contacts_found": sum(r.contacts_found for r in runs),
        "total_contacts_pushed": sum(r.contacts_pushed for r in runs),
    }


@router.post("/autonomous/trigger-now")
def trigger_autonomous_now(db: Session = Depends(get_db)):
    """Manual override to run today's cycle immediately instead of waiting for the 24h
    scheduler tick -- e.g. for testing before going live."""
    return run_daily_autonomous_cycle(db, tenant_id=ELEPHANT_EDGE_TENANT_ID)


@router.post("/autonomous/runs/{run_id}/cancel")
def cancel_autonomous_run(run_id: int, db: Session = Depends(get_db)):
    """Cancels a run still in its 1-hour approval window -- the periodic sweep checks this
    before ever pushing to the outreach channel. Not cancellable once it's already resumed."""
    return cancel_run(run_id, db, ELEPHANT_EDGE_TENANT_ID)


@router.post("/autonomous/runs/{run_id}/recover")
def recover_autonomous_run(run_id: int, db: Session = Depends(get_db)):
    """One-off recovery for a run whose status was incorrectly flipped to 'failed' by a
    notification-layer bug after it had already genuinely reached awaiting_approval -- see
    recover_run_to_awaiting_approval's docstring."""
    return recover_run_to_awaiting_approval(run_id, db, ELEPHANT_EDGE_TENANT_ID)


@router.post("/autonomous/runs/{run_id}/resend-notification")
def resend_run_notification(run_id: int, db: Session = Depends(get_db)):
    """Re-sends the approval email/Slack using messages already generated and stored -- no
    new Aviato/Gemini/Claude calls. Needed when the original notification attempt failed
    outright (see resend_approval_notification's docstring)."""
    return resend_approval_notification(run_id, db, ELEPHANT_EDGE_TENANT_ID)


@router.post("/autonomous/resume-check")
def manual_resume_check(db: Session = Depends(get_db)):
    """Manual override to run the approval-window sweep immediately, instead of waiting for
    the 5-minute scheduler tick -- e.g. for testing."""
    return resume_pending_approvals(db, ELEPHANT_EDGE_TENANT_ID)


@router.get("/deepline/balance")
def deepline_balance():
    """Free (no billed call) sanity check for the exact mechanism BudgetGuard depends on --
    lets this specific call be verified against this deployment's real auth (env-var API key,
    no interactive CLI session to fall back on) without spending anything or running a full
    cycle first."""
    try:
        return {"ok": True, "rough_usd_balance": get_credit_balance_usd()}
    except DeeplineError as e:
        return {"ok": False, "error": str(e)}


@router.get("/linkedin-monitor/profiles")
def list_linkedin_monitor_profiles(db: Session = Depends(get_db)):
    from app.phases.gtm_partner_matching import is_cro_focused

    profiles = (
        db.query(LinkedinMonitorProfile)
        .filter(LinkedinMonitorProfile.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .order_by(LinkedinMonitorProfile.id)
        .all()
    )
    return [
        {
            "id": p.id, "name": p.name, "linkedin_url": p.linkedin_url, "company": p.company,
            "active": p.active, "last_checked_at": p.last_checked_at,
            "industry": p.industry, "sells_to": p.sells_to,
            "classification_status": p.classification_status,
            "classification_confidence": p.classification_confidence,
            "classification_reasoning": p.classification_reasoning,
            "classified_at": p.classified_at,
            "is_cro": is_cro_focused(p),
        }
        for p in profiles
    ]


@router.post("/linkedin-monitor/profiles")
def add_linkedin_monitor_profile(name: str | None = None, linkedin_url: str = "", company: str | None = None, db: Session = Depends(get_db)):
    if not linkedin_url:
        raise HTTPException(status_code=400, detail="linkedin_url is required")
    existing = (
        db.query(LinkedinMonitorProfile)
        .filter(LinkedinMonitorProfile.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(LinkedinMonitorProfile.linkedin_url == linkedin_url)
        .first()
    )
    if existing:
        return {"id": existing.id, "already_existed": True}
    profile = LinkedinMonitorProfile(tenant_id=ELEPHANT_EDGE_TENANT_ID, name=name, linkedin_url=linkedin_url, company=company)
    db.add(profile)
    db.commit()
    return {"id": profile.id, "already_existed": False}


@router.delete("/linkedin-monitor/profiles/{profile_id}")
def delete_linkedin_monitor_profile(profile_id: int, db: Session = Depends(get_db)):
    profile = (
        db.query(LinkedinMonitorProfile)
        .filter(LinkedinMonitorProfile.id == profile_id)
        .filter(LinkedinMonitorProfile.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    db.delete(profile)
    db.commit()
    return {"deleted": True}


@router.patch("/linkedin-monitor/profiles/{profile_id}")
def update_linkedin_monitor_profile(profile_id: int, name: str | None = None, company: str | None = None, active: bool | None = None, slack_user_id: str | None = None, db: Session = Depends(get_db)):
    profile = (
        db.query(LinkedinMonitorProfile)
        .filter(LinkedinMonitorProfile.id == profile_id)
        .filter(LinkedinMonitorProfile.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if name is not None:
        profile.name = name
    if company is not None:
        profile.company = company
    if active is not None:
        profile.active = active
    if slack_user_id is not None:
        # Manual confirmation path -- deliberately no validation against Slack's own API here
        # (that would tempt auto-accepting anything that merely LOOKS like a user id). A human
        # pasting this in is expected to have verified it's the right person themselves.
        profile.slack_user_id = slack_user_id or None
    db.commit()
    return {"updated": True}


@router.get("/linkedin-monitor/keywords")
def get_linkedin_monitor_keywords(db: Session = Depends(get_db)):
    from app.phases.linkedin_monitor import get_keyword_tiers
    return get_keyword_tiers(db, ELEPHANT_EDGE_TENANT_ID)


@router.put("/linkedin-monitor/keywords")
def put_linkedin_monitor_keywords(tiers: dict[str, list[str]], db: Session = Depends(get_db)):
    """Full replace -- the dashboard's Targets > Settings tab sends the complete taxonomy back
    on every save, so adding/removing a tier or a single keyword is just editing the object
    client-side and PUTting the whole thing, no separate add/remove endpoints needed."""
    from app.phases.linkedin_monitor import set_keyword_tiers
    set_keyword_tiers(db, ELEPHANT_EDGE_TENANT_ID, tiers)
    return {"updated": True}


@router.get("/linkedin-monitor/signals")
def list_linkedin_monitor_signals(limit: int = 50, db: Session = Depends(get_db)):
    signals = (
        db.query(LinkedinMonitorSignal)
        .filter(LinkedinMonitorSignal.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .order_by(LinkedinMonitorSignal.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": s.id, "profile_id": s.profile_id, "profile_name": s.profile.name if s.profile else None,
            "post_url": s.post_url, "post_text": s.post_text, "author_name": s.author_name,
            "posted_at": s.posted_at, "matched_keywords": s.matched_keywords, "tier": s.tier,
            "relevance_score": s.relevance_score, "recommended_action": s.recommended_action,
            "classifier_reason": s.classifier_reason,
            "alerted_at": s.alerted_at, "created_at": s.created_at,
        }
        for s in signals
    ]


@router.post("/linkedin-monitor/sweep")
def trigger_linkedin_monitor_sweep(db: Session = Depends(get_db)):
    """Manual trigger for testing -- the scheduled sweep runs automatically once enabled, but
    this lets a test post be checked immediately instead of waiting for the next tick."""
    from app.phases.linkedin_monitor import run_linkedin_monitor_sweep
    return run_linkedin_monitor_sweep(db, ELEPHANT_EDGE_TENANT_ID)


@router.get("/linkedin-monitor/schedule")
def get_linkedin_monitor_schedule(db: Session = Depends(get_db)):
    """Real, current poll interval + pause state -- replaces the "Every 45 min" string that used
    to be hardcoded directly in the Targets page (Targets > Settings, 2026-08-18)."""
    from app.phases.linkedin_monitor import get_monitor_schedule
    return get_monitor_schedule(db, ELEPHANT_EDGE_TENANT_ID)


@router.put("/linkedin-monitor/schedule")
def put_linkedin_monitor_schedule(body: dict = Body(...), db: Session = Depends(get_db)):
    """Saves the new interval/pause state AND reschedules the live scheduler job immediately
    (see reschedule_linkedin_monitor_job in main.py) -- so a change here takes effect on this
    process right away, not just on the next restart."""
    from app.phases.linkedin_monitor import ScheduleConfigError, set_monitor_schedule
    from app.main import reschedule_linkedin_monitor_job

    try:
        schedule = set_monitor_schedule(
            db, ELEPHANT_EDGE_TENANT_ID,
            days=body.get("days", 0), hours=body.get("hours", 0), minutes=body.get("minutes", 0),
            enabled=body.get("enabled", True),
        )
    except ScheduleConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))

    reschedule_linkedin_monitor_job(schedule["interval_minutes"])
    return schedule


# ---- GTM Partner Categorization -- which industry a watched profile is in, and who they sell
# to, so the lead can spot cross-industry referral-partner candidates. See
# app/phases/gtm_partner_classification.py for the full grounding/no-fabrication discipline. Not
# on the 45-min schedule (unlike the sweep above) -- classification quality depends on how many
# real posts have accumulated, so it's manually triggered/refreshed, not run blindly every tick.

@router.post("/linkedin-monitor/classify")
def trigger_gtm_partner_classification(only_unclassified: bool = True, db: Session = Depends(get_db)):
    from app.phases.gtm_partner_classification import run_gtm_partner_classification_sweep
    return run_gtm_partner_classification_sweep(db, ELEPHANT_EDGE_TENANT_ID, only_unclassified=only_unclassified)


@router.get("/linkedin-monitor/partner-matches")
def get_gtm_partner_matches(db: Session = Depends(get_db)):
    from app.phases.gtm_partner_classification import get_partner_matches
    return get_partner_matches(db, ELEPHANT_EDGE_TENANT_ID)


@router.get("/reverse-discovery/candidates")
def list_reverse_discovery_candidates(icp_status: str = "", limit: int = 100, db: Session = Depends(get_db)):
    query = db.query(ReverseDiscoveryCandidate).filter(ReverseDiscoveryCandidate.tenant_id == ELEPHANT_EDGE_TENANT_ID)
    if icp_status:
        query = query.filter(ReverseDiscoveryCandidate.icp_status == icp_status)
    candidates = query.order_by(ReverseDiscoveryCandidate.id.desc()).limit(limit).all()
    return [
        {
            "id": c.id, "post_url": c.post_url, "post_text": c.post_text, "matched_keyword": c.matched_keyword,
            "author_name": c.author_name, "author_profile_url": c.author_profile_url,
            "author_occupation": c.author_occupation, "guessed_company_name": c.guessed_company_name,
            "relevance_score": c.relevance_score, "recommended_action": c.recommended_action,
            "classifier_reason": c.classifier_reason, "icp_status": c.icp_status, "icp_reasoning": c.icp_reasoning,
            "posted_at": c.posted_at, "created_at": c.created_at,
        }
        for c in candidates
    ]


@router.post("/reverse-discovery/sweep")
def trigger_reverse_discovery_sweep(db: Session = Depends(get_db)):
    """Manual trigger for testing -- not yet on a schedule (see Mode B build notes)."""
    from app.phases.reverse_discovery import run_reverse_discovery_sweep
    return run_reverse_discovery_sweep(db, ELEPHANT_EDGE_TENANT_ID)


@router.post("/linkedin-monitor/backfill-names")
def backfill_linkedin_monitor_names(db: Session = Depends(get_db)):
    """One-time-ish utility: fills in name/company for profiles added with only a LinkedIn URL
    (targetedcompanies.md had no name/company for ~80 of them), using the same post-scraper
    actor's own author metadata -- no scrapeUntil filter, limitPerSource=1, so this pulls each
    profile's single most recent post (if any) purely to read the author's name off it. Real
    cost: ~$0.002 per profile that has at least one public post, ~$0 for one with none."""
    from app.apify_client import search_linkedin_posts
    from app.apify_client import _get_api_key as _get_apify_api_key
    from app.phases.linkedin_monitor import PROFILES_PER_BATCH

    profiles = (
        db.query(LinkedinMonitorProfile)
        .filter(LinkedinMonitorProfile.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(LinkedinMonitorProfile.name.is_(None))
        .all()
    )
    if not profiles:
        return {"checked": 0, "updated": 0}

    api_key = _get_apify_api_key(db, ELEPHANT_EDGE_TENANT_ID)
    updated = 0
    for i in range(0, len(profiles), PROFILES_PER_BATCH):
        batch = profiles[i:i + PROFILES_PER_BATCH]
        urls = [p.linkedin_url for p in batch]
        try:
            posts = search_linkedin_posts(api_key, urls, limit_per_source=1)
        except Exception:
            continue
        by_url = {p.linkedin_url.rstrip("/").lower(): p for p in batch}
        for post in posts:
            input_url = (post.get("inputUrl") or post.get("authorProfileUrl") or "").rstrip("/").lower()
            profile = by_url.get(input_url)
            if not profile:
                continue
            author = post.get("author") or {}
            name = post.get("authorName") or " ".join(filter(None, [author.get("firstName"), author.get("lastName")])).strip()
            occupation = author.get("occupation") or ""
            if name:
                profile.name = name
            if occupation and " at " in occupation.lower():
                profile.company = occupation.split(" at ")[-1].strip()
            elif occupation and "@" in occupation:
                profile.company = occupation.split("@")[-1].strip()
            updated += 1
        db.commit()

    return {"checked": len(profiles), "updated": updated}


# ---- GTM Partner -> Company matching -- daily automated pipeline: match companies to a
# partner's specialty, human approval, auto-draft an outreach message, human approval, manual
# "mark sent" (no live send integration yet -- channel undecided). See
# app/phases/gtm_partner_matching.py and app/phases/gtm_partner_messaging.py.

@router.post("/linkedin-monitor/match-companies")
def trigger_partner_matching_sweep(only_new_profiles: bool = True, profile_id: int | None = None, db: Session = Depends(get_db)):
    """profile_id, when given, scopes this run to exactly that one partner -- the real
    per-partner "Run matching" action in the Recommended Companies detail view. Without it, this
    runs across every eligible partner at once, which should stay a deliberate bulk action (or
    just let the daily schedule handle it), not the default click target."""
    from app.phases.gtm_partner_matching import run_partner_matching_sweep
    return run_partner_matching_sweep(db, ELEPHANT_EDGE_TENANT_ID, only_new_profiles=only_new_profiles, profile_id=profile_id)


@router.get("/linkedin-monitor/match-cap")
def get_partner_match_cap(db: Session = Depends(get_db)):
    from app.phases.gtm_partner_matching import get_match_cap
    return {"cap": get_match_cap(db, ELEPHANT_EDGE_TENANT_ID)}


@router.put("/linkedin-monitor/match-cap")
def put_partner_match_cap(body: dict = Body(...), db: Session = Depends(get_db)):
    from app.phases.gtm_partner_matching import ScheduleConfigError, set_match_cap
    try:
        cap = set_match_cap(db, ELEPHANT_EDGE_TENANT_ID, body.get("cap"))
    except ScheduleConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"cap": cap}


@router.get("/linkedin-monitor/match-schedule")
def get_partner_match_schedule(db: Session = Depends(get_db)):
    from app.phases.gtm_partner_matching import get_match_schedule
    return get_match_schedule(db, ELEPHANT_EDGE_TENANT_ID)


@router.put("/linkedin-monitor/match-schedule")
def put_partner_match_schedule(body: dict = Body(...), db: Session = Depends(get_db)):
    """Saves the schedule AND reschedules the live scheduler job immediately -- same pattern as
    PUT /linkedin-monitor/schedule (see reschedule_partner_matching_job in main.py)."""
    from app.phases.gtm_partner_matching import ScheduleConfigError, set_match_schedule
    from app.main import reschedule_partner_matching_job

    try:
        schedule = set_match_schedule(
            db, ELEPHANT_EDGE_TENANT_ID,
            days=body.get("days", 0), hours=body.get("hours", 0), minutes=body.get("minutes", 0),
            enabled=body.get("enabled", True),
        )
    except ScheduleConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))

    reschedule_partner_matching_job(schedule["interval_minutes"])
    return schedule


@router.get("/linkedin-monitor/recommendations")
def list_partner_recommendations(status: str = "", profile_id: int | None = None, db: Session = Depends(get_db)):
    from app.phases.gtm_partner_matching import get_all_recommendations, get_recommendations_for_profile
    recs = (
        get_recommendations_for_profile(db, ELEPHANT_EDGE_TENANT_ID, profile_id) if profile_id
        else get_all_recommendations(db, ELEPHANT_EDGE_TENANT_ID, status=status or None)
    )
    if profile_id and status:
        recs = [r for r in recs if r.status == status]
    companies_by_id = {c.id: c for c in db.query(Company).filter(Company.id.in_([r.company_id for r in recs])).all()}
    return [
        {
            "id": r.id, "profile_id": r.profile_id, "company_id": r.company_id,
            "company_name": companies_by_id[r.company_id].name if r.company_id in companies_by_id else None,
            "company_domain": companies_by_id[r.company_id].domain if r.company_id in companies_by_id else None,
            "match_reasoning": r.match_reasoning, "match_confidence": r.match_confidence,
            "status": r.status, "created_at": r.created_at, "reviewed_at": r.reviewed_at,
        }
        for r in recs
    ]


@router.patch("/linkedin-monitor/recommendations/{recommendation_id}")
def update_partner_recommendation(recommendation_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Approving a recommendation auto-drafts the outreach message right away (was previously a
    separate manual "Draft message" click) -- generate_recommendation_message() already only
    covers approved recs not yet part of a message, so this is safe to call on every approval,
    not just the first. A generation failure never blocks the approval itself from succeeding."""
    from app.phases.gtm_partner_matching import ScheduleConfigError, update_recommendation_status
    try:
        rec = update_recommendation_status(db, ELEPHANT_EDGE_TENANT_ID, recommendation_id, body.get("status"))
    except ScheduleConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    message_created = False
    if rec.status == "approved":
        from app.phases.gtm_partner_messaging import generate_recommendation_message
        try:
            message_created = bool(generate_recommendation_message(db, ELEPHANT_EDGE_TENANT_ID, rec.profile_id))
        except Exception:  # noqa: BLE001 -- approval must succeed even if drafting fails
            message_created = False

    return {"updated": True, "status": rec.status, "message_drafted": message_created}


@router.post("/linkedin-monitor/recommendations/generate-message")
def trigger_generate_recommendation_message(profile_id: int, db: Session = Depends(get_db)):
    from app.phases.gtm_partner_messaging import generate_recommendation_message
    msg = generate_recommendation_message(db, ELEPHANT_EDGE_TENANT_ID, profile_id)
    if not msg:
        return {"created": False, "reason": "no newly-approved recommendations to draft a message for"}
    return {"created": True, "message_id": msg.id, "status": msg.status, "generated_message": msg.generated_message}


@router.get("/linkedin-monitor/messages")
def list_partner_recommendation_messages(profile_id: int, db: Session = Depends(get_db)):
    from app.phases.gtm_partner_messaging import get_messages_for_profile
    msgs = get_messages_for_profile(db, ELEPHANT_EDGE_TENANT_ID, profile_id)
    return [
        {
            "id": m.id, "profile_id": m.profile_id, "recommendation_ids": m.recommendation_ids,
            "generated_message": m.generated_message, "status": m.status,
            "generated_at": m.generated_at, "reviewed_at": m.reviewed_at,
            "sent_at": m.sent_at, "send_channel": m.send_channel,
        }
        for m in msgs
    ]


@router.patch("/linkedin-monitor/messages/{message_id}")
def update_partner_recommendation_message(message_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    from app.phases.gtm_partner_messaging import ScheduleConfigError, update_message_status
    try:
        msg = update_message_status(db, ELEPHANT_EDGE_TENANT_ID, message_id, body.get("status"))
    except ScheduleConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    return {"updated": True, "status": msg.status}


@router.patch("/linkedin-monitor/messages/{message_id}/mark-sent")
def mark_partner_recommendation_message_sent(message_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """For send_channel="slack" with a slack_user_id on file, this actually sends the DM (see
    app/slack_bot_client.py) -- a real send failure returns 502, NOT marked sent, so the human
    knows to retry or fall back to manual delivery rather than believing it went out."""
    from app.phases.gtm_partner_messaging import ScheduleConfigError, mark_message_sent
    from app.slack_bot_client import SlackBotError
    try:
        msg, auto_sent = mark_message_sent(db, ELEPHANT_EDGE_TENANT_ID, message_id, body.get("send_channel"))
    except ScheduleConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SlackBotError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    return {"updated": True, "status": msg.status, "send_channel": msg.send_channel, "auto_sent": auto_sent}


@router.post("/linkedin-monitor/profiles/{profile_id}/slack-lookup")
def lookup_partner_slack_id(profile_id: int, body: dict = Body(...), db: Session = Depends(get_db)):
    """Exact-email Slack lookup only -- see app/slack_bot_client.py's module docstring for why
    there's deliberately no fuzzy/name-based alternative. Sets slack_user_id on the profile only
    when Slack itself confirms an exact match; returns found=False (not an error) otherwise, so
    the UI can fall back to asking for a manually-confirmed id instead."""
    email = (body.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="email is required")
    profile = (
        db.query(LinkedinMonitorProfile)
        .filter(LinkedinMonitorProfile.id == profile_id)
        .filter(LinkedinMonitorProfile.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    from app.slack_bot_client import SlackBotError, lookup_user_id_by_email
    try:
        user_id = lookup_user_id_by_email(db, ELEPHANT_EDGE_TENANT_ID, email)
    except SlackBotError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not user_id:
        return {"found": False}
    profile.slack_user_id = user_id
    db.commit()
    return {"found": True, "slack_user_id": user_id}


@router.get("/gtm-os/business-context")
def get_gtm_os_business_context(db: Session = Depends(get_db)):
    """GTM OS foundation step -- read-only view of the company's own operating context (goals,
    offerings, ICP, TAM, GTM motions, sales methodology, messaging). See
    app/gtm_os/context/business_context.py. Purely additive: this is the only place anything
    in app/gtm_os/ is wired into the running app, and it's a brand-new route, not a change to
    any existing one."""
    from app.gtm_os.context.business_context import get_business_context
    return get_business_context(db, ELEPHANT_EDGE_TENANT_ID)


@router.put("/gtm-os/business-context")
def put_gtm_os_business_context(context: dict, db: Session = Depends(get_db)):
    from app.gtm_os.context.business_context import set_business_context
    set_business_context(db, ELEPHANT_EDGE_TENANT_ID, context)
    return {"updated": True}


@router.get("/gtm-os/accounts/{company_id}/brief")
def get_gtm_os_account_brief(company_id: int, db: Session = Depends(get_db)):
    """V2 Account 360 (Phase 2) -- read-only wrapper over the existing, unmodified
    build_account_brief() (Batch 12). Same "purely additive, brand-new route" precedent as
    /gtm-os/business-context above: this function contains zero business logic of its own, it
    only calls the already-built aggregation and returns its JSON verbatim. 404 only when the
    company_id doesn't resolve to a real row at all (mirrors the 404 pattern used elsewhere in
    this file, e.g. GET /batches/{batch_id}) -- build_account_brief() itself already returns
    that exact case as {"company": None, "account_status": "insufficient_context", ...}, so the
    404 here is just translating that into a standard HTTP not-found rather than a 200 with a
    null company."""
    from app.gtm_os.account_agent.account_agent import build_account_brief

    company = db.get(Company, company_id)
    if company is None or company.batch.tenant_id != ELEPHANT_EDGE_TENANT_ID:
        raise HTTPException(status_code=404, detail="Company not found")

    return build_account_brief(db, ELEPHANT_EDGE_TENANT_ID, company_id)


@router.get("/gtm-os/accounts/{company_id}/messages")
def get_gtm_os_account_messages(company_id: int, db: Session = Depends(get_db)):
    """V2 Account 360 Messages tab (Phase 3, Part 6) -- read-only wrapper over
    list_messages_for_company() (Batch 7's MessageDraft, unmodified). Same additive-route
    precedent as the two routes above. Never generates, never approves -- see that function's
    own docstring for the exact safety guarantees."""
    from app.gtm_os.learning.message_draft import list_messages_for_company

    company = db.get(Company, company_id)
    if company is None or company.batch.tenant_id != ELEPHANT_EDGE_TENANT_ID:
        raise HTTPException(status_code=404, detail="Company not found")

    return {"company_id": company_id, "messages": list_messages_for_company(db, ELEPHANT_EDGE_TENANT_ID, company_id)}


@router.get("/gtm-os/accounts/summary")
def get_gtm_os_accounts_summary(db: Session = Depends(get_db)):
    """V2 Accounts list summary strip (Phase 3, Part 8) -- read-only wrapper over
    summarize_account_states() (Batch 12's account_status ladder, reproduced via bulk queries
    instead of build_account_brief() per company -- see that function's own docstring for why
    and how it stays in exact agreement with the per-company version)."""
    from app.gtm_os.account_agent.account_agent import summarize_account_states

    return summarize_account_states(db, ELEPHANT_EDGE_TENANT_ID)


@router.get("/gtm-os/pipeline")
def get_gtm_os_pipeline(page: int = 1, page_size: int = 25, db: Session = Depends(get_db)):
    """V2 Pipeline page (Phase 3, Part 2) -- read-only wrapper over list_pipeline_items()
    (Batch 13's execution-readiness module, unmodified reuse of get_next_execution_action() and
    recommend_gtm_motion() per item -- see that function's own docstring)."""
    from app.gtm_os.execution.execution_readiness import list_pipeline_items

    return list_pipeline_items(db, ELEPHANT_EDGE_TENANT_ID, page=page, page_size=page_size)


@router.get("/gtm-os/market-intelligence")
def get_gtm_os_market_intelligence(db: Session = Depends(get_db)):
    """V2 Market Intelligence page (Phase 4, Part 4) -- read-only wrapper over
    get_market_intelligence_overview() (Batch 2/6/11's trend intelligence + market-account
    bridge, unmodified reuse of evaluate_topic_trend() per configured topic)."""
    from app.gtm_os.content.trend_intelligence import get_market_intelligence_overview

    return get_market_intelligence_overview(db, ELEPHANT_EDGE_TENANT_ID)


@router.get("/gtm-os/content-topics")
def get_gtm_os_content_topics(db: Session = Depends(get_db)):
    """Content Intelligence topic configuration (topics.py, Step 16A) -- no override saved yet
    returns the live-derived default (from this tenant's real ICP/offering/business-context
    config), same "no override -> compute fresh" contract as get_content_topics() itself."""
    from app.gtm_os.content.topics import get_content_topics

    return {"topics": get_content_topics(db, ELEPHANT_EDGE_TENANT_ID)}


@router.put("/gtm-os/content-topics")
def put_gtm_os_content_topics(body: dict = Body(...), db: Session = Depends(get_db)):
    """Saves a human override of the topic list -- same Parameter-backed pattern as
    pattern-detection-config above. Once saved, this list is the source of truth going forward
    (see topics.py's set_content_topics() docstring)."""
    from app.gtm_os.content.topics import TopicConfigError, get_content_topics, set_content_topics

    topics = body.get("topics")
    if not isinstance(topics, list):
        raise HTTPException(status_code=400, detail="body must be an object with a 'topics' list")
    try:
        set_content_topics(db, ELEPHANT_EDGE_TENANT_ID, topics)
    except TopicConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"topics": get_content_topics(db, ELEPHANT_EDGE_TENANT_ID)}


@router.get("/gtm-os/debug/signal-texts")
def get_gtm_os_debug_signal_texts(limit: int = 500, db: Session = Depends(get_db)):
    """TEMPORARY read-only diagnostic (2026-08-19) -- dumps the exact text topic_linking.py's
    match_topic() actually searches (via its own unmodified extract_signal_text()), so real
    alias/keyword gaps can be audited against real signal language before touching any matching
    logic. No topic/matching/trend code is touched by this route. Intended to be removed once
    the audit is done."""
    from app.gtm_os.content.topic_linking import extract_signal_text
    from app.gtm_os.intelligence.signal import GtmSignal

    signals = (
        db.query(GtmSignal)
        .filter(GtmSignal.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .order_by(GtmSignal.id)
        .limit(limit)
        .all()
    )
    return {
        "count": len(signals),
        "signals": [
            {"id": s.id, "source": s.source, "observed_at": s.observed_at, "text": extract_signal_text(s)}
            for s in signals
        ],
    }


@router.get("/gtm-os/demand-grid")
def get_gtm_os_demand_grid(db: Session = Depends(get_db)):
    """V2 Demand Grid page (Phase 4, Part 7) -- read-only wrapper over get_demand_grid()
    (Batch 8/9's ICP + offering configuration, aggregated -- see that function's own docstring
    for the exact "applicable" vs "matched_account_count" distinction it preserves)."""
    from app.gtm_os.icp.icp_offering_matching import get_demand_grid

    return get_demand_grid(db, ELEPHANT_EDGE_TENANT_ID)


@router.get("/gtm-os/demand-grid/{icp_id}/companies")
def get_gtm_os_demand_grid_companies(icp_id: str, db: Session = Depends(get_db)):
    """Demand Grid drill-down (Phase 4, Part 12) -- read-only wrapper over
    get_demand_grid_companies() (real ICPMatch rows only, see that function's own docstring)."""
    from app.gtm_os.icp.icp_offering_matching import get_demand_grid_companies

    return get_demand_grid_companies(db, ELEPHANT_EDGE_TENANT_ID, icp_id)


@router.get("/gtm-os/icps-offerings")
def get_gtm_os_icps_offerings(db: Session = Depends(get_db)):
    """V2 ICPs & Offerings configuration page (Phase 5) -- read-only wrapper over
    get_icps_offerings_overview() (ICP/offering/motion config readers + Demand Grid, all
    unmodified -- see that function's own docstring)."""
    from app.gtm_os.icp.icp_offering_matching import get_icps_offerings_overview

    return get_icps_offerings_overview(db, ELEPHANT_EDGE_TENANT_ID)


# ---- Configuration writes (Phase 5, Part 15) ----
# AUTHORIZATION NOTE: this app has no role/permission system -- the gateway's own module
# docstring is explicit ("Single shared login for the whole platform -- there is no per-tenant
# auth. Any authenticated user may access any tenant"). That is the SAME mechanism every other
# write in this file already relies on (PUT /gtm-os/business-context, PUT /parameters, PUT
# /credentials, POST /batches, ...) -- reused here as-is, not a new or weaker gate invented for
# this phase. Session-cookie auth (Depends(get_db) alone doesn't enforce it, but the gateway's
# reverse proxy in front of this backend does -- see app/main.py) is the existing safe mechanism;
# there is no finer-grained "who may edit business config" concept anywhere in this codebase to
# reuse or fake. If tighter authorization is ever needed, that is a real gap for the gateway
# layer, not something to invent here.
@router.put("/gtm-os/icps")
def put_gtm_os_icps(icps: list = Body(...), db: Session = Depends(get_db)):
    """Reuses set_icp_config() (Batch 8) verbatim -- all validation lives there, not duplicated
    here. Returns the freshly-read overview so the frontend never has to guess whether the write
    landed."""
    from app.gtm_os.icp.icp_config import IcpConfigError, set_icp_config
    from app.gtm_os.icp.icp_offering_matching import get_icps_offerings_overview

    try:
        set_icp_config(db, ELEPHANT_EDGE_TENANT_ID, icps)
    except IcpConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return get_icps_offerings_overview(db, ELEPHANT_EDGE_TENANT_ID)


@router.put("/gtm-os/offerings")
def put_gtm_os_offerings(offerings: list = Body(...), db: Session = Depends(get_db)):
    """Reuses set_offering_config() (Batch 4) verbatim."""
    from app.gtm_os.icp.icp_offering_matching import get_icps_offerings_overview
    from app.gtm_os.opportunity.offering_config import OfferingConfigError, set_offering_config

    try:
        set_offering_config(db, ELEPHANT_EDGE_TENANT_ID, offerings)
    except OfferingConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return get_icps_offerings_overview(db, ELEPHANT_EDGE_TENANT_ID)


@router.put("/gtm-os/gtm-motions")
def put_gtm_os_gtm_motions(motions: list = Body(...), db: Session = Depends(get_db)):
    """Reuses set_gtm_motion_config() (Batch 10) verbatim."""
    from app.gtm_os.gtm_motion.gtm_motion_config import GtmMotionConfigError, set_gtm_motion_config
    from app.gtm_os.icp.icp_offering_matching import get_icps_offerings_overview

    try:
        set_gtm_motion_config(db, ELEPHANT_EDGE_TENANT_ID, motions)
    except GtmMotionConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return get_icps_offerings_overview(db, ELEPHANT_EDGE_TENANT_ID)


@router.get("/gtm-os/briefing-governance")
def get_gtm_os_briefing_governance(db: Session = Depends(get_db)):
    """V2 Briefing/Governance page (Phase 6) -- the SAME "purely additive, brand-new route, zero
    new business logic" pattern as every /gtm-os/* route above. Returns evaluate_gtm_governance()
    (Batch 14)'s output verbatim -- no field added, none removed, none renamed. Batch 14 has no
    single overall "ready/blocked" status field and no separate "briefing" object; the frontend
    derives only a presentational count (configuration_gaps.length, etc.) from these same real
    lists, never a new score or narrative (see Phase 6 report for why no such backend field
    exists to reuse).

    PERFORMANCE FIX (V2 UI audit, 2026-08-18): used to call evaluate_gtm_governance() live on
    every request -- confirmed directly against production to take 5+ minutes (4+ full sweeps
    over 706 companies), which was the actual cause of the Briefing page's reported slow load.
    Now reads the latest GovernanceSnapshot (computed hourly by _scheduled_governance_snapshot,
    app/main.py) instead -- same function, same numbers, just computed on a schedule rather than
    per-request. Adds `computed_at` alongside the unchanged governance fields so the frontend can
    show real staleness ("as of 12 minutes ago") rather than implying this is always live.
    Bootstraps a snapshot synchronously ONLY on the very first-ever call for this tenant (no row
    exists yet) -- every call after that is a fast read, even if slow.

    AUTHORIZATION: same note as every Phase 5 write route -- this app has no role/permission
    system, only the gateway's existing "logged in or not" session-cookie gate."""
    from app.gtm_os.governance.governance import compute_and_store_governance_snapshot, get_latest_governance_snapshot

    latest = get_latest_governance_snapshot(db, ELEPHANT_EDGE_TENANT_ID)
    if latest is None:
        snapshot = compute_and_store_governance_snapshot(db, ELEPHANT_EDGE_TENANT_ID)
        computed_at = datetime.utcnow()
    else:
        snapshot, computed_at = latest
    return {**snapshot, "computed_at": computed_at}


@router.post("/gtm-os/briefing-governance/refresh")
def post_gtm_os_briefing_governance_refresh(db: Session = Depends(get_db)):
    """Manual 'Refresh now' for the Briefing page -- the ONLY other caller of
    compute_and_store_governance_snapshot() besides the hourly scheduler. Deliberately synchronous
    (the caller explicitly asked for a fresh recompute right now and the frontend shows a loading
    state while it runs) rather than a background-job + poll pattern -- this is a rare, explicit,
    human-initiated action, not the default page-load path, so the same multi-minute cost that
    was wrong to pay on every GET is acceptable here exactly because it's opt-in."""
    from app.gtm_os.governance.governance import compute_and_store_governance_snapshot

    snapshot = compute_and_store_governance_snapshot(db, ELEPHANT_EDGE_TENANT_ID)
    return {**snapshot, "computed_at": datetime.utcnow()}


@router.get("/gtm-os/pipeline/{opportunity_id}")
def get_gtm_os_pipeline_item(opportunity_id: int, db: Session = Depends(get_db)):
    """V2 Opportunity Detail (Phase 7) -- read-only wrapper over get_pipeline_item(), itself the
    exact per-item computation list_pipeline_items() (Phase 3) already uses -- see that
    function's own docstring. Combine with GET /gtm-os/accounts/{company_id}/brief (Phase 2, via
    this item's own company_id) for the account-level ICP/offering/motion/governance context, and
    GET /gtm-os/accounts/{company_id}/messages (Phase 3) for message review -- deliberately NOT
    duplicated into one mega-endpoint (Part 13's own "prefer existing combined-read patterns"
    instruction), since both of those already exist and already return everything this page needs
    beyond the opportunity's own execution/strategy facts."""
    from app.gtm_os.execution.execution_readiness import get_pipeline_item

    item = get_pipeline_item(db, ELEPHANT_EDGE_TENANT_ID, opportunity_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return item


@router.patch("/gtm-os/messages/{message_draft_id}")
def patch_gtm_os_message(message_draft_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """2026-08-25, explicit instruction -- real human editing of AI-generated content before
    approval. Thin dispatch over update_message_draft_content() (message_draft.py); no editing
    logic lives in this route. Rejects editing an already-approved draft with 400 (same
    immutability rule the review route's own approve action already enforces)."""
    from app.gtm_os.learning.message_draft import update_message_draft_content

    subject = payload.get("subject")
    message_text = payload.get("message_text")
    if subject is None and message_text is None:
        raise HTTPException(status_code=400, detail="at least one of subject/message_text must be provided")

    try:
        draft = update_message_draft_content(db, ELEPHANT_EDGE_TENANT_ID, message_draft_id, subject=subject, message_text=message_text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "id": draft.id, "subject": draft.subject, "message_text": draft.message_text,
        "status": draft.status, "last_updated_at": draft.last_updated_at,
    }


@router.patch("/gtm-os/contacts/{contact_id}/email")
def patch_gtm_os_contact_email(contact_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """2026-08-25, explicit instruction -- lets a human add/correct a contact's email directly
    from the Message Workspace (e.g. the contact has none on file yet, blocking an email draft).
    Thin dispatch over update_contact_email() (contact_discovery.py); no editing logic here."""
    from app.gtm_os.sales.contact_discovery import update_contact_email

    email = payload.get("email")
    if not isinstance(email, str) or not email.strip():
        raise HTTPException(status_code=400, detail="email is required")

    try:
        contact = update_contact_email(db, ELEPHANT_EDGE_TENANT_ID, contact_id, email.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"id": contact.id, "email": contact.email, "email_source": contact.email_source}


@router.post("/gtm-os/messages/{message_draft_id}/review")
def post_gtm_os_message_review(message_draft_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """V2 human approval boundary (Phase 7, Part 8-10) -- the ONLY write route this phase adds.
    Reuses approve_message_draft()/reject_message_draft()/request_changes_message_draft()
    (Batch 7 + this phase's own additions, see message_draft.py) verbatim: this route is pure
    dispatch + validation, no review logic of its own. A rejected precondition (wrong status)
    raises ValueError from those functions, translated to 400 here -- never partially applied.

    DOES NOT EXECUTE ANYTHING (Part 10): no email/LinkedIn send, no CRM write, no campaign
    launch -- this function's only effect is a status/timestamp/note change on one MessageDraft
    row. Nothing downstream is triggered by any outcome.

    IDENTITY (Part 14): `reviewed_by` is REQUIRED in the payload and is trusted as supplied by
    the caller -- this backend has no independent user-identity channel to check it against. The
    gateway in front of this backend strips the session cookie before proxying and adds no
    identity header (confirmed by inspecting gateway/app/main.py's proxy route directly), so the
    only honest source for "who reviewed this" is the browser's own already-authenticated session
    (TenantContext's real user.email), passed through by the frontend. This is not a fabricated
    identity -- it's the real logged-in user, just supplied by the client because the backend has
    no other way to learn it. Documented here per Part 14's own explicit instruction, same
    limitation already named in the Phase 5 write routes: any authenticated user can perform this
    action, since no role/permission system exists anywhere in this app."""
    from app.gtm_os.learning.message_draft import (
        approve_message_draft,
        reject_message_draft,
        request_changes_message_draft,
    )

    action = payload.get("action")
    reviewed_by = payload.get("reviewed_by")
    note = payload.get("note")

    if action not in ("approve", "reject", "request_changes"):
        raise HTTPException(status_code=400, detail="action must be one of: approve, reject, request_changes")
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        raise HTTPException(status_code=400, detail="reviewed_by is required")

    handler = {
        "approve": lambda: approve_message_draft(db, ELEPHANT_EDGE_TENANT_ID, message_draft_id, reviewed_by),
        "reject": lambda: reject_message_draft(db, ELEPHANT_EDGE_TENANT_ID, message_draft_id, reviewed_by, note),
        "request_changes": lambda: request_changes_message_draft(db, ELEPHANT_EDGE_TENANT_ID, message_draft_id, reviewed_by, note),
    }[action]

    try:
        draft = handler()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "id": draft.id,
        "status": draft.status,
        "reviewed_at": draft.reviewed_at,
        "reviewed_by": draft.reviewed_by,
        "review_note": draft.review_note,
        "approved_at": draft.approved_at,
        "approved_by": draft.approved_by,
    }


@router.post("/gtm-os/messages/{message_draft_id}/regenerate")
def post_gtm_os_message_regenerate(message_draft_id: int, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """V2 Frontend Phase (Message Workspace) -- the real single-draft regeneration V2 never had.
    Thin dispatch over regenerate_message_draft() (message_draft.py), which itself is a controlled
    re-invocation of the existing, unmodified generate_message_draft() -- no new generation logic
    lives in this route. Optional `contact_id` re-targets the regenerated draft at a different
    eligible contact (validated against get_eligible_contacts() inside regenerate_message_draft()
    itself, never trusted blindly); omitted or null regenerates for the same contact/decision-maker
    resolution as before. Same real LLM call/cost profile as the existing generation path -- no
    new spend mechanism, no bulk/batch behavior, exactly one draft per call."""
    from app.gtm_os.learning.message_draft import regenerate_message_draft

    contact_id = payload.get("contact_id")

    try:
        draft = regenerate_message_draft(db, ELEPHANT_EDGE_TENANT_ID, message_draft_id, contact_id=contact_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "id": draft.id, "opportunity_id": draft.opportunity_id, "contact_id": draft.contact_id,
        "channel": draft.channel, "subject": draft.subject, "message_text": draft.message_text,
        "status": draft.status, "missing_information": draft.missing_information,
        "quality_gate_reasons": draft.quality_gate_reasons, "created_at": draft.created_at,
    }


@router.get("/gtm-os/opportunities/{opportunity_id}/eligible-contacts")
def get_gtm_os_opportunity_eligible_contacts(opportunity_id: int, db: Session = Depends(get_db)):
    """V2 Frontend Phase (Message Workspace) -- read-only wrapper over the existing
    get_eligible_contacts() (app/gtm_os/sales/contact_discovery.py), unmodified. Real suppression
    rule only (Contact.excluded_from_push) -- no new eligibility concept invented. Used by the
    Message Workspace's "change recipient" control so only real, non-suppressed contacts for this
    opportunity's company can ever be offered/selected."""
    from app.gtm_os.opportunity.opportunity import Opportunity
    from app.gtm_os.sales.contact_discovery import get_eligible_contacts

    opportunity = db.get(Opportunity, opportunity_id)
    if opportunity is None or opportunity.tenant_id != ELEPHANT_EDGE_TENANT_ID:
        raise HTTPException(status_code=404, detail=f"no Opportunity {opportunity_id}")
    if opportunity.company_id is None:
        return {"contacts": []}

    contacts = get_eligible_contacts(db, opportunity.company_id)
    return {
        "contacts": [
            {
                "id": c.id, "first_name": c.first_name, "last_name": c.last_name, "title": c.title,
                "email": c.email, "linkedin_url": c.linkedin_url,
            }
            for c in contacts
        ]
    }


# ---- V2 Efficiency (time-saved ledger) ----

@router.get("/gtm-os/efficiency")
def get_gtm_os_efficiency(month: str | None = None, db: Session = Depends(get_db)):
    """V2 Efficiency page -- read-only wrapper over get_monthly_efficiency()
    (app/gtm_os/efficiency/efficiency.py), a pure aggregation over EfficiencyActivityEvent (real
    recorded activity volume, instrumented at the actual pipeline call sites -- see
    app/phases/autonomous_orchestrator.py and app/main.py), MessageDraft (read directly, not
    duplicated), and AutonomousRun's own started_at/completed_at. Never fabricates "equivalent
    SDRs", "ran outside 9-5 %", or "admin time cut %" -- see that module's own docstring."""
    from app.gtm_os.efficiency.efficiency import get_monthly_efficiency

    return get_monthly_efficiency(db, ELEPHANT_EDGE_TENANT_ID, month=month)


@router.get("/gtm-os/efficiency-benchmarks")
def get_gtm_os_efficiency_benchmarks(db: Session = Depends(get_db)):
    """V2 Settings Efficiency/Time Benchmarks section -- read-only wrapper over
    get_efficiency_benchmarks() (app/gtm_os/efficiency/benchmark_config.py)."""
    from app.gtm_os.efficiency.benchmark_config import get_efficiency_benchmarks

    return {"benchmarks": get_efficiency_benchmarks(db, ELEPHANT_EDGE_TENANT_ID)}


@router.put("/gtm-os/efficiency-benchmarks")
def put_gtm_os_efficiency_benchmarks(benchmarks: list = Body(...), db: Session = Depends(get_db)):
    """Reuses set_efficiency_benchmarks() verbatim -- all validation lives there. Same
    authorization note as every other Settings/config write route in this file: session-cookie
    auth via the gateway's reverse proxy is the existing mechanism; no finer-grained role system
    exists in this codebase to gate on."""
    from app.gtm_os.efficiency.benchmark_config import EfficiencyBenchmarkConfigError, get_efficiency_benchmarks, set_efficiency_benchmarks

    try:
        set_efficiency_benchmarks(db, ELEPHANT_EDGE_TENANT_ID, benchmarks)
    except EfficiencyBenchmarkConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"benchmarks": get_efficiency_benchmarks(db, ELEPHANT_EDGE_TENANT_ID)}


@router.get("/gtm-os/learning-readout")
def get_gtm_os_learning_readout(db: Session = Depends(get_db)):
    """V2 Settings Performance tab -- read-only wrapper over evaluate_learning_readout()
    (app/gtm_os/learning/evaluation.py). This is the SAME real message-funnel/reply-outcome/
    strategy-type readout governance.py's own snapshot already computes internally for Briefing
    -- never a second evaluation engine, just exposing the existing one directly so Settings can
    show real operational counts instead of an unavailable state. No accuracy/hit-rate/success
    percentage is computed here or in evaluation.py itself -- see that module's own docstring for
    why none is defensible yet."""
    from app.gtm_os.learning.evaluation import evaluate_learning_readout

    return evaluate_learning_readout(db, ELEPHANT_EDGE_TENANT_ID)


@router.get("/gtm-os/jobs-to-be-done")
def get_gtm_os_jobs_to_be_done(db: Session = Depends(get_db)):
    """V2 Jobs to Be Done -- read-only composition of four existing, unmodified backend readers:
    get_next_execution_action() (execution_readiness.py), Company.hot_lead/hot_lead_reasoning
    (hot_leads.py), Company.decision_maker_searched_at + Contact, and GtmSignal/InterpretedSignal.
    See app/gtm_os/jobs/jobs_to_be_done.py for the full priority-ordering rationale. "calls_to_make"
    is always returned as an explicitly unavailable category -- no real qualifying data exists for
    it yet, and this route never fabricates one. Nothing here is persisted; every call re-derives
    the queue from current state."""
    from app.gtm_os.jobs.jobs_to_be_done import get_jobs_to_be_done

    return get_jobs_to_be_done(db, ELEPHANT_EDGE_TENANT_ID)


# ---- V2 Overrides & Evals (learning foundation) ----

@router.get("/gtm-os/overrides-evals")
def get_gtm_os_overrides_evals(month: str | None = None, db: Session = Depends(get_db)):
    """V2 Overrides & Evals -- read-only wrapper over get_overrides_evals()
    (app/gtm_os/learning/overrides_evals.py), a pure aggregation over MessageDraft's own review
    lifecycle and CalendarBooking's own outcome fields (both unmodified, no second review/outcome
    engine). Candidate patterns are computed fresh on every call, never persisted -- only a
    ConfirmedPattern row (created solely via the confirm/dismiss routes below) is durable. No
    "accuracy %" is fabricated -- see that module's own docstring for why none is defensible yet."""
    from app.gtm_os.learning.overrides_evals import get_overrides_evals

    return get_overrides_evals(db, ELEPHANT_EDGE_TENANT_ID, month=month)


@router.post("/gtm-os/patterns/{category}/confirm")
def post_gtm_os_pattern_confirm(category: str, body: dict = Body(default={}), db: Session = Depends(get_db)):
    """Human confirmation of a candidate pattern -- keyed by category (not a numeric id) because
    a candidate is not a persisted row until this action (or dismiss, below) creates one; see
    overrides_evals.py's own docstring for why. Writes ONLY to confirmed_patterns -- never
    touches ICP/offering/GTM-motion config, GtmStrategy, MessageDraft, the revenue goal, or any
    autonomous-run behavior (Part 6/9's explicit boundary). `confirmed_by` is supplied by the
    frontend from the real, already-authenticated user, same pattern as every other reviewer-
    identity field in this backend."""
    from app.gtm_os.learning.overrides_evals import confirm_pattern

    try:
        row = confirm_pattern(db, ELEPHANT_EDGE_TENANT_ID, category, body.get("confirmed_by"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "id": row.id, "category": row.category, "status": row.status,
        "trigger_description": row.trigger_description, "pattern_description": row.pattern_description,
        "source_event_refs": row.source_event_refs, "confirmed_by": row.confirmed_by, "confirmed_at": row.confirmed_at,
    }


@router.post("/gtm-os/patterns/{category}/dismiss")
def post_gtm_os_pattern_dismiss(category: str, body: dict = Body(default={}), db: Session = Depends(get_db)):
    """Human dismissal of a candidate pattern -- same write-boundary as confirm above (only ever
    writes to confirmed_patterns)."""
    from app.gtm_os.learning.overrides_evals import dismiss_pattern

    try:
        row = dismiss_pattern(db, ELEPHANT_EDGE_TENANT_ID, category, body.get("confirmed_by"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "id": row.id, "category": row.category, "status": row.status,
        "trigger_description": row.trigger_description, "pattern_description": row.pattern_description,
        "source_event_refs": row.source_event_refs, "confirmed_by": row.confirmed_by, "confirmed_at": row.confirmed_at,
    }


@router.get("/gtm-os/accounts/{company_id}/icp-candidates")
def get_gtm_os_icp_candidates(company_id: int, db: Session = Depends(get_db)):
    """Cross-ICP matching (architecture upgrade, Part 3) -- read-only wrapper over
    get_icp_candidates_for_company() (app/gtm_os/icp/icp_candidates.py), itself a pure
    composition over evaluate_icp_matches_for_company() (Batch 8, unmodified). Distinguishes
    primary vs alternative confirmed ICP matches vs insufficient-evidence candidates -- never a
    second ICP engine."""
    from app.gtm_os.icp.icp_candidates import get_icp_candidates_for_company

    return get_icp_candidates_for_company(db, ELEPHANT_EDGE_TENANT_ID, company_id)


@router.get("/gtm-os/accounts/{company_id}/offering-recommendation")
def get_gtm_os_offering_recommendation(company_id: int, db: Session = Depends(get_db)):
    """Offering-recommendation-follows-the-problem (architecture upgrade, Part 4) -- read-only
    wrapper over get_offering_recommendation_for_company() (app/gtm_os/opportunity/
    offering_recommendation.py), a composition over match_offerings_for_company() and
    match_offerings() (both unmodified) -- never a third offering matcher."""
    from app.gtm_os.opportunity.offering_recommendation import get_offering_recommendation_for_company

    return get_offering_recommendation_for_company(db, ELEPHANT_EDGE_TENANT_ID, company_id)


# ---- V2 Human-Provided Knowledge (architecture upgrade, Parts 6-9) ----
# GOVERNANCE: submit/confirm/dismiss below write ONLY to the human_knowledge table -- never to
# ICP/offering/GTM-motion config, the revenue goal, or any other authoritative configuration (see
# human_knowledge.py's own module docstring for the full boundary).

@router.get("/gtm-os/knowledge")
def get_gtm_os_knowledge(status: str | None = None, db: Session = Depends(get_db)):
    from app.gtm_os.learning.human_knowledge import list_human_knowledge

    return {"items": list_human_knowledge(db, ELEPHANT_EDGE_TENANT_ID, status=status)}


@router.post("/gtm-os/knowledge")
def post_gtm_os_knowledge(body: dict = Body(...), db: Session = Depends(get_db)):
    """Submits one free-text human observation. Makes exactly one bounded LLM call to interpret
    it into an inspectable structure (see human_knowledge.py) -- never loops, never chains
    further calls, and never fails to persist original_text even if interpretation fails."""
    from app.gtm_os.learning.human_knowledge import submit_human_knowledge

    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="text is required")

    return submit_human_knowledge(db, ELEPHANT_EDGE_TENANT_ID, text, body.get("created_by"))


@router.post("/gtm-os/knowledge/{knowledge_id}/confirm")
def post_gtm_os_knowledge_confirm(knowledge_id: int, body: dict = Body(default={}), db: Session = Depends(get_db)):
    from app.gtm_os.learning.human_knowledge import confirm_human_knowledge

    try:
        return confirm_human_knowledge(db, ELEPHANT_EDGE_TENANT_ID, knowledge_id, body.get("confirmed_by"))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/gtm-os/knowledge/{knowledge_id}/dismiss")
def post_gtm_os_knowledge_dismiss(knowledge_id: int, body: dict = Body(default={}), db: Session = Depends(get_db)):
    from app.gtm_os.learning.human_knowledge import dismiss_human_knowledge

    try:
        return dismiss_human_knowledge(db, ELEPHANT_EDGE_TENANT_ID, knowledge_id, body.get("confirmed_by"))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---- GTM-OS Intelligence Runs (end-to-end autonomous pipeline observability) ----

@router.get("/gtm-os/intelligence-runs")
def list_gtm_intelligence_runs(limit: int = 20, db: Session = Depends(get_db)):
    """Real, persisted run history for the hourly GTM-OS sweep (app/gtm_os/orchestration/
    sweep.py) -- "what happened during the last run" as a queryable table, not just a log line.
    Read-only."""
    from app.gtm_os.orchestration.sweep import GtmIntelligenceRun

    runs = (
        db.query(GtmIntelligenceRun)
        .filter(GtmIntelligenceRun.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .order_by(GtmIntelligenceRun.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id, "status": r.status, "stage_results": r.stage_results, "error_summary": r.error_summary,
            "started_at": r.started_at, "completed_at": r.completed_at,
        }
        for r in runs
    ]


@router.post("/gtm-os/intelligence-runs/trigger")
def trigger_gtm_intelligence_run(dry_run: bool = False, db: Session = Depends(get_db)):
    """Manual trigger -- the scheduled cycle runs automatically once daily, at a fixed configurable
    UTC time (control.py's get_intelligence_schedule_utc); this lets a run be checked immediately
    instead of waiting for the next scheduled occurrence -- same exact path as the scheduled tick,
    no separate "test run" implementation (see body below: identical run_gtm_intelligence_sweep()
    call, identical concurrency/stale-run/control-plane guards). Same persisted run-record as the
    scheduled path (unless dry_run=True, which persists nothing, matching
    run_gtm_intelligence_sweep's own dry_run contract).

    V2 CONTROL PLANE (Phase 0): dry_run is always allowed (introspection only, no writes/external
    calls -- useful for checking what a sweep WOULD do while paused). A real run is blocked when
    the control plane isn't "running", same as the scheduled tick.

    Autonomous Sensing Phase S7 -- now also respects the same single-concurrency guard as the
    scheduled tick (_scheduled_gtm_intelligence_cycle, app/main.py): recovers any stale "running"
    run first, then refuses to start a second overlapping run rather than silently double-running
    the sweep. Mirrors V1's manual "trigger-now" route (autonomous_orchestrator.py), which the
    daily cycle's own concurrency guard already covers identically."""
    from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run
    from app.gtm_os.orchestration.sweep import (
        GtmIntelligenceRun, finish_gtm_intelligence_run, recover_stale_gtm_intelligence_runs,
        run_gtm_intelligence_sweep, start_gtm_intelligence_run,
    )

    if dry_run:
        return run_gtm_intelligence_sweep(db, ELEPHANT_EDGE_TENANT_ID, dry_run=True)

    try:
        check_can_run(db, ELEPHANT_EDGE_TENANT_ID)
    except ControlPlaneHalted as e:
        raise HTTPException(status_code=409, detail=str(e))

    recover_stale_gtm_intelligence_runs(db, ELEPHANT_EDGE_TENANT_ID)
    already_running = (
        db.query(GtmIntelligenceRun)
        .filter(GtmIntelligenceRun.tenant_id == ELEPHANT_EDGE_TENANT_ID, GtmIntelligenceRun.status == "running")
        .first()
    )
    if already_running is not None:
        raise HTTPException(status_code=409, detail=f"a run ({already_running.id}) is already in progress since {already_running.started_at}")

    run = start_gtm_intelligence_run(db, ELEPHANT_EDGE_TENANT_ID)
    result = run_gtm_intelligence_sweep(db, ELEPHANT_EDGE_TENANT_ID)
    finish_gtm_intelligence_run(db, run, result)
    return {"run_id": run.id, "status": run.status, "result": result}


@router.get("/gtm-os/pattern-detection-config")
def get_gtm_os_pattern_detection_config(db: Session = Depends(get_db)):
    """Detection sensitivity config (min_occurrences, lookback_days) -- Parameter-backed, same
    pattern as every other V2 config."""
    from app.gtm_os.learning.pattern_detection_config import get_pattern_detection_config

    return get_pattern_detection_config(db, ELEPHANT_EDGE_TENANT_ID)


@router.put("/gtm-os/pattern-detection-config")
def put_gtm_os_pattern_detection_config(config: dict = Body(...), db: Session = Depends(get_db)):
    from app.gtm_os.learning.pattern_detection_config import PatternDetectionConfigError, get_pattern_detection_config, set_pattern_detection_config

    try:
        set_pattern_detection_config(db, ELEPHANT_EDGE_TENANT_ID, config)
    except PatternDetectionConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return get_pattern_detection_config(db, ELEPHANT_EDGE_TENANT_ID)


@router.get("/gtm-os/control")
def get_gtm_os_control(db: Session = Depends(get_db)):
    """V2 control plane config (Phase 0) -- state (running/paused/stopped, doubling as the kill
    switch), safety limits, business-hours window, discovery cadence/target, retry policy.
    Parameter-backed, same pattern as every other V2 config."""
    from app.gtm_os.orchestration.control import get_control_config

    return get_control_config(db, ELEPHANT_EDGE_TENANT_ID)


@router.put("/gtm-os/control")
def put_gtm_os_control(config: dict = Body(...), db: Session = Depends(get_db)):
    from app.gtm_os.orchestration.control import ControlPlaneConfigError, get_control_config, set_control_config

    try:
        set_control_config(db, ELEPHANT_EDGE_TENANT_ID, config)
    except ControlPlaneConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return get_control_config(db, ELEPHANT_EDGE_TENANT_ID)


@router.get("/gtm-os/control/status")
def get_gtm_os_control_status(db: Session = Depends(get_db)):
    """Current control-plane state plus the latest GtmIntelligenceRun (status/failure state) --
    reuses GtmIntelligenceRun as-is, no new run-tracking table."""
    from app.gtm_os.orchestration.control import get_control_status

    return get_control_status(db, ELEPHANT_EDGE_TENANT_ID)


@router.get("/gtm-os/intelligence-schedule")
def get_gtm_os_intelligence_schedule(db: Session = Depends(get_db)):
    """Fixed daily UTC time the sensing cycle fires at -- was a hardcoded hourly interval until
    2026-08-23, changed to once-daily to match V1's autonomous cycle's own cadence/pattern."""
    from app.gtm_os.orchestration.control import get_intelligence_schedule_utc

    hour, minute = get_intelligence_schedule_utc(db, ELEPHANT_EDGE_TENANT_ID)
    return {"hour": hour, "minute": minute}


@router.put("/gtm-os/intelligence-schedule")
def put_gtm_os_intelligence_schedule(body: dict = Body(...), db: Session = Depends(get_db)):
    """Applies immediately (no redeploy needed) -- reschedule_gtm_intelligence_job calls
    scheduler.reschedule_job on the live APScheduler instance, same pattern as
    /autonomous/schedule. Import deferred to avoid a circular import (main.py imports this
    router; this route needs something back from main.py)."""
    from app.gtm_os.orchestration.control import ControlPlaneConfigError, set_intelligence_schedule_utc

    hour, minute = body.get("hour"), body.get("minute")
    try:
        set_intelligence_schedule_utc(db, ELEPHANT_EDGE_TENANT_ID, hour, minute)
    except ControlPlaneConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))

    from app.main import reschedule_gtm_intelligence_job
    reschedule_gtm_intelligence_job(hour, minute)
    return {"hour": hour, "minute": minute}


# ---- Inbound Data reporting -- Google Analytics (traffic/channels) + Search Console (SEO/
# search performance), live-queried on each request (no snapshot storage yet -- these are cheap,
# infrequent, human-viewed reads, not something a background job needs to pre-compute). A third
# source (inbound leads/forms) is still being scoped and not wired up yet. See
# app/google_analytics_client.py and app/google_search_console_client.py -- both explicitly kept
# separate from the existing Google Calendar integration.

@router.get("/inbound/analytics/overview")
def get_inbound_analytics_overview(start_date: str = "7daysAgo", end_date: str = "today", db: Session = Depends(get_db)):
    from app.google_analytics_client import GoogleAnalyticsError, get_traffic_overview
    try:
        return get_traffic_overview(db, ELEPHANT_EDGE_TENANT_ID, start_date, end_date)
    except GoogleAnalyticsError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/inbound/analytics/top-pages")
def get_inbound_analytics_top_pages(start_date: str = "7daysAgo", end_date: str = "today", limit: int = 10, db: Session = Depends(get_db)):
    from app.google_analytics_client import GoogleAnalyticsError, get_top_pages
    try:
        return get_top_pages(db, ELEPHANT_EDGE_TENANT_ID, start_date, end_date, limit)
    except GoogleAnalyticsError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/inbound/analytics/trend")
def get_inbound_analytics_trend(start_date: str = "30daysAgo", end_date: str = "today", db: Session = Depends(get_db)):
    from app.google_analytics_client import GoogleAnalyticsError, get_daily_trend
    try:
        return get_daily_trend(db, ELEPHANT_EDGE_TENANT_ID, start_date, end_date)
    except GoogleAnalyticsError as e:
        raise HTTPException(status_code=502, detail=str(e))


def _default_search_console_dates() -> tuple[str, str]:
    """Search Console data has a real ~2-3 day reporting lag (Google's own documented
    behavior, not a bug here) -- "today" isn't populated yet, so the default end date is 3 days
    back, not today, to avoid every default-range query silently showing a truncated last few
    days as if they were complete."""
    from datetime import datetime, timedelta
    end = datetime.utcnow().date() - timedelta(days=3)
    start = end - timedelta(days=28)
    return start.isoformat(), end.isoformat()


@router.get("/inbound/search-console/top-queries")
def get_inbound_search_console_top_queries(start_date: str | None = None, end_date: str | None = None, limit: int = 20, db: Session = Depends(get_db)):
    from app.google_search_console_client import GoogleSearchConsoleError, get_top_queries
    default_start, default_end = _default_search_console_dates()
    try:
        return get_top_queries(db, ELEPHANT_EDGE_TENANT_ID, start_date or default_start, end_date or default_end, limit)
    except GoogleSearchConsoleError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/inbound/search-console/top-pages")
def get_inbound_search_console_top_pages(start_date: str | None = None, end_date: str | None = None, limit: int = 20, db: Session = Depends(get_db)):
    from app.google_search_console_client import GoogleSearchConsoleError, get_top_pages
    default_start, default_end = _default_search_console_dates()
    try:
        return get_top_pages(db, ELEPHANT_EDGE_TENANT_ID, start_date or default_start, end_date or default_end, limit)
    except GoogleSearchConsoleError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/inbound/search-console/trend")
def get_inbound_search_console_trend(start_date: str | None = None, end_date: str | None = None, db: Session = Depends(get_db)):
    from app.google_search_console_client import GoogleSearchConsoleError, get_daily_trend
    default_start, default_end = _default_search_console_dates()
    try:
        return get_daily_trend(db, ELEPHANT_EDGE_TENANT_ID, start_date or default_start, end_date or default_end)
    except GoogleSearchConsoleError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Website visitor identification (company-level only -- see
# app/website_visitor_tracking.py's module docstring for why this can never be person-level).
# /inbound/visitor-ping is deliberately PUBLIC (no tenant-slug prefix, no auth) -- it's called
# directly by a tracking snippet embedded in the public marketing site (hosted separately on
# Replit), which has no session/auth context of its own. It only ever accepts a beacon and
# returns a minimal ack; it never returns anything the caller doesn't already know.

_VISITOR_PING_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


@router.options("/inbound/visitor-ping")
async def options_inbound_visitor_ping():
    """CORS preflight -- handled explicitly here rather than widening the app-wide
    CORSMiddleware (app/main.py), which is scoped to the dashboard's own origin. This one route
    is meant to be called cross-origin from the public marketing site, and returns nothing
    sensitive either way, so a wildcard origin is safe here without loosening it elsewhere."""
    return Response(status_code=200, headers=_VISITOR_PING_CORS_HEADERS)


@router.post("/inbound/visitor-ping")
async def post_inbound_visitor_ping(request: Request, body: dict = Body(default={}), db: Session = Depends(get_db)):
    from app.website_visitor_tracking import extract_client_ip, record_visit

    client_ip = request.client.host if request.client else None
    ip_address = extract_client_ip(dict(request.headers), client_ip)
    record_visit(
        db, ELEPHANT_EDGE_TENANT_ID, ip_address,
        page_path=body.get("page_path"), referrer=body.get("referrer"),
        user_agent=request.headers.get("user-agent"),
    )
    # Always 200/ok -- a beacon caller (the public website) never needs to handle a failure
    # case, and a resolution failure is already captured server-side via company_lookup_status.
    return Response(status_code=200, headers=_VISITOR_PING_CORS_HEADERS, content='{"ok": true}', media_type="application/json")


@router.get("/inbound/visitors")
def get_inbound_visitors(limit: int = 50, resolved_only: bool = True, db: Session = Depends(get_db)):
    from app.website_visitor_tracking import get_recent_visitors

    visitors = get_recent_visitors(db, ELEPHANT_EDGE_TENANT_ID, limit=limit, resolved_only=resolved_only)
    return [
        {
            "id": v.id, "ip_address": v.ip_address, "page_path": v.page_path, "referrer": v.referrer,
            "company_name": v.company_name, "company_domain": v.company_domain, "company_website": v.company_website,
            "company_industry": v.company_industry, "company_employee_range": v.company_employee_range,
            "company_city": v.company_city, "company_state": v.company_state, "company_country": v.company_country,
            "is_fuzzy_match": v.is_fuzzy_match, "company_lookup_status": v.company_lookup_status,
            "created_at": v.created_at,
        }
        for v in visitors
    ]


# ---- CRM (V2) -- reads, edits, and deletes real HubSpot data directly (not V1's push-only
# sync in app/phases/hubspot_sync.py, which this never touches). Curated property sets only --
# see app/hubspot_client.py's own module comment for why. Every route here talks to HubSpot
# live on every call; nothing is cached or mirrored locally.

@router.get("/crm/companies")
def list_crm_companies(limit: int = 25, after: str | None = None, search: str | None = None, db: Session = Depends(get_db)):
    from app.hubspot_client import HubSpotError, list_companies
    try:
        return list_companies(db, ELEPHANT_EDGE_TENANT_ID, limit=limit, after=after, search=search)
    except HubSpotError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/crm/contacts")
def list_crm_contacts(limit: int = 25, after: str | None = None, search: str | None = None, db: Session = Depends(get_db)):
    from app.hubspot_client import HubSpotError, list_contacts
    try:
        return list_contacts(db, ELEPHANT_EDGE_TENANT_ID, limit=limit, after=after, search=search)
    except HubSpotError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/crm/companies/{company_id}")
def get_crm_company(company_id: str, db: Session = Depends(get_db)):
    from app.hubspot_client import HubSpotError, get_company
    try:
        return get_company(company_id, db, ELEPHANT_EDGE_TENANT_ID)
    except HubSpotError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/crm/contacts/{contact_id}")
def get_crm_contact(contact_id: str, db: Session = Depends(get_db)):
    from app.hubspot_client import HubSpotError, get_contact
    try:
        return get_contact(contact_id, db, ELEPHANT_EDGE_TENANT_ID)
    except HubSpotError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.patch("/crm/companies/{company_id}")
def update_crm_company(company_id: str, body: dict = Body(...), db: Session = Depends(get_db)):
    from app.hubspot_client import HubSpotError, update_company
    try:
        return update_company(company_id, body, db, ELEPHANT_EDGE_TENANT_ID)
    except HubSpotError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.patch("/crm/contacts/{contact_id}")
def update_crm_contact(contact_id: str, body: dict = Body(...), db: Session = Depends(get_db)):
    from app.hubspot_client import HubSpotError, update_contact
    try:
        return update_contact(contact_id, body, db, ELEPHANT_EDGE_TENANT_ID)
    except HubSpotError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.delete("/crm/companies/{company_id}")
def delete_crm_company(company_id: str, db: Session = Depends(get_db)):
    from app.hubspot_client import HubSpotError, delete_company
    try:
        delete_company(company_id, db, ELEPHANT_EDGE_TENANT_ID)
    except HubSpotError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"deleted": True}


@router.delete("/crm/contacts/{contact_id}")
def delete_crm_contact(contact_id: str, db: Session = Depends(get_db)):
    from app.hubspot_client import HubSpotError, delete_contact
    try:
        delete_contact(contact_id, db, ELEPHANT_EDGE_TENANT_ID)
    except HubSpotError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"deleted": True}
