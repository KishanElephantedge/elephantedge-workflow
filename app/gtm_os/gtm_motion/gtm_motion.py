"""GTM Motion recommendation -- Batch 10. Deterministic, no LLM.

A vs B DISTINCTION (Part 2): this module implements ONLY (A) "which motion should we use" --
a recommendation derived from configured rules + current ICP/offering evidence. It does NOT
implement (B) "which motion actually worked" (historical performance/attribution) -- no such
mechanism exists yet anywhere in this codebase to reuse, and Part 2 explicitly scopes this batch
to A unless a real existing B mechanism could be safely reused (none was found). The result
structure leaves room for B to inform allocation later (see `allocation`/`allocation_reason`
below) without B existing yet.

PERSISTENCE DECISION: NO new table, per Part 10's own "prefer pure computation" instruction.
recommend_gtm_motion() is pure and computed on demand from ICPMatch (Batch 8) + the ICP x
Offering matcher (Batch 9, reused unmodified) + GTM motion config (Parameter) -- all already
cheap to query. Same precedent as trend_intelligence.py/sales_agent.py/icp_offering_matching.py.

ACCOUNT VS MARKET (Part 9): operates strictly on Company -> ICPMatch -> Offering -> Motion.
Never imports or reads ContentTopic/TopicCandidate/trend_intelligence -- confirmed by having zero
such import in this file, verified by an explicit test, same discipline as opportunity.py's own
market/account boundary (Batch 4)."""

from app.db.models import Batch, Company
from app.gtm_os.gtm_motion.gtm_motion_config import get_gtm_motion_config
from app.gtm_os.icp.icp_offering_matching import match_offerings_for_company
from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis

# Account-aware "why" (architecture upgrade, Part 5). NOT a persona/channel/sequence engine --
# no such data model exists anywhere in this codebase (confirmed: no persona/channel/cadence
# field on any GTM-OS model). Rather than fabricate one, this dict is always returned with every
# value explicitly None plus a real reason, so the frontend can show "why this channel?" honestly
# as "not yet supported by the data model" instead of a silently-missing section.
_CHANNEL_PERSONA_SEQUENCE_UNSUPPORTED = {
    "persona": None,
    "channel": None,
    "sequence": None,
    "note": "no persona/channel/sequence data model exists yet -- motion recommendation is currently ICP x offering rule-based only, not persona- or channel-level",
}


def _account_context(db, tenant_id: int, company_id: int) -> list[str]:
    """Real ProblemHypothesis/DemandHypothesis statements for this account -- the "why" behind an
    account-aware motion recommendation beyond bare ICP x offering config. Read-only, reuses the
    same models account_agent.py already queries; never re-derives problem/demand evidence."""
    context = []
    for p in db.query(ProblemHypothesis).filter(ProblemHypothesis.tenant_id == tenant_id, ProblemHypothesis.company_id == company_id).order_by(ProblemHypothesis.id).all():
        context.append(f"problem evidence ({p.affected_function}): {p.problem_statement}")
    for d in db.query(DemandHypothesis).filter(DemandHypothesis.tenant_id == tenant_id, DemandHypothesis.company_id == company_id).order_by(DemandHypothesis.id).all():
        context.append(f"demand evidence ({d.affected_function}): {d.demand_statement}")
    return context


