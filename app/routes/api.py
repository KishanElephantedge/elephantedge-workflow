import json
from collections import Counter
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import func, or_

from app.cache import active_keys, bump_batch_version, cache_get, cache_set, get_batch_version, mark_active
from app.db.models import AutonomousRun, Batch, CalendarBooking, CampaignEvent, CampaignPush, Company, Contact, Credential, Notification, Parameter, PersonalizedMessage, Score
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
        "error_message": pm.error_message,
        "generated_at": pm.generated_at,
    }


class MessageEdit(BaseModel):
    generated_message: str | None = None
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
    if body.status is not None:
        if body.status not in ("draft", "approved", "rejected"):
            raise HTTPException(status_code=400, detail="status must be draft, approved, or rejected")
        pm.status = body.status
    db.commit()
    bump_batch_version(contact.company.batch_id)
    return {"contact_id": contact_id, "status": pm.status, "generated_message": pm.generated_message}


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


def _normalize_linkedin_url(url: str | None) -> str | None:
    """LinkedIn URLs come in inconsistently from every source we deal with (our own decision-
    maker search, SalesRobot's prospect records) -- with/without https://, with/without www.,
    with/without a trailing slash, mixed case. Matching on raw strings silently drops real
    matches. Normalizes to the bare "linkedin.com/in/handle" form for reliable comparison."""
    if not url:
        return None
    normalized = url.strip().lower()
    for prefix in ("https://", "http://"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    if normalized.startswith("www."):
        normalized = normalized[4:]
    return normalized.rstrip("/")


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
def list_companies(page: int = 1, page_size: int = 25, search: str = "", qualified: str = "", db: Session = Depends(get_db)):
    """Cross-batch company list -- previously the only way to see companies at all was per-
    batch (BatchDetail), with no single "everything we've researched" view. `qualified`
    ("true"/"false") filters by the exact same has_qualifying_hiring_signal check that's the
    real production gate before Decision Maker ever runs (see hiring_signal.py), so this
    matches production behavior rather than an approximated definition. That check has no SQL
    column of its own (it's role-is-set OR product-fit-categories-is-set), so it's applied
    Python-side after a bounded DB fetch -- acceptable at today's real company volume (low
    thousands), would need a computed/indexed column if that grows an order of magnitude."""
    query = (
        db.query(Company)
        .join(Batch)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
    )
    if search.strip():
        like = f"%{search.strip()}%"
        query = query.filter(or_(Company.name.ilike(like), Company.domain.ilike(like), Company.industry.ilike(like)))

    companies = query.order_by(Company.created_at.desc()).all()

    if qualified == "true":
        companies = [c for c in companies if _is_company_qualified(c)]
    elif qualified == "false":
        companies = [c for c in companies if not _is_company_qualified(c)]

    total = len(companies)
    page = max(page, 1)
    page_size = max(1, min(page_size, 100))
    page_items = companies[(page - 1) * page_size: page * page_size]

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
            }
            for c in page_items
        ],
    }


def _lead_dict(c: Contact, live: dict | None) -> dict:
    pm = c.personalized_message
    push = c.campaign_pushes[-1] if c.campaign_pushes else None
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
            "leads": [_lead_dict(c, live) for c, live in page_slice],
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
    leads = [_lead_dict(c, live_by_url.get(_normalize_linkedin_url(c.linkedin_url))) for c in contacts]
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
    misses prospects added directly in SalesRobot outside this app).

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
    }


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
def test_salesrobot_push(campaign_uuid: str, linkedin_url: str, personalized_message: str, first_name: str | None = None, connection_note: str | None = None, db: Session = Depends(get_db)):
    """One-off test utility -- pushes a single synthetic prospect to any campaign UUID with
    known customMap values, so we can confirm live whether a SalesRobot campaign's message
    templates actually use API-supplied custom field values (vs. SalesRobot's own "AI
    Variable" generation) before trusting the real pipeline's push. Not tied to a real
    Contact row -- doesn't touch CampaignPush/scoring/any real pipeline state.

    connection_note mirrors the real push path (app/outreach/salesrobot.py): a separate
    short field for the connection-request step, distinct from personalizedMessage (the
    full follow-up message) -- defaults to a generic line if not given, same shape as
    production, so this test reflects what the real system will actually do."""
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
    return {"sent_customMap": prospect["customMap"], "salesrobot_response": result}


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


# ---- In-app notifications -- see app/notifications.py for where these get created ----

@router.post("/_scratch/seed-test-notifications")
def _scratch_seed_test_notifications(db: Session = Depends(get_db)):
    """TEMPORARY -- for visually verifying the new notification bell/dropdown/page against
    real production data before real events start creating rows. Delete after use."""
    from app.notifications import create_notification
    create_notification(db, ELEPHANT_EDGE_TENANT_ID, "run_failed", "Run failed — test-batch", "A data provider rejected our API key.", severity="error")
    create_notification(db, ELEPHANT_EDGE_TENANT_ID, "decision_makers_found", "3 decision-maker(s) found — test-batch", "Across 5 companies checked. Review and approve in the dashboard.", severity="info")
    create_notification(db, ELEPHANT_EDGE_TENANT_ID, "meeting_booked", "Meeting booked — Jane Doe", "Scheduled for Aug 10, 2026 03:00 PM UTC.", severity="success")
    create_notification(db, ELEPHANT_EDGE_TENANT_ID, "outreach_pushed", "5 contact(s) pushed — test-batch", "0 failed, 1 skipped as already pushed.", severity="success")
    return {"seeded": 4}


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
