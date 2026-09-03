"""Fetch companies FOR A PARTNER, using their ICP, kept entirely out of Elephant Edge's pipeline.

WHY THIS IS SEPARATE. Elephant Edge's own flow is long -- discovery, sensing, interpretation,
hypotheses, opportunities, strategy, contacts, drafts, send. A partner does not need any of that.
They need a list of companies that fit their stated ICP. So this is deliberately short, closer to
V1: read their ICP, turn it into a search, run it, filter what comes back. No hypotheses, no
opportunities, no drafts, nothing that could ever put a partner's company into our outreach.

HOW ISOLATION IS ENFORCED -- read this before changing anything here. Every one of the ~45 places
in this codebase that selects companies does it through `Batch.tenant_id`, and every scheduled job
in app/main.py hardcodes ELEPHANT_EDGE_TENANT_ID. So a partner's companies living under their OWN
tenant_id are invisible to our pipeline by construction: no new exclusion filter to add, none to
forget, and no scheduled job will ever pick them up. The alternative -- a flag on Batch that 45
queries must each remember to exclude -- fails silently the first time someone forgets, and the
failure is a partner's company being messaged from Majji's LinkedIn.

ONE TENANT PER PARTNER, not one shared "partners" tenant. Discovery dedupes against
_existing_domains(tenant_id), so a shared tenant would stop Michele from ever discovering a company
Isabel already got -- wrong, since two partners can legitimately want the same account.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.apify_budget_guard import STATUS_ALLOWED as APIFY_BUDGET_ALLOWED, check_apify_budget
from app.apify_client import estimate_cost_usd
from app.db.models import Batch, Company, LinkedinMonitorProfile, Tenant
from app.gtm_os.icp.icp_matching import REVENUE_PER_EMPLOYEE_USD
from app.gtm_os.icp.revenue_estimation import estimate_company_revenue
from app.llm_client import generate_json
from app.phases.apify_discovery import APIFY_TITLE_SEARCH, run_apify_discovery
from app.phases.partner_icp import get_structured_icp

logger = logging.getLogger(__name__)

PARTNER_TENANT_PREFIX = "partner:"
PARTNER_BATCH_SOURCE = "partner_discovery"

# LinkedIn's job-search actor silently returns ZERO results (not an error) if any industry name
# contains a comma -- confirmed live in apify_discovery.py's own module docstring. Every mapped
# name is checked against this before it is used, because the failure mode is an empty result set
# that looks exactly like "no companies match", which would send us hunting the wrong problem.
INDUSTRY_MAP_PROMPT = """Map this ICP's industries to LinkedIn's own industry taxonomy, which is what
the job-search API filters on.

The ICP describes industries in the partner's own words:
{industries}

Full ICP context (use it to disambiguate, e.g. whether "payments" means fintech software or
payment hardware):
{context}

Return ONLY JSON: {{"linkedin_industries": ["...", "..."]}}

RULES:
- Use real LinkedIn industry names exactly as LinkedIn writes them.
- NEVER include a name containing a comma. LinkedIn has names like "Technology, Information and
  Internet" -- the API returns zero results if you use them, so choose a comma-free alternative
  such as "Software Development" instead.
