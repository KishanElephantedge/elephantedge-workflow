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


def enforce_icp_on_companies(db: Session, tenant_id: int, companies: list, icp: dict,
                             exclude_locations: list[str] | None = None,
                             enrich_revenue: bool = True,
                             enrichment_tenant_id: int = 2) -> dict:
    """Enrich revenue where it is missing, then DELETE the companies this ICP's own revenue band
    and location exclusions rule out. Returns {kept, dropped, needs_review, revenue_enrichment}.

    Extracted from run_partner_discovery (2026-09-11) so the shared daily engine enforces a
    tenant's ICP through exactly this code rather than a second copy. The engine previously had no
    revenue enforcement at all, and the job search cannot express a revenue band -- confirmed live
    the same day: a 5-company engine run for Sandy Yu ($25-250M) returned Alteryx, AlphaSense and
    Meltwater, and one for Amy Phillips ($20-500M) returned Yamaha Motor USA, Daikin and Kia
    America. Every one of them passed the headcount band and is far outside the stated revenue
    band, because headcount understates revenue badly at the large end.

    enrichment_tenant_id is who pays for the revenue lookups -- Elephant Edge, whose Apify account
    every partner run borrows, not the partner tenant the companies belong to.

    FIND the revenue before judging on it: the job actor exposes no revenue field, so without
    enrichment every company arrives with revenue null and a revenue-banded ICP can only ever pass
    them on ignorance. estimate_company_revenue tries Google's AI Overview (Apify, ~$0.0085/query)
    before anything billed to Deepline, and no-ops on a company that already has a figure.

    Only a KNOWN violation removes a company: one we still could not price is kept, because
    absence of a figure is not evidence of a bad fit and dropping unknowns would discard most of
    what was just paid for.
    """
    revenue_enrichment = {"attempted": 0, "resolved": 0, "not_found": 0}
    lo, hi = icp.get("revenue_min_usd"), icp.get("revenue_max_usd")

    if enrich_revenue and (isinstance(lo, int) or isinstance(hi, int)):
        for c in companies:
            if c.estimated_revenue_lower_usd or c.estimated_revenue_higher_usd:
                continue
            revenue_enrichment["attempted"] += 1
            try:
                r = estimate_company_revenue(db, enrichment_tenant_id, c)
                revenue_enrichment["resolved" if r.get("status") == "resolved" else "not_found"] += 1
            except Exception as e:  # noqa: BLE001 -- one company's lookup must not lose the run
                db.rollback()
                revenue_enrichment["not_found"] += 1
                logger.warning("partner_pipeline: revenue lookup failed for %r -- %s", c.name, e)

    excl = [e.strip().lower() for e in (exclude_locations or []) if e and e.strip()]
    kept, dropped, dropped_companies, needs_review = [], [], [], []
    for c in companies:
        loc = (c.location or "").lower()
        if excl and any(x in loc for x in excl):
            dropped.append((c.name, f"location excluded ({c.location})"))
            dropped_companies.append(c)
            continue
        if isinstance(lo, int) and c.estimated_revenue_higher_usd and c.estimated_revenue_higher_usd < lo:
            dropped.append((c.name, f"revenue below ${lo:,}"))
            dropped_companies.append(c)
            continue
        if isinstance(hi, int) and c.estimated_revenue_lower_usd and c.estimated_revenue_lower_usd > hi:
            dropped.append((c.name, f"revenue above ${hi:,}"))
            dropped_companies.append(c)
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

    # Real bug fix (2026-09-09): dropped/kept used to be reflected only in the RETURN VALUE -- the
    # Company rows themselves stayed in the batch regardless, with no field anywhere to mark one
    # rejected. A partner's dashboard (GET /companies) reads Company rows directly by
    # tenant_id/batch_id with no other filter, so every "dropped" company kept showing up in their
    # real Accounts view as if it had passed. Deleting the actual rejected Company objects
    # (tracked directly, not re-matched by name) is what makes "dropped" actually mean dropped.
    for c in dropped_companies:
        db.delete(c)
    if dropped_companies:
        db.commit()

    return {"kept": kept, "dropped": dropped, "needs_review": needs_review,
            "revenue_enrichment": revenue_enrichment}


