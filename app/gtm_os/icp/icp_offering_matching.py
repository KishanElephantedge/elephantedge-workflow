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

ICP -> OFFERING MAPPING (approved by the lead, GTM-OS acceptance-test follow-up, stored in
offering_config.py's own `applicable_icps` per offering, not duplicated here):
    ICP 1 "Stuck in Sales"              -> Consulting, Workshop, Digital Playbook
    ICP 2 "Upgrading With AI"           -> Sales OS
    ICP 3 "Needs Fractional Leadership" -> Execution
    Sales Products                      -> deliberately left unmapped to any ICP for now (real,
                                            named open question -- see offering_config.py)

Superseded an earlier draft mapping (ICP 2 -> Execution/Sales Products, ICP 3 unmapped) that
guessed "Sales OS" might be ICP 3's fit purely from the word "Fractional" in ICP 3's name -- the
lead's own real product-pillar breakdown confirmed Sales OS (AI SDR/Sales Automation, "Agentic
layers for sales") is the ICP 2 fit (ICP 2's trigger is literally hiring a GTM-engineer-type
role), and Execution (Fractional VP Sales) is the ICP 3 fit (ICP 3's trigger is literally hiring a
VP/Head of Sales they'd prefer fractional) -- both confirmed by the lead, not re-guessed here.

Deterministic only -- no LLM, no embeddings (per scope boundary).

PERSISTENCE DECISION: NO new table. match_offerings_for_company() is a pure, read-only,
computed-on-demand function -- cheaply and fully derivable from ICPMatch (Batch 8, already
persisted) + offering config (Parameter, already persisted) every time, with no expensive
computation. Same "smallest structure" precedent as sales_agent.py (Batch 6, zero new
persistence) and trend_intelligence.py (zero new persistence) -- introducing a table here would
duplicate state that's already fully reconstructable from two existing, cheap-to-query sources."""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Batch, CalendarBooking, Company
from app.gtm_os.content.topic import ContentTopic
from app.gtm_os.content.trend_config import get_trend_config
from app.gtm_os.content.trend_intelligence import evaluate_topic_trend
from app.gtm_os.gtm_motion.gtm_motion_config import get_gtm_motion_config
from app.gtm_os.icp.icp_config import get_icp_config
from app.gtm_os.icp.icp_matching import ICPMatch
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.opportunity.offering_config import MATCHING_RELEVANT_FIELDS, get_offering_config
from app.gtm_os.opportunity.offering_matcher import match_offering, match_offerings
from app.gtm_os.opportunity.opportunity import Opportunity

# Trend states ranked strongest -> weakest, for picking the single best-matching topic's state as
# an offering's market_demand summary when multiple configured topics relate to it (Part 1's own
# "market demand exists" question -- never a numeric score, just the real evaluate_topic_trend()
# classification, reused unmodified from trend_intelligence.py).
_TREND_STRENGTH_ORDER = ["accelerating", "emerging", "persistent", "stable", "declining", "insufficient_evidence"]


def _topic_relates_to_offering(topic_terms: set[str], offering: dict) -> bool:
    """Deliberately simple, deterministic, inspectable keyword overlap -- NOT semantic/embedding
    matching (no such capability exists anywhere in this codebase, and Part 10 forbids adding
    unnecessary LLM calls for something a plain substring check can do honestly). No ICP/offering
    <-> ContentTopic association table exists yet (confirmed: ContentTopic has no offering_name/
    icp_id field) -- this is the documented, minimal foundation until a real tagging mechanism is
    built. A topic "relates" to an offering when the topic's own name/aliases share a word with
    the offering's name or configured target_functions -- real text, no invented taxonomy."""
    offering_terms = {w.lower() for w in offering.get("name", "").replace("-", " ").split()}
    for tf in offering.get("target_functions") or []:
        offering_terms.add(str(tf).lower())
    return bool(topic_terms & offering_terms)


def _market_demand_by_offering(db: Session, tenant_id: int, offerings: list[dict]) -> dict[str, dict]:
    """One evaluate_topic_trend() call per configured ContentTopic (small, tenant-owned set, same
    performance precedent as get_market_intelligence_overview()), then a pure in-memory keyword
    match against each offering -- never a query per offering."""
    topics = db.query(ContentTopic).filter(ContentTopic.tenant_id == tenant_id).all()
    if not topics:
        return {o["name"]: {"state": "no_related_topic", "relevant_signals": 0, "related_topics": []} for o in offerings}

    trend_config = get_trend_config(db, tenant_id)
    topic_trends = []
    for topic in topics:
        terms = {topic.canonical_name.lower()} | {a.lower() for a in (topic.aliases or [])}
        trend = evaluate_topic_trend(db, tenant_id, topic, trend_config)
        topic_trends.append((terms, topic.canonical_name, trend))

    result = {}
    for offering in offerings:
        matched = [(name, trend) for terms, name, trend in topic_trends if _topic_relates_to_offering(terms, offering)]
        if not matched:
            result[offering["name"]] = {"state": "no_related_topic", "relevant_signals": 0, "related_topics": []}
            continue
        best_state = min(
            (trend["state"] for _name, trend in matched),
            key=lambda s: _TREND_STRENGTH_ORDER.index(s) if s in _TREND_STRENGTH_ORDER else len(_TREND_STRENGTH_ORDER),
        )
        result[offering["name"]] = {
            "state": best_state,
            "relevant_signals": sum(trend["recent_observation_count"] for _name, trend in matched),
            "related_topics": [name for name, _trend in matched],
        }
    return result


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


def match_offerings_for_opportunity(db: Session, tenant_id: int, opportunity: Opportunity) -> list[dict]:
    """The real connection this batch's acceptance test found missing: strategy.py's
    generate_strategy_facts() was calling offering_matcher.py's match_offerings() directly --
    target_functions/exclusions only, blind to ICP applicability -- so a company clearly matched
    to one ICP could still get pitched an offering configured for a completely different ICP,
    just because it happened to sort first among target_functions-compatible offerings.

    Deliberately reads Opportunity.icp_context (the Batch 8/Phase 2 snapshot taken at Opportunity
    creation time) rather than re-querying live ICPMatch rows: Opportunity should retain the ICP
    reasoning state it was actually created from, strategy generation should be deterministic
    against that stored context (not silently drift if ICP config changes later), and this avoids
    a second live ICP evaluation on every strategy sweep. Genuinely NOT a third offering matcher --
    reuses match_offering() (offering_matcher.py, unmodified per-offering target_functions/
    exclusions logic) for every offering this function decides is even in play; the only new
    logic here is which offerings are in play at all.

    Multi-ICP aware: if icp_context.matches names more than one ICP, an offering is in play if
    its applicable_icps intersects ANY of them (a real, deterministic union -- no score, no
    precedence rule invented). Offering-config list order is preserved through filtering, so the
    existing "first candidate_match wins" best-fit behavior in generate_strategy_facts() is
    unchanged for however many offerings survive the ICP filter.

    Falls back to the plain, unfiltered match_offerings() when this Opportunity has no real ICP
    match recorded (icp_context missing, or has_icp_match is not True, or matches is empty) --
    ICP applicability is additive offering-selection input, never a hard gate on opportunities
    that never matched an ICP in the first place (those still deserve honest target_functions-only
    matching, not silent insufficient_offering_context)."""
    icp_context = opportunity.icp_context or {}
    matches = icp_context.get("matches") or []
    if not icp_context.get("has_icp_match") or not matches:
        return match_offerings(db, tenant_id, opportunity)

    matched_icp_ids = {m.get("icp_id") for m in matches if m.get("icp_id")}
    offerings = get_offering_config(db, tenant_id)
    icp_applicable_offerings = [o for o in offerings if matched_icp_ids & set(o.get("applicable_icps") or [])]

    # Same "omit, don't fabricate no_match" convention match_offerings_for_company() already
    # established for offerings with no relationship to the matched ICP(s) at all -- an empty
    # list here (this Opportunity's matched ICP(s) have zero offerings configured for them) flows
    # straight into generate_strategy_facts()'s existing "insufficient_offering_context" bucket,
    # an honest, already-existing status -- no new vocabulary invented for this case.
    return [match_offering(offering, opportunity) for offering in icp_applicable_offerings]


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
            # Roll back before anything else: a per-item failure must not poison the SHARED session.
            # Neon drops idle connections, and this sweep idles ~40s per LLM call under Gemini rate
            # limiting -- when that lands mid-loop the transaction is left invalid and EVERY later item
            # AND stage dies with PendingRollbackError. Confirmed live: run 122 lost its connection
            # during topic_linking, then burned 233 minutes failing everything after it. The stage-level
            # handler already rolled back; these per-item ones did not.
            db.rollback()
            counts["failed"] += 1

    return counts


def _opportunity_and_outcome_evidence(db: Session, tenant_id: int, icp_ids: list[str], matched_company_ids_by_icp: dict[str, set[int]]) -> tuple[dict, dict]:
    """Real commercial-demand evidence per (icp_id, offering) cell -- Part 1's "is it turning into
    opportunities / meetings / wins / revenue" question. Two bulk queries (never per-cell):
    every Opportunity for any matched company (then offering_matcher.py's own match_offerings(),
    unmodified, run once per opportunity -- real problem/demand alignment, not a new matcher), and
    every won/lost CalendarBooking outcome with an offering_name set (outcome_icp_snapshot is
    already a real, server-captured fact -- see revenue_pace.py)."""
    all_matched_company_ids = {cid for ids in matched_company_ids_by_icp.values() for cid in ids}
    opportunities_by_cell: dict[tuple[str, str], list[str]] = {}
    if all_matched_company_ids:
        opportunities = (
            db.query(Opportunity)
            .filter(Opportunity.tenant_id == tenant_id, Opportunity.company_id.in_(all_matched_company_ids))
            .all()
        )
        for opp in opportunities:
            candidate_offerings = {r["offering"] for r in match_offerings(db, tenant_id, opp) if r["status"] == "candidate_match"}
            for icp_id in icp_ids:
                if opp.company_id in matched_company_ids_by_icp.get(icp_id, ()):
                    for offering_name in candidate_offerings:
                        opportunities_by_cell.setdefault((icp_id, offering_name), []).append(opp.opportunity_statement)

    outcomes_by_cell: dict[tuple[str, str], dict] = {}
    bookings = (
        db.query(CalendarBooking)
        .join(Company, CalendarBooking.outcome_company_id == Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id, CalendarBooking.outcome_status.in_(("won", "lost")), CalendarBooking.outcome_offering_name.isnot(None))
        .all()
    )
    for booking in bookings:
        snapshot_icp_ids = {e.get("icp_id") for e in (booking.outcome_icp_snapshot or [])}
        for icp_id in snapshot_icp_ids & set(icp_ids):
            key = (icp_id, booking.outcome_offering_name)
            entry = outcomes_by_cell.setdefault(key, {"meetings": 0, "won": 0, "revenue_usd": 0})
            entry["meetings"] += 1
            if booking.outcome_status == "won":
                entry["won"] += 1
                entry["revenue_usd"] += booking.outcome_amount_usd or 0

    return opportunities_by_cell, outcomes_by_cell


def get_demand_grid(db: Session, tenant_id: int) -> dict:
    """V2 Demand Grid -- redefined (architecture upgrade, Parts 1-2) from a bare ICP x Offering
    APPLICABILITY matrix into a demand-INTELLIGENCE surface, answering "where is demand highest
    right now?" instead of just "what's configured?".

    `status`/`matched_account_count` are UNCHANGED (backward-compatible: get_icps_offerings_overview()
    and the V2 ICPs & Offerings config page both still read them exactly as before). Every
    "applicable" cell additionally carries a real `evidence` object distinguishing the four levels
    the spec asks for, NEVER collapsed into one number and never a fabricated score:
        market_demand         -- ContentTopic trend state (trend_intelligence.py, unmodified),
                                  keyword-matched to this offering; "no_related_topic" if none
                                  configured relates to it -- an honest absence, not a zero score.
        account_demand        -- matched_account_count, restated here for convenience (same real
                                  ICPMatch count, not a second computation) + real DemandHypothesis
                                  count among those same matched companies (evidence a specific
                                  account is outward-looking, not just ICP-shaped).
        opportunities          -- real Opportunity count among matched companies whose
                                  offering_matcher.py match is candidate_match for THIS offering
                                  (problem/demand-aligned, not just ICP-aligned).
        commercial (meetings/won/revenue) -- real CalendarBooking outcomes (Revenue Pace's own
                                  data, unmodified) whose outcome_icp_snapshot includes this ICP
                                  and outcome_offering_name matches this offering.
        evidence_strength      -- "sufficient" | "thin" | "none", a plain-English label derived
                                  from whether ANY of the above is nonzero -- never a weighted
                                  numeric score (Part 1's explicit "no score unless defensible").
    Unconfigured/excluded cells report no evidence at all (status alone already explains why),
    same discipline as the original matched_account_count rule.

    PERFORMANCE: matched-company-ids, opportunities, outcomes, and market-topic trends are each
    fetched with a small, fixed number of bulk queries up front (never per-cell, never per-company
    inside the cell loop) -- same "bulk first, compose in memory" discipline this function already
    used for matched_account_count."""
    icps = get_icp_config(db, tenant_id)
    offerings = get_offering_config(db, tenant_id)
    icp_ids = [icp["id"] for icp in icps]

    matched_counts: dict[str, int] = {}
    matched_company_ids_by_icp: dict[str, set[int]] = {}
    demand_hypothesis_counts: dict[str, int] = {}
    if icp_ids:
        matched_counts = dict(
            db.query(ICPMatch.icp_id, func.count(ICPMatch.id))
            .filter(ICPMatch.tenant_id == tenant_id, ICPMatch.icp_id.in_(icp_ids))
            .group_by(ICPMatch.icp_id)
            .all()
        )
        for icp_id, company_id in db.query(ICPMatch.icp_id, ICPMatch.company_id).filter(ICPMatch.tenant_id == tenant_id, ICPMatch.icp_id.in_(icp_ids)).all():
            matched_company_ids_by_icp.setdefault(icp_id, set()).add(company_id)

        demand_company_ids = {
            row[0] for row in db.query(DemandHypothesis.company_id).filter(DemandHypothesis.tenant_id == tenant_id, DemandHypothesis.company_id.isnot(None)).all()
        }
        for icp_id, company_ids in matched_company_ids_by_icp.items():
            demand_hypothesis_counts[icp_id] = len(company_ids & demand_company_ids)

    opportunities_by_cell, outcomes_by_cell = _opportunity_and_outcome_evidence(db, tenant_id, icp_ids, matched_company_ids_by_icp)
    market_demand_by_offering = _market_demand_by_offering(db, tenant_id, offerings)

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

            account_demand = matched_counts.get(icp["id"], 0) if status == "applicable" else 0
            evidence = None
            if status == "applicable":
                market = market_demand_by_offering.get(offering["name"], {"state": "no_related_topic", "relevant_signals": 0, "related_topics": []})
                opp_statements = opportunities_by_cell.get((icp["id"], offering["name"]), [])
                outcome = outcomes_by_cell.get((icp["id"], offering["name"]), {"meetings": 0, "won": 0, "revenue_usd": 0})
                has_evidence_anywhere = (
                    account_demand > 0 or market["state"] not in ("no_related_topic", "insufficient_evidence") or opp_statements or outcome["meetings"] > 0
                )
                has_deep_evidence = bool(opp_statements) and account_demand > 0
                evidence = {
                    "market_demand": market,
                    "account_demand": {
                        "matched_account_count": account_demand,
                        "demand_hypothesis_count": demand_hypothesis_counts.get(icp["id"], 0),
                    },
                    "opportunities": {"count": len(opp_statements), "sample_statements": opp_statements[:3]},
                    "commercial": outcome,
                    "evidence_strength": "sufficient" if has_deep_evidence else ("thin" if has_evidence_anywhere else "none"),
                }

            cells.append({
                "offering": offering["name"],
                "status": status,
                # Only a real, configured cell can meaningfully report account evidence -- an
                # unconfigured/excluded cell reporting a nonzero count would misleadingly imply
                # a relationship the config doesn't actually establish.
                "matched_account_count": account_demand,
                "evidence": evidence,
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