- 3 to 8 names. Prefer precision over coverage: a wrong industry wastes the search.
- If the ICP is about physical/engineered products, use manufacturing industries, not software ones.
"""


def _partner_slug(name: str) -> str:
    return PARTNER_TENANT_PREFIX + re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def get_or_create_partner_tenant(db: Session, partner_name: str) -> Tenant:
    """The partner's own tenant. backend_url stays NULL -- these tenants exist purely as a data
    boundary and must never be routable by the gateway."""
    slug = _partner_slug(partner_name)
    tenant = db.query(Tenant).filter(Tenant.slug == slug).first()
    if tenant is None:
        tenant = Tenant(name=f"Partner — {partner_name.strip()}", slug=slug, backend_url=None)
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        logger.info("partner_pipeline: created tenant %s for %r", tenant.id, partner_name)
    return tenant


def map_icp_to_linkedin_industries(db: Session, tenant_id: int, icp: dict) -> tuple[list[str], list[str]]:
    """(usable_names, rejected_names). Rejected are returned rather than dropped so a dry run can
    show what was discarded and why -- a silently shortened industry list changes the search
    without anyone noticing."""
    industries = icp.get("industries") or []
    if not industries:
        return [], []
    try:
        result = generate_json(
            INDUSTRY_MAP_PROMPT.format(industries=json.dumps(industries), context=json.dumps(icp)[:2000]),
            db, tenant_id, max_tokens=500,
        )
    except Exception as e:  # noqa: BLE001 -- a failed mapping must not crash a dry run
        logger.warning("partner_pipeline: industry mapping failed -- %s", e)
        return [], []
    names = [n.strip() for n in (result.get("linkedin_industries") or []) if isinstance(n, str) and n.strip()]
    usable = [n for n in names if "," not in n]
    rejected = [n for n in names if "," in n]
    return usable[:8], rejected


def _geographies_to_locations(icp: dict) -> list[str]:
    """LinkedIn's locationSearch wants places, and ICPs say things like "Multi-region" or "Global"
    which are not places. Those become an empty list, which the caller reads as "no geographic
    restriction" rather than searching for a word that matches nothing."""
    NON_PLACES = {"multi-region", "global", "worldwide", "international", "emea", "apac", "anywhere", "remote"}
    out = []
    for g in icp.get("geographies") or []:
        if isinstance(g, str) and g.strip() and g.strip().lower() not in NON_PLACES:
            out.append(g.strip())
    return out


def build_discovery_plan(db: Session, tenant_id: int, partner_name: str, icp: dict, target: int = 10,
                         exclude_locations: list[str] | None = None) -> dict:
    """Everything the run WOULD do, priced, without doing it. The dry run exists because a search
    built from the wrong industry mapping returns nothing and still costs money."""
    industries, rejected = map_icp_to_linkedin_industries(db, tenant_id, icp)
    locations = _geographies_to_locations(icp)
    emp_min, emp_max = icp.get("employee_min"), icp.get("employee_max")
    derived_headcount = None

    # An ICP that states revenue but not headcount is common, and letting the actor fall back to
    # its default 25-50 band is actively harmful: Isabel's ICP is $20-150M, and a 25-50 person
    # company is nowhere near that. The whole search would return companies the revenue filter then
    # discards -- a wasted run, not merely a suboptimal one.
    #
    # The p25-p75 spread behind this ratio is roughly 2.5x, so the band is widened generously in
    # both directions: this decides what we SEARCH for, and the real revenue check happens after.
    if emp_min is None and emp_max is None:
        rev_lo, rev_hi = icp.get("revenue_min_usd"), icp.get("revenue_max_usd")
        if isinstance(rev_lo, int) or isinstance(rev_hi, int):
            if isinstance(rev_lo, int):
                emp_min = max(1, int(rev_lo / REVENUE_PER_EMPLOYEE_USD / 2))
            if isinstance(rev_hi, int):
                emp_max = int(rev_hi / REVENUE_PER_EMPLOYEE_USD * 2)
            derived_headcount = (
                f"ICP states no headcount, so {emp_min}-{emp_max} was derived from its "
                f"${rev_lo or 0:,}-${rev_hi or 0:,} revenue band at ~${REVENUE_PER_EMPLOYEE_USD:,}/employee "
                "(widened 2x each way for the real spread). Searching the actor's 25-50 default "
                "instead would have found companies far too small for this ICP."
            )

    # Sized to the ask, not to Elephant Edge's daily run. Observed keep rates on real runs: 31
    # postings -> 7 companies over a 7d window, 150 -> 10 over 6m (more duplicates). ~10x the
    # target is a realistic oversample for a small list and costs a third of the old floor.
    limit = max(25, min(target * 10, 150))
    warnings = []
    if not industries:
        warnings.append("No usable LinkedIn industries mapped -- the search would fall back to the default software set, which will not match this ICP.")
    if rejected:
        warnings.append(f"Dropped industry names containing a comma (the actor returns zero results for those): {rejected}")
    if not locations:
        warnings.append("No specific country in the ICP -- the search will not be geographically restricted, so results may be anywhere.")
    if derived_headcount:
        warnings.append(derived_headcount)
    elif emp_min is None and emp_max is None:
        warnings.append("No headcount band in the ICP and no revenue band to derive one from -- using the actor's default, which may not fit.")
    if icp.get("revenue_min_usd"):
        warnings.append(f"Revenue floor ${icp['revenue_min_usd']:,} cannot be searched (the job actor exposes no revenue filter) -- it is applied AFTER discovery, so expect fewer kept than found.")

    return {
        "partner": partner_name,
        "search": {
            "location_search": locations or None,
            "employee_min": emp_min, "employee_max": emp_max,
            "industry_filter": industries or None,
            "title_search": APIFY_TITLE_SEARCH,
            "time_range": "6m",
            "limit": limit,
        },
        "icp_filters_applied_after": {
            "revenue_usd": [icp.get("revenue_min_usd"), icp.get("revenue_max_usd")],
            "exclude_locations": exclude_locations or [],
            "exclusions": icp.get("exclusions") or [],
            "revenue_enrichment": "google AI overview -> deepline crustdata identify, run before filtering",
        },
        "estimated_max_cost_usd": round(estimate_cost_usd(limit), 3),
        "warnings": warnings,
    }