def headcount_band_for_partner_icp(icp: dict) -> tuple[int | None, int | None, str | None]:
    """(employee_min, employee_max, why_derived) for a partner ICP -- its own stated headcount when
    it has one, otherwise derived from its revenue band.

    Extracted from build_discovery_plan (2026-09-11) so the daily discovery engine derives a
    partner's search band through exactly this logic instead of its own copy. The reasoning below
    is specific to the PARTNER pipeline and deliberately differs from icp_matching's
    p25/p75 range check -- see that module's own note. The difference is not an inconsistency: a
    partner run applies a hard revenue post-filter to what it finds, so admitting under-revenue
    companies wastes the run, whereas Elephant Edge's own path verifies each company through
    icp_matching afterwards and can afford a wider net.

    An ICP that states revenue but not headcount is common, and letting the actor fall back to
    its default 25-50 band is actively harmful: Isabel's ICP is $20-150M, and a 25-50 person
    company is nowhere near that. The whole search would return companies the revenue filter then
    discards -- a wasted run, not merely a suboptimal one.

    ASYMMETRIC ON PURPOSE. Halving the floor as well as doubling the ceiling looked even-handed
    and was wrong: Isabel's $20M floor became 125 employees, and the run came back with three
    ~176-person companies earning $0.5-2.5M. A VC-funded startup can have 176 people and almost no
    revenue, so a low headcount floor admits exactly the companies a revenue floor exists to
    exclude -- 3 of 5 results were wasted on it. So the floor uses the median ratio straight (no
    widening) and only the ceiling is widened. Missing an unusually efficient company costs one
    candidate; admitting under-revenue ones costs most of the run.

    Returns (None, None, None) when the ICP states neither headcount nor revenue -- the caller
    decides what to do with that, because "we cannot size this ICP" is a real answer that must not
    be silently replaced with a default band belonging to someone else.
    """
    emp_min, emp_max = icp.get("employee_min"), icp.get("employee_max")
    if emp_min is not None or emp_max is not None:
        return emp_min, emp_max, None

    rev_lo, rev_hi = icp.get("revenue_min_usd"), icp.get("revenue_max_usd")
    if not isinstance(rev_lo, int) and not isinstance(rev_hi, int):
        return None, None, None

    if isinstance(rev_lo, int):
        emp_min = max(1, int(rev_lo / REVENUE_PER_EMPLOYEE_USD))
    if isinstance(rev_hi, int):
        emp_max = int(rev_hi / REVENUE_PER_EMPLOYEE_USD * 2)
    why = (
        f"ICP states no headcount, so {emp_min}-{emp_max} was derived from its "
        f"${rev_lo or 0:,}-${rev_hi or 0:,} revenue band at ~${REVENUE_PER_EMPLOYEE_USD:,}/employee "
        "(ceiling widened 2x for the real spread). Searching the actor's 25-50 default "
        "instead would have found companies far too small for this ICP."
    )
    return emp_min, emp_max, why


def build_discovery_plan(db: Session, tenant_id: int, partner_name: str, icp: dict, target: int = 10,
                         exclude_locations: list[str] | None = None,
                         title_search: list[str] | None = None) -> dict:
    """Everything the run WOULD do, priced, without doing it. The dry run exists because a search
    built from the wrong industry mapping returns nothing and still costs money.

    title_search overrides the hiring roles we search for, and defaults to APIFY_TITLE_SEARCH so
    nothing that already calls this changes. WHY IT NEEDS TO BE OVERRIDABLE: the default is
    Elephant Edge's own list of SDR/AE/VP Sales titles, which encodes OUR assumption that a
    company hiring sellers is a company with a sales problem. Some partners sell against a
    different problem entirely -- a channel consultant's ICP is "the partner program is
    underperforming", and the only observable proxy for that is a company hiring a
    channel/partnerships role, not a sales one. Leaving the titles hardcoded would silently run
    every partner's search against Elephant Edge's hypothesis and return plausible-looking
    companies that have nothing to do with what the partner sells."""
    industries, rejected = map_icp_to_linkedin_industries(db, tenant_id, icp)
    locations = _geographies_to_locations(icp)
    emp_min, emp_max, derived_headcount = headcount_band_for_partner_icp(icp)

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
            "title_search": list(title_search) if title_search else APIFY_TITLE_SEARCH,
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
                          enrich_revenue: bool = True,
                          title_search: list[str] | None = None) -> dict:
    """Find companies for one partner, in their own tenant.

    icp overrides what we hold, so this works for someone with no GTM University profile at all --
    paste their ICP and run it. dry_run defaults to True: this spends real money, and a search
    built on a bad industry mapping costs the same as a good one.

    title_search likewise overrides the hiring signal (see build_discovery_plan) and defaults to
    the existing SDR/AE/VP Sales list, so every current caller behaves exactly as before.
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
    plan = build_discovery_plan(db, ee_tenant_id, partner_name, resolved_icp, target, exclude_locations,
                                title_search=title_search)
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
    enforced = enforce_icp_on_companies(
        db, tenant.id, companies, resolved_icp,
        exclude_locations=exclude_locations, enrich_revenue=enrich_revenue,
        enrichment_tenant_id=ee_tenant_id,
    )
    kept = enforced["kept"]
    dropped = enforced["dropped"]
    needs_review = enforced["needs_review"]
    revenue_enrichment = enforced["revenue_enrichment"]

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
