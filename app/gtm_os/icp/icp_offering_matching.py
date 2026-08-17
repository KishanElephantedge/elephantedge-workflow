"""ICP x Offering Matching -- Batch 9. Connects Company -> ICPMatch (Batch 8) -> Offering,
answering: "given what we know about this company and the ICP it matches, which offerings are
appropriate, and what evidence supports that?"

NOT A SECOND OFFERING MATCHER: reuses the existing Parameter-backed offering configuration
(offering_config.py, unmodified storage mechanism, one field added: `applicable_icps`) rather
than inventing a new config system. Deliberately does NOT modify offering_matcher.py's own
match_offering()/match_offerings() (Batch 4/6/7's `affected_function`-vs-`target_functions`
matcher, which operates on an Opportunity, not a Company/ICPMatch) -- that matcher keeps working
exactly as before (verified by regression test). This module is a genuinely new, additional
matching path that Section 5 of the Batch 9 spec explicitly asks for: "this batch should improve
the matcher specifically through the new ICPMatch relationship," a different (and today, richer)
signal than `affected_function` alone.

ICP -> OFFERING MAPPING (transcribed from the lead's reference material, per "where the lead has
given an explicit rule, represent it" -- stored in offering_config.py's own
`_APPLICABLE_ICPS_BY_OFFERING`, not duplicated here):
    ICP 1 "Stuck in Sales"              -> Consulting, Workshop, Digital Playbook
    ICP 2 "Upgrading With AI"           -> Execution, Sales Products
    ICP 3 "Needs Fractional Leadership" -> deliberately left UNMAPPED

WHY ICP 3 IS LEFT UNMAPPED: the lead's reference names ICP 3's best-fit offering as "Fractional
Sales/GTM Leadership." The six EXISTING, canonical offering names (from business_context.py,
repeated verbatim across every batch since Batch 4) are Consulting/Execution/Workshop/Sales OS/
Sales Products/Digital Playbook -- none of which literally says "Fractional Leadership." The
closest candidate by name is "Sales OS" (the founder's own raw transcript describes it as "a
sales operating system for this business"), but that is this implementation's own guess, not a
confirmed 1:1 mapping -- forcing it would violate this batch's explicit "do not invent additional
qualification rules" instruction. Left unmapped (`applicable_icps: []` for "Sales OS") and
flagged prominently in the final report as a real, named ambiguity requiring lead confirmation,
rather than silently guessed.

Deterministic only -- no LLM, no embeddings (per scope boundary).

PERSISTENCE DECISION: NO new table. match_offerings_for_company() is a pure, read-only,
computed-on-demand function -- cheaply and fully derivable from ICPMatch (Batch 8, already
persisted) + offering config (Parameter, already persisted) every time, with no expensive
computation. Same "smallest structure" precedent as sales_agent.py (Batch 6, zero new
persistence) and trend_intelligence.py (zero new persistence) -- introducing a table here would
duplicate state that's already fully reconstructable from two existing, cheap-to-query sources."""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Batch, Company
from app.gtm_os.gtm_motion.gtm_motion_config import get_gtm_motion_config
from app.gtm_os.icp.icp_config import get_icp_config
from app.gtm_os.icp.icp_matching import ICPMatch
from app.gtm_os.opportunity.offering_config import MATCHING_RELEVANT_FIELDS, get_offering_config