def recommend_gtm_motion(db, tenant_id: int, company_id: int) -> dict:
    """Pure, read-only. Reuses match_offerings_for_company() (Batch 9) as its sole input for
    ICP/offering evidence -- never re-derives ICP or offering matching itself.

    Result `status`:
      "insufficient_context" -- no ICP match, no offering candidate_match, or no motion is
                                 configured for the company's matched ICP/offering combination(s)
      "multiple_candidates"  -- 2+ motions are equally applicable and no complete priority
                                 ordering distinguishes them -- no arbitrary primary is chosen
      "recommended"          -- exactly one motion applies, or configured priority deterministically
                                 picks one among several applicable motions
    """
    icp_offering_result = match_offerings_for_company(db, tenant_id, company_id)

    base_result = {
        "company_id": company_id,
        "status": "insufficient_context",
        "primary_motion": None,
        "alternative_motions": [],
        "reason": None,
        "missing_information": [],
        "evidence": [],
        "allocation": None,
        "allocation_reason": "no historical motion-performance data or configured allocation rule exists yet",
        "account_context": _account_context(db, tenant_id, company_id),
        "channel_persona_sequence": dict(_CHANNEL_PERSONA_SEQUENCE_UNSUPPORTED),
    }

    if not icp_offering_result["icp_matches"]:
        base_result["missing_information"] = ["no ICPMatch rows exist for this company -- run the Batch 8 ICP matching sweep first"]
        base_result["reason"] = "no ICP match -- cannot recommend a GTM motion without knowing the account situation"
        return base_result

    candidate_pairs: list[tuple[str, str]] = []
    evidence: list[str] = []
    for icp_entry in icp_offering_result["icp_matches"]:
        for offering_result in icp_entry["offerings"]:
            if offering_result["status"] == "candidate_match":
                candidate_pairs.append((icp_entry["icp_id"], offering_result["offering"]))
                evidence.append(f"ICP {icp_entry['icp_id']!r} ({icp_entry['icp_name']}) x offering {offering_result['offering']!r}: {offering_result['reason']}")

    if not candidate_pairs:
        base_result["missing_information"] = ["ICP matched, but no offering has a candidate_match yet -- cannot recommend a motion without knowing what we'd be going to market with"]
        base_result["reason"] = "no offering candidate_match"
        return base_result

    base_result["evidence"] = evidence

    motions_config = get_gtm_motion_config(db, tenant_id)
    matched_motions = []
    for motion in motions_config:
        if not motion.get("enabled", True):
            continue
        applicable_icps = set(motion.get("applicable_icps") or [])
        applicable_offerings = set(motion.get("applicable_offerings") or [])
        if not applicable_icps and not applicable_offerings:
            continue  # unconfigured for any specific application -- never recommended, per "don't invent" discipline

        matched_pairs = [
            (icp_id, offering)
            for icp_id, offering in candidate_pairs
            if (not applicable_icps or icp_id in applicable_icps) and (not applicable_offerings or offering in applicable_offerings)
        ]
        if not matched_pairs:
            continue

        matched_motions.append({
            "motion": motion["motion"],
            "priority": motion.get("priority"),
            "rationale": motion.get("rationale"),
            "matched_pairs": matched_pairs,
        })

    if not matched_motions:
        base_result["missing_information"] = ["no GTM motion is configured for this company's matched ICP/offering combination(s)"]
        base_result["reason"] = "no motion rule applies to this company's evidence"
        return base_result

    if len(matched_motions) == 1:
        chosen = matched_motions[0]
        return {
            **base_result,
            "status": "recommended",
            "primary_motion": chosen["motion"],
            "alternative_motions": [],
            "reason": f"only applicable motion for {chosen['matched_pairs']}" + (f" -- {chosen['rationale']}" if chosen["rationale"] else ""),
        }

    prioritized = [m for m in matched_motions if m["priority"] is not None]
    if len(prioritized) == len(matched_motions):
        # every applicable motion has an explicit priority -- deterministic, lowest number wins
        best = min(matched_motions, key=lambda m: m["priority"])
        others = [m["motion"] for m in matched_motions if m is not best]
        return {
            **base_result,
            "status": "recommended",
            "primary_motion": best["motion"],
            "alternative_motions": others,
            "reason": f"priority {best['priority']} is the lowest among {len(matched_motions)} equally-applicable, fully-prioritized motions" + (f" -- {best['rationale']}" if best["rationale"] else ""),
        }

    # multiple motions apply, but priority doesn't fully distinguish them -- no arbitrary pick
    return {
        **base_result,
        "status": "multiple_candidates",
        "primary_motion": None,
        "alternative_motions": [m["motion"] for m in matched_motions],
        "reason": f"{len(matched_motions)} motions ({sorted(m['motion'] for m in matched_motions)}) are all equally applicable and priority does not fully distinguish them -- no arbitrary primary selected",
    }


def run_gtm_motion_sweep(db, tenant_id: int, limit: int = 200, dry_run: bool = False) -> dict:
    """Bounded, standalone, tenant-scoped, read-only reporting sweep -- writes NOTHING regardless
    of `dry_run` (accepted purely for interface consistency with this feature's other sweeps --
    see module docstring's persistence decision). One company's failure never aborts the sweep.
    NOT wired into the scheduler (per scope boundary)."""
    counts = {
        "companies_evaluated": 0,
        "recommended": 0,
        "multiple_candidates": 0,
        "insufficient_context": 0,
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
            result = recommend_gtm_motion(db, tenant_id, company_id)
            counts[result["status"]] += 1
        except Exception:  # noqa: BLE001 -- one company's failure must never block the others
            counts["failed"] += 1

    return counts
