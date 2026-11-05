"""Runs the self-serve landing page's actual discovery step (2026-11-05, Majji's request): take
a visitor's inferred-or-uploaded ICP, find them up to 10 real companies to reach out to.

DELIBERATELY NOT A NEW PIPELINE. A real partner (Sandy, Nora, Majji, Angela) already gets
exactly this -- their own stored ICP, routed across whichever provider (Icypeas, Crustdata-v3)
actually covers it, qualified by an LLM, capped by a daily quota -- via
app/gtm_os/plays/icp_filters.py's run_icp_filters(). A visitor on this page is not a different
kind of search, just a one-off, anonymous partner. So this module's whole job is: give that
visitor a real (if throwaway) Tenant row, write their ICP into the SAME Parameter row a real
partner's ICP lives in, and call the SAME run_icp_filters(). No new discovery code, no
Crustdata-only shortcut -- whatever provider the router picks for a real partner with this ICP
shape is exactly what a visitor gets too.

WHY A DEDICATED TENANT PER SUBMISSION, NOT ONE SHARED "self_serve" TENANT. quota.py's own daily
target (DEFAULT_DAILY_TARGET = 10, used whenever a tenant has no explicit config) is PER TENANT
PER DAY. A shared tenant would mean the second visitor on the same day gets 0 companies because
the first one already spent today's quota -- not a self-serve demo failing to serve one visitor,
one serving nobody after the first. A dedicated tenant gets a fresh, unconfigured quota (so the
cap is exactly 10, the system's own default, with no bespoke config needed) and is cheap: it's
one row, never shown in any tenant listing (there isn't one in this codebase), and nothing else
in the product ever queries across all tenants.

COST. run_icp_filters' own default run_cap_usd=0.25 is kept as this module's default too --
every real partner run already spends under that ceiling, so a visitor's one-off run costs no
more than Elephant Edge already pays per partner run. Spend is reserved against
BILLING_TENANT_ID (Elephant Edge's own account), same as every partner run.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Batch, Company, Parameter, SelfServeSubmission, Tenant

logger = logging.getLogger(__name__)

DEFAULT_RESULT_LIMIT = 10


def new_token() -> str:
    return secrets.token_urlsafe(18)


def start_submission(db: Session, icp: dict, source: str, email: str | None = None,
                      website_url: str | None = None) -> SelfServeSubmission:
    """Records the ICP the visitor has seen and confirmed (or uploaded), before any spend --
    this is the "here's what we think you sell to, confirm?" step landing on something durable,
    not just held in the browser, so a visitor who submits an email later can be matched back to
    exactly the ICP they agreed to."""
    submission = SelfServeSubmission(token=new_token(), email=email, source=source,
                                     website_url=website_url, icp=icp, status="inferred")
    db.add(submission)
    db.commit()
    return submission


def _create_submission_tenant(db: Session, submission: SelfServeSubmission) -> Tenant:
    slug = f"self-serve-{uuid.uuid4().hex[:12]}"
    label = submission.website_url or f"template upload ({submission.email or 'no email'})"
    tenant = Tenant(name=f"Self-serve: {label}"[:255], slug=slug)
    db.add(tenant)
    db.commit()
    return tenant


def run_discovery(db: Session, submission: SelfServeSubmission, run_cap_usd: float = 0.25,
                  pages: int = 3) -> dict:
    """Confirms the email gate (no verification, per Majji -- just "did they type something"),
    spins up this submission's dedicated tenant, writes its ICP into the exact Parameter row a
    real partner's ICP lives in, runs the real routed pipeline, and leaves the submission row
    updated either way (done with a result, or failed with why) so a retry never silently re-runs
    a run that already spent money."""
    from app.phases.partner_icp import PARTNER_ICP_PARAMETER_KEY

    if not (submission.email or "").strip():
        raise ValueError("an email is required before running discovery")
    if submission.status == "done":
        return submission.result_summary or {}

    submission.status = "running"
    db.commit()

    try:
        tenant = _create_submission_tenant(db, submission)
        submission.tenant_id = tenant.id
        db.add(Parameter(tenant_id=tenant.id, key=PARTNER_ICP_PARAMETER_KEY, value=submission.icp,
                         description="Self-serve landing page: visitor-confirmed ICP"))
        db.commit()

        from app.gtm_os.plays.icp_filters import run_icp_filters

        run_result = run_icp_filters(db, tenant.id, pages=pages, run_cap_usd=run_cap_usd)
        companies = get_discovered_companies(db, tenant.id)

        summary = {
            "companies": companies,
            "company_count": len(companies),
            "provider": (run_result.get("routing") or {}).get("provider"),
            "spent_usd": run_result.get("spent_usd"),
            "status": run_result.get("status"),
        }
        submission.status = "done"
        submission.result_summary = summary
        submission.discovered_at = datetime.utcnow()
        db.commit()
        return summary
    except Exception as e:  # noqa: BLE001 -- a visitor must get a clear failure, not a 500 with no record
        logger.exception("self-serve discovery failed for submission %s", submission.id)
        submission.status = "failed"
        submission.error = str(e)[:2000]
        db.commit()
        raise


def get_discovered_companies(db: Session, tenant_id: int, limit: int = DEFAULT_RESULT_LIMIT) -> list[dict]:
    """The same 'real accounts, not rejects' filter app/routes/api.py's get_partner_accounts
    applies for a real partner's dashboard, trimmed to just the fields worth showing an anonymous
    visitor (no internal ids, no contact/outreach detail -- this page is a teaser, not the CRM)."""
    from app.gtm_os.plays.lead import STATE_REJECTED, GtmLead

    rejected_company_ids = (
        db.query(GtmLead.company_id)
        .filter(GtmLead.tenant_id == tenant_id, GtmLead.state == STATE_REJECTED, GtmLead.company_id.isnot(None))
    )
    companies = (
        db.query(Company).join(Batch).filter(Batch.tenant_id == tenant_id, Company.id.notin_(rejected_company_ids))
        .order_by(Company.created_at.desc()).limit(limit).all()
    )
    return [
        {
            "name": c.name,
            "domain": c.domain,
            "industry": c.industry,
            "employee_count": c.employee_count,
            "location": c.location,
            "linkedin_url": c.linkedin_url,
        }
        for c in companies
    ]