def match_offerings_for_company(db: Session, tenant_id: int, company_id: int) -> dict:
    """Pure, read-only. Loads this company's current ICPMatch rows (Batch 8), then for each
    matched ICP, evaluates every offering whose `applicable_icps` includes that ICP. Never
    fabricates a match: an offering with zero `applicable_icps` configured anywhere is reported
    separately as unconfigured, never silently treated as excluded or matched."""
    icp_matches = (
        db.query(ICPMatch)
        .filter(ICPMatch.tenant_id == tenant_id, ICPMatch.company_id == company_id)
        .all()
    )

    icp_config_by_id = {icp["id"]: icp for icp in get_icp_config(db, tenant_id)}
    offerings = get_offering_config(db, tenant_id)

    unconfigured_offerings = [o["name"] for o in offerings if not o.get("applicable_icps")]

    if not icp_matches:
        return {
            "company_id": company_id,
            "icp_matches": [],
            "unconfigured_offerings": unconfigured_offerings,
            "best_fit": None,
            "best_fit_reason": "no ICPMatch rows exist for this company -- run the Batch 8 ICP matching sweep first",
        }

    icp_match_results = []
    all_candidate_offerings: dict[str, dict] = {}  # offering name -> first candidate_match result seen, for best_fit

    for icp_match in icp_matches:
        icp = icp_config_by_id.get(icp_match.icp_id)
        icp_name = icp["name"] if icp else icp_match.icp_id
        offering_results = []

        for offering in offerings:
            applicable_icps = offering.get("applicable_icps") or []
            if icp_match.icp_id not in applicable_icps:
                continue  # not associated with this ICP at all -- omitted, not reported as no_match (avoids noise, per spec's own example shape)

            exclusions = offering.get("exclusions") or []
            if icp_match.icp_id in exclusions:
                offering_results.append({
                    "offering": offering["name"],
                    "status": "excluded",
                    "reason": f"{icp_match.icp_id} is explicitly configured as an exclusion for {offering['name']!r}",
                    "supporting_evidence": [],
                })
                continue

            supporting_evidence = list(icp_match.reasons) + [
                f"trigger_evidence: {icp_match.trigger_evidence}",
            ]
            result = {
                "offering": offering["name"],
                "status": "candidate_match",
                "reason": f"{offering['name']!r} is configured as applicable for ICP {icp_match.icp_id!r} ({icp_name})",
                "supporting_evidence": supporting_evidence,
            }
            offering_results.append(result)
            all_candidate_offerings.setdefault(offering["name"], result)

        icp_match_results.append({
            "icp_id": icp_match.icp_id,
            "icp_name": icp_name,
            "offerings": offering_results,
        })

    if len(all_candidate_offerings) == 1:
        only_name = next(iter(all_candidate_offerings))
        best_fit = {"offering": only_name}
        best_fit_reason = "exactly one offering has a candidate_match across all matched ICPs -- no other configured offering competes"
    elif len(all_candidate_offerings) == 0:
        best_fit = None
        best_fit_reason = "no offering has a candidate_match for this company's matched ICP(s) -- either unconfigured or genuinely not applicable"
    else:
        best_fit = None
        best_fit_reason = (
            f"{len(all_candidate_offerings)} offerings ({sorted(all_candidate_offerings)}) are all candidate_match -- "
            "no configured precedence rule exists to pick one, so none is returned as a definitive best fit"
        )

    return {
        "company_id": company_id,
        "icp_matches": icp_match_results,
        "unconfigured_offerings": unconfigured_offerings,
        "best_fit": best_fit,
        "best_fit_reason": best_fit_reason,
    }


def run_offering_matching_sweep(db: Session, tenant_id: int, limit: int = 200, dry_run: bool = False) -> dict:
    """Bounded, standalone, tenant-scoped, read-only reporting sweep -- writes NOTHING regardless
    of `dry_run` (accepted purely for interface consistency with this feature's other sweeps, see
    module docstring's persistence decision). One company's failure never aborts the sweep. NOT
    wired into the scheduler (per scope boundary)."""
    counts = {
        "companies_evaluated": 0,
        "no_icp_match": 0,
        "has_candidate_offering": 0,
        "no_candidate_offering": 0,
        "failed": 0,
        "dry_run": dry_run,
    }

    company_ids = [
        row[0]
        for row in db.query(Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id)
        .order_by(Company.id)
        .limit(limit)
        .all()
    ]

    for company_id in company_ids:
        try:
            counts["companies_evaluated"] += 1
            result = match_offerings_for_company(db, tenant_id, company_id)
            if not result["icp_matches"]:
                counts["no_icp_match"] += 1
                continue
            has_candidate = any(
                any(o["status"] == "candidate_match" for o in icp_entry["offerings"])
                for icp_entry in result["icp_matches"]
            )
            if has_candidate:
                counts["has_candidate_offering"] += 1
            else:
                counts["no_candidate_offering"] += 1
        except Exception:  # noqa: BLE001 -- one company's failure must never block the others
            counts["failed"] += 1

    return counts


