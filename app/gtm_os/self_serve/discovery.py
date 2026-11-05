"""Runs the self-serve landing page's actual discovery step (2026-11-05, Majji's request): take
a visitor's inferred-or-uploaded ICP, find them up to 10 real companies to reach out to.

COMPANIES ONLY -- explicit correction, 2026-11-05 (the user: "here we fetch only accounts the
companies... don't fetch the decision makers... that's the biggest costing for us"). The FIRST
version of this reused run_icp_filters() wholesale -- the same pipeline a real partner's daily
run uses -- which does three things this page never needed: (1) a free Jobo lookup plus, for
anything that misses, a PAID HarvestAPI decision-maker search per company, (2) an LLM qualify()
call per company, and (3) -- the real bug this surfaced -- a "pool-first" delivery step that
hands back already-cached companies from OTHER tenants' past runs, matched only on headcount/
revenue/geography, DELIBERATELY NOT INDUSTRY (see icp_filters.py's own sourcing/pool.py comment).
A visitor's inferred ICP here has no headcount/revenue bounds (a homepage rarely states them), so
pool matching had nothing real to filter on and handed back whatever was cached for unrelated
partners -- confirmed live: an ICP for "credit unions, banks" returned Netomi, Amperity, ChowNow,
none of them financial institutions. That was never "the provider gave wrong results" -- the
exact same rendered filters (icypeas_filters_for_icp/crustdata_filters_for_icp, unchanged below)
were never actually sent to the provider for that run; the pool answered instead.

THIS VERSION calls the provider directly with the fully rendered filters -- the same industry/
geography/headcount atoms a real partner's search enforces, reusing icypeas_filters_for_icp /
crustdata_filters_for_icp (pure, side-effect-free functions, safe to reuse) and the two FREE
local rejection checks (vendor/agency name match, government/education name-or-industry match).
No pool lookup, no decision-maker resolution of any kind (free or paid), no qualify(), no
Company/Contact/GtmLead rows written at all -- this is a one-off teaser list for an anonymous
visitor, not a CRM record a partner needs to see again, so there is nothing here that needs to
persist past the submission's own result_summary.

PROVIDER ROUTING, still not hardcoded to one provider: app/gtm_os/sourcing/planner.rank() scores
every registered provider against this exact ICP the same way a real partner's run does; this
just calls the winner's raw search tool directly instead of going through its full play adapter.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import SelfServeSubmission

logger = logging.getLogger(__name__)

BILLING_TENANT_ID = 2  # Elephant Edge's own account -- every paid call here is reserved against it
DEFAULT_RESULT_LIMIT = 10
DEFAULT_RUN_CAP_USD = 0.25  # same ceiling run_icp_filters() already defaults every partner run to


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


def search_companies_only(db: Session, icp: dict, limit: int = DEFAULT_RESULT_LIMIT,
                          run_cap_usd: float = DEFAULT_RUN_CAP_USD) -> dict:
    """Company search, nothing else. Picks the best-covering, healthy, registered provider for
    this ICP (same ranking a real partner run uses) and calls its raw search tool directly with
    the fully rendered filters -- no pool, no decision-maker lookup, no qualify. Falls to the
    next ranked provider only if the first returns nothing (never because it returned something
    we didn't love -- that would be second-guessing the filters we just asked it to enforce)."""
    from app.deepline_client import DeeplineError, DeeplineSpendBlocked, execute_tool
    from app.gtm_os.orchestration.control import ControlPlaneHalted, check_can_run
    from app.gtm_os.plays.icp_filters import (
        NON_BUYER_INDUSTRIES, _looks_like_a_vendor, _looks_like_government_or_education,
        _normalize_crustdata_row, crustdata_filters_for_icp, icypeas_filters_for_icp,
    )
    from app.gtm_os.sourcing.planner import rank
    from app.spend_ledger import spend_scope

    result = {"companies": [], "provider": None, "stopped": None, "spent_usd": 0.0}

    try:
        check_can_run(db, BILLING_TENANT_ID)
    except ControlPlaneHalted as e:
        result["stopped"] = f"paused: {e}"
        return result

    # REAL FIX, 2026-11-05: planner.rank() ties icypeas and crustdata-v3 at an equal coverage
    # score for an ICP neither has specific history on, and the tie-break (registration order)
    # put icypeas first -- confirmed live against Elephant Edge's own real ICP (industries: ai,
    # saas, cybersecurity, e-commerce, manufacturing, supply chain, logistics): Icypeas only
    # resolved 2 of 7 terms, and mapped the generic word "manufacturing" to a narrow, stale
    # learned value ("Motor Vehicle Manufacturing" -- a leftover mapping from some past
    # unrelated partner's run), silently dropping cybersecurity/e-commerce/supply chain/
    # logistics and returning a car dealership. Crustdata's taxonomy-category CONTAINS-match
    # degrades far more gracefully on a brand-new ICP shape it has no history with -- it mapped
    # "manufacturing" to a sensible broad set of real manufacturing sub-categories instead of
    # one wrong narrow value. A RECURRING daily partner run benefits from Icypeas' cheaper,
    # tuned-over-time exact-value search; a ONE-OFF self-serve visitor with zero learned history
    # needs the provider that doesn't gamble on a stale mapping -- so self-serve prefers
    # crustdata-v3 first when both are viable, falling back to icypeas only if crustdata fails
    # or returns nothing. This reorders ONLY this self-serve path; planner.rank()'s own output
    # (and every real partner's run through it) is untouched.
    ranked = [c for c in rank(db, icp) if c.healthy and c.provider in ("icypeas", "crustdata-v3")]
    provider_order = sorted((c.provider for c in ranked), key=lambda p: 0 if p == "crustdata-v3" else 1)
    if not provider_order:
        provider_order = ["crustdata-v3", "icypeas"]

    # Over-fetch a bit beyond `limit` so the LLM industry-fit pass below (which rejects real
    # mismatches) still leaves close to `limit` good rows rather than under-delivering.
    fetch_size = max(limit * 3, 15)

    with spend_scope(db, BILLING_TENANT_ID, f"self_serve:company_search", run_cap_usd=run_cap_usd) as scope:
        for provider in provider_order:
            try:
                if provider == "icypeas":
                    filters = icypeas_filters_for_icp(icp, db=db, tenant_id=BILLING_TENANT_ID)
                    response = execute_tool("icypeas_find_companies",
                                            {"query": filters, "pagination": {"size": fetch_size}})
                    raw = (response.get("toolResponse") or {}).get("raw") or {}
                    rows = raw.get("leads") or []
                else:
                    filters, _structured = crustdata_filters_for_icp(icp, db=db, tenant_id=BILLING_TENANT_ID)
                    response = execute_tool("crustdata_v3_company_search",
                                            {"filters": filters, "limit": fetch_size,
                                             "fields": ["basic_info", "headcount", "revenue", "locations",
                                                       "funding", "taxonomy"]})
                    raw = (response.get("toolResponse") or {}).get("raw") or {}
                    rows = [_normalize_crustdata_row(c) for c in (raw.get("companies") or [])]
            except DeeplineSpendBlocked as e:
                result["stopped"] = f"{provider}: budget blocked ({e})"
                break
            except DeeplineError as e:
                logger.warning("self-serve search failed on %s: %s", provider, e)
                result["stopped"] = f"{provider}: search failed ({e})"
                continue

            candidates = []
            for co in rows:
                name = co.get("name") or ""
                industry = co.get("industry")
                # REAL FIX, 2026-11-05: crustdata_filters_for_icp() has no NON_BUYER_INDUSTRIES
                # exclusion at all (confirmed by reading it -- unlike icypeas_filters_for_icp,
                # it never builds a query-level industry exclude), so a vendor/consultancy whose
                # NAME doesn't contain a telltale word (CES: "IT Services and IT Consulting",
                # 5632 employees) still slipped through _looks_like_a_vendor's name-only regex.
                # Checked here by industry label, provider-agnostic, so this protection applies
                # no matter which provider search_companies_only ends up using.
                if industry and industry in NON_BUYER_INDUSTRIES:
                    continue
                if _looks_like_a_vendor(name) or _looks_like_government_or_education(name, industry):
                    continue
                candidates.append({
                    "name": name,
                    "domain": co.get("website"),
                    "industry": industry,
                    "description": co.get("description"),
                    "employee_count": co.get("numberOfEmployees"),
                    "location": co.get("address"),
                    "linkedin_url": co.get("url") or None,
                })

            # REAL FIX, 2026-11-05: a taxonomy/keyword match on a niche industry phrase (e.g.
            # "credit unions, banks", which has no clean taxonomy value in either provider)
            # still let real mismatches through -- Fiem Industries (Motor Vehicle Manufacturing),
            # International Diamond Services (Oil and Gas) -- confirmed live even after the
            # NON_BUYER_INDUSTRIES fix above, which only catches vendor/agency categories, not
            # "genuinely the wrong industry." Same discipline icp_filters.py's search_crustdata
            # already uses for exactly this (verify_industry_fit_with_llm's own docstring:
            # "the keyword match itself is the weak signal, no amount of checking rows after
            # catches this") -- reused here rather than re-invented. Fails open on any judgment
            # outage (missing index = kept), never blocks a real run.
            industry_terms = icp.get("industries") or []
            if candidates and industry_terms:
                try:
                    from app.gtm_os.sourcing.verification import verify_industry_fit_with_llm

                    verdicts = verify_industry_fit_with_llm(db, candidates, industry_terms, notes=icp.get("notes"))
                    candidates = [c for i, c in enumerate(candidates) if verdicts.get(i, True) is not False]
                except Exception as e:  # noqa: BLE001 -- a judgment outage must never block a real run
                    logger.warning("self-serve industry-fit judgment skipped: %s: %s", type(e).__name__, e)

            companies = [{k: v for k, v in c.items() if k != "description"} for c in candidates[:limit]]

            if companies:
                result["companies"] = companies
                result["provider"] = provider
                result["stopped"] = None
                break
            result["stopped"] = result["stopped"] or f"{provider}: no companies matched these filters"

        result["spent_usd"] = round(scope.spent_usd, 4)

    return result


def run_discovery(db: Session, submission: SelfServeSubmission, limit: int = DEFAULT_RESULT_LIMIT,
                  run_cap_usd: float = DEFAULT_RUN_CAP_USD) -> dict:
    """Confirms the email gate (no verification, per Majji -- just "did they type something"),
    runs the companies-only search above against the visitor's confirmed ICP, and leaves the
    submission row updated either way (done with a result, or failed with why) so a retry never
    silently re-runs a run that already spent money."""
    if not (submission.email or "").strip():
        raise ValueError("an email is required before running discovery")
    if submission.status == "done":
        return submission.result_summary or {}

    submission.status = "running"
    db.commit()

    try:
        search_result = search_companies_only(db, submission.icp, limit=limit, run_cap_usd=run_cap_usd)
        summary = {
            "companies": search_result["companies"],
            "company_count": len(search_result["companies"]),
            "provider": search_result["provider"],
            "spent_usd": search_result["spent_usd"],
            "stopped": search_result["stopped"],
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