def run_partner_discovery(db: Session, partner_name: str, icp: dict | None = None,
                          target: int = 10, dry_run: bool = True, ee_tenant_id: int = 2,
                          exclude_locations: list[str] | None = None,
                          enrich_revenue: bool = True) -> dict:
    """Find companies for one partner, in their own tenant.

    icp overrides what we hold, so this works for someone with no GTM University profile at all --
    paste their ICP and run it. dry_run defaults to True: this spends real money, and a search
    built on a bad industry mapping costs the same as a good one.
    """
    profile = (
        db.query(LinkedinMonitorProfile)
        .filter(LinkedinMonitorProfile.tenant_id == ee_tenant_id,
                LinkedinMonitorProfile.name.ilike(f"%{partner_name.strip()}%"))
        .first()
    )
    resolved_icp = icp or (get_structured_icp(profile) if profile else None)
    if not resolved_icp:
        return {"status": "failed", "error": f"No ICP found for {partner_name!r}. Pass one via icp= to run anyway."}

    # The mapping LLM call is made against Elephant Edge's tenant -- it is our API key and our
    # cost, not something to attribute to a partner tenant that exists only as a data boundary.
    plan = build_discovery_plan(db, ee_tenant_id, partner_name, resolved_icp, target, exclude_locations)
    if dry_run:
        return {"status": "dry_run", "plan": plan, "icp_used": resolved_icp}

    budget = check_apify_budget(db, ee_tenant_id, plan["estimated_max_cost_usd"])
    if budget["status"] != APIFY_BUDGET_ALLOWED:
        return {"status": "blocked", "reason": budget["reason"], "plan": plan}

    tenant = get_or_create_partner_tenant(db, partner_name)
    batch = Batch(tenant_id=tenant.id, name=f"{partner_name} ICP — {datetime.utcnow():%Y-%m-%d %H:%M}",
                  source=PARTNER_BATCH_SOURCE, current_phase="signal_discovery")
    db.add(batch)
    db.commit()

    s = plan["search"]
    result = run_apify_discovery(
        batch.id, db, tenant.id, target=target, time_range=s["time_range"],
        location_search=s["location_search"], title_search=s["title_search"],
        employee_min=s["employee_min"], employee_max=s["employee_max"],
        industry_filter=s["industry_filter"], limit=s["limit"],
        budget_tenant_id=ee_tenant_id,  # our Apify account funds this, not the partner's tenant
    )

    companies = db.query(Company).filter(Company.batch_id == batch.id).all()

    # FIND the revenue before judging on it. The job actor exposes no revenue field, so without
    # this every company arrives with revenue null and a revenue-banded ICP can only ever pass
    # them on ignorance. estimate_company_revenue is the existing waterfall -- Google AI Overview
    # (~$0.0085/query) first, then Deepline's Crustdata identify -- and it no-ops on a company that
    # already has a figure, so this never re-pays for data we hold.
    revenue_enrichment = {"attempted": 0, "resolved": 0, "not_found": 0}
    if enrich_revenue and (resolved_icp.get("revenue_min_usd") or resolved_icp.get("revenue_max_usd")):
        for c in companies:
            if c.estimated_revenue_lower_usd or c.estimated_revenue_higher_usd:
                continue
            revenue_enrichment["attempted"] += 1
            try:
                r = estimate_company_revenue(db, ee_tenant_id, c)
                revenue_enrichment["resolved" if r.get("status") == "resolved" else "not_found"] += 1
            except Exception as e:  # noqa: BLE001 -- one company's lookup must not lose the run
                db.rollback()
                revenue_enrichment["not_found"] += 1
                logger.warning("partner_pipeline: revenue lookup failed for %r -- %s", c.name, e)

    # Post-filter on what the job search cannot express. Only a KNOWN violation removes a company:
    # a company we still could not price is kept and flagged, because absence of a figure is not
    # evidence of a bad fit and dropping unknowns would discard most of what we just paid for.
    lo, hi = resolved_icp.get("revenue_min_usd"), resolved_icp.get("revenue_max_usd")
    excl = [e.strip().lower() for e in (exclude_locations or []) if e and e.strip()]
    kept, dropped, needs_review = [], [], []
    for c in companies:
        loc = (c.location or "").lower()
        if excl and any(x in loc for x in excl):
            dropped.append((c.name, f"location excluded ({c.location})"))
            continue
        if isinstance(lo, int) and c.estimated_revenue_higher_usd and c.estimated_revenue_higher_usd < lo:
            dropped.append((c.name, f"revenue below ${lo:,}"))
            continue
        if isinstance(hi, int) and c.estimated_revenue_lower_usd and c.estimated_revenue_lower_usd > hi:
            dropped.append((c.name, f"revenue above ${hi:,}"))
            continue
        # A range that STRADDLES the ceiling is not a pass. Domaine came back as $100-250M against
        # Isabel's $150M ceiling and was kept, because its lower bound sat inside the band -- so a
        # company that may be $100M over her limit read as a clean match. Straddling is uncertainty,
        # and uncertainty about a hard boundary belongs in front of a human, not silently on the
        # "send" side of the list.
        if isinstance(hi, int) and c.estimated_revenue_higher_usd and c.estimated_revenue_higher_usd > hi:
            needs_review.append((c.name, f"revenue range ${(c.estimated_revenue_lower_usd or 0):,}-${c.estimated_revenue_higher_usd:,} straddles the ${hi:,} ceiling"))
            continue
        kept.append(c)

    return {
        "status": "succeeded",
        "partner": partner_name,
        "tenant_id": tenant.id,
        "batch_id": batch.id,
        "plan": plan,
        "discovery": {k: v for k, v in result.items() if k != "rejection_breakdown"},
        "revenue_enrichment": revenue_enrichment,
        "kept": [
            {"name": c.name, "domain": c.domain, "industry": c.industry,
             "employees": c.employee_count, "location": c.location,
             "revenue_usd": [c.estimated_revenue_lower_usd, c.estimated_revenue_higher_usd],
             "revenue_known": bool(c.estimated_revenue_lower_usd or c.estimated_revenue_higher_usd),
             "linkedin_url": c.linkedin_url}
            for c in kept
        ],
        "dropped_by_icp": dropped,
        "needs_review": needs_review,
    }