def get_demand_grid(db: Session, tenant_id: int) -> dict:
    """V2 Demand Grid page (Phase 4, Parts 7-10) -- an ICP x Offering AGGREGATE view, distinct
    from match_offerings_for_company()'s per-company view above (reused for its config-reading
    logic, never re-implemented). A cell's status is a pure function of the existing offering
    config (applicable_icps/exclusions) -- the same three facts icp_offering_matching.py's own
    per-company loop already checks, just not re-evaluated per company here:
        "excluded"     -- icp_id is in this offering's exclusions
        "applicable"   -- icp_id is in this offering's applicable_icps (and not excluded)
        "unconfigured" -- neither -- no relationship has been configured for this pair at all

    NEVER "applicable" -> "demand exists" (Part 8's own explicit warning): `matched_account_count`
    is a SEPARATE field, real evidence from ICPMatch (Batch 8), not a rephrasing of the
    configuration fact. A cell can be "applicable" with matched_account_count == 0 (configured,
    but no company has matched that ICP yet) -- that distinction is preserved, never collapsed.

    PERFORMANCE (Part 14): one GROUP BY query gets every ICP's matched-company count up front;
    per-cell status is then pure in-memory config lookup (no query at all) for every
    ICP x offering pair -- never a call to match_offerings_for_company() per company."""
    icps = get_icp_config(db, tenant_id)
    offerings = get_offering_config(db, tenant_id)
    icp_ids = [icp["id"] for icp in icps]

    matched_counts: dict[str, int] = {}
    if icp_ids:
        matched_counts = dict(
            db.query(ICPMatch.icp_id, func.count(ICPMatch.id))
            .filter(ICPMatch.tenant_id == tenant_id, ICPMatch.icp_id.in_(icp_ids))
            .group_by(ICPMatch.icp_id)
            .all()
        )

    rows = []
    for icp in icps:
        cells = []
        for offering in offerings:
            applicable_icps = offering.get("applicable_icps") or []
            exclusions = offering.get("exclusions") or []
            if icp["id"] in exclusions:
                status = "excluded"
            elif icp["id"] in applicable_icps:
                status = "applicable"
            else:
                status = "unconfigured"

            cells.append({
                "offering": offering["name"],
                "status": status,
                # Only a real, configured cell can meaningfully report account evidence -- an
                # unconfigured/excluded cell reporting a nonzero count would misleadingly imply
                # a relationship the config doesn't actually establish.
                "matched_account_count": matched_counts.get(icp["id"], 0) if status == "applicable" else 0,
            })
        rows.append({"icp_id": icp["id"], "icp_name": icp["name"], "cells": cells})

    return {
        "icps": [{"id": icp["id"], "name": icp["name"]} for icp in icps],
        "offerings": [o["name"] for o in offerings],
        "rows": rows,
    }


def get_demand_grid_companies(db: Session, tenant_id: int, icp_id: str, limit: int = 200) -> dict:
    """Demand Grid drill-down (Phase 4, Part 12) -- the real, persisted ICPMatch rows behind one
    grid row's matched_account_count. Bounded by `limit`, tenant-scoped, read-only. Offering is
    deliberately NOT a further filter here: for an "applicable" cell, offering-fit is a
    deterministic config fact of the ICP match alone (icp_offering_matching.py's own logic), so
    the same company list applies to every applicable offering in that ICP's row -- filtering by
    offering would not change which companies appear, only imply a distinction the data doesn't
    actually make."""
    matches = (
        db.query(ICPMatch)
        .filter(ICPMatch.tenant_id == tenant_id, ICPMatch.icp_id == icp_id)
        .order_by(ICPMatch.id.desc())
        .limit(limit)
        .all()
    )
    company_ids = [m.company_id for m in matches]
    companies_by_id = {
        c.id: c
        for c in db.query(Company).join(Batch, Company.batch_id == Batch.id).filter(Batch.tenant_id == tenant_id, Company.id.in_(company_ids)).all()
    } if company_ids else {}

    return {
        "icp_id": icp_id,
        "companies": [
            {
                "company_id": m.company_id,
                "company_name": companies_by_id[m.company_id].name if m.company_id in companies_by_id else None,
                "reasons": m.reasons,
                "evaluated_at": m.evaluated_at,
            }
            for m in matches
            if m.company_id in companies_by_id
        ],
    }


def get_icps_offerings_overview(db: Session, tenant_id: int) -> dict:
    """V2 ICPs & Offerings configuration page (Phase 5, Part 14) -- ONE combined read
    (spec's own "use the minimum number of endpoints necessary") over the three EXISTING
    Parameter-backed config readers (get_icp_config, get_offering_config,
    get_gtm_motion_config, all unmodified) plus get_demand_grid() (Batch 4/Phase 4, unmodified)
    so the matrix tab always reflects whatever config was just read -- no second matrix
    computation, no drift between what this page shows and what /v2/demand-grid shows.

    The only NEW computation here is two honest "configuration completeness" flags -- never a
    new business rule, just whether existing optional fields happen to be filled in:
        - ICP `is_complete`: revenue_min_usd, revenue_max_usd, and a non-empty
          trigger_hiring_roles are all present. (name/trigger_mode are already required by
          set_icp_config()'s own validation, so those two are never the missing piece.)
        - Offering `is_complete`: reuses MATCHING_RELEVANT_FIELDS (offering_config.py's own,
          already-established "enough to match" field list, imported not re-derived) -- true
          when at least one of those fields is non-empty, matching offering_matcher.py's own
          insufficient_offering_context check exactly.
    Also exposes each ICP's real matched-account count (ICPMatch, Batch 8) -- the SAME
    single GROUP BY query get_demand_grid() already runs, not a second per-ICP query loop."""
    icps = get_icp_config(db, tenant_id)
    offerings = get_offering_config(db, tenant_id)
    gtm_motions = get_gtm_motion_config(db, tenant_id)
    demand_grid = get_demand_grid(db, tenant_id)

    icp_ids = [icp["id"] for icp in icps]
    matched_counts: dict[str, int] = {}
    if icp_ids:
        matched_counts = dict(
            db.query(ICPMatch.icp_id, func.count(ICPMatch.id))
            .filter(ICPMatch.tenant_id == tenant_id, ICPMatch.icp_id.in_(icp_ids))
            .group_by(ICPMatch.icp_id)
            .all()
        )

    icps_out = []
    for icp in icps:
        is_complete = (
            icp.get("revenue_min_usd") is not None
            and icp.get("revenue_max_usd") is not None
            and bool(icp.get("trigger_hiring_roles"))
        )
        icps_out.append({**icp, "matched_account_count": matched_counts.get(icp["id"], 0), "is_complete": is_complete})

    offerings_out = []
    for offering in offerings:
        is_complete = any(offering.get(f) for f in MATCHING_RELEVANT_FIELDS)
        offerings_out.append({**offering, "is_complete": is_complete})

    return {
        "icps": icps_out,
        "offerings": offerings_out,
        "gtm_motions": gtm_motions,
        "demand_grid": demand_grid,
    }
