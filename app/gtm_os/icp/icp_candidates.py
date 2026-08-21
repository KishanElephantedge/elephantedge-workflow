"""ICP Candidates -- cross-ICP composition layer (GTM-OS architecture upgrade, Part 3).

NOT A SECOND ICP ENGINE: evaluate_icp_matches_for_company() (icp_matching.py, unmodified) already
evaluates a company against EVERY configured ICP and already returns one result per ICP -- a
company already CAN match multiple ICPs simultaneously, each independently persisted as its own
ICPMatch row. Nothing about the matching algorithm changes here.

What was actually missing: a company's several matched ICPs were all reported flat, with no
distinction between "the strongest fit" and "also fits, but weaker" -- and a company that almost
matched an ICP (missing_information present, or one check unsatisfied) left no trace anywhere.
This module is a pure, read-only interpretation layer over evaluate_icp_matches_for_company()'s
own output, classifying each ICP into exactly one of three buckets a human can act on differently:

    "confirmed"           -- a real, persisted ICPMatch exists (every configured check for that
                              ICP was both evaluable and satisfied)
    "alternative"          -- same as confirmed (a real match), just not the primary one
    "insufficient_evidence" -- the ICP could not be confirmed OR ruled out, because at least one
                              check couldn't be evaluated (a null Company field) -- this is
                              explicitly NOT "doesn't match": it's an honest "not enough data yet"
    (a cleanly-failed ICP -- evaluated, and genuinely doesn't fit -- is not returned as a
    candidate at all; real evidence already says no, so there is nothing to invite a human to
    review)

PRIMARY SELECTION: deterministic, no invented scoring. Among confirmed matches, the one with the
most satisfied `reasons` wins (more independently-satisfied checks = the strongest-evidenced fit);
ties broken by ICP id order (stable, not arbitrary-per-call). If only one ICP is confirmed, it is
trivially primary. If zero are confirmed, there is no primary -- never forced."""

from sqlalchemy.orm import Session

from app.db.models import Company
from app.gtm_os.icp.icp_config import get_icp_config
from app.gtm_os.icp.icp_matching import evaluate_icp_matches_for_company


def get_icp_candidates_for_company(db: Session, tenant_id: int, company_id: int) -> dict:
    """Pure, read-only. Re-evaluates evaluate_icp_matches_for_company() live (not from persisted
    ICPMatch rows) so insufficient-evidence ICPs -- which are never persisted -- are visible here
    too, not just confirmed ones."""
    company = db.get(Company, company_id)
    if company is None:
        return {
            "company_id": company_id,
            "primary": None,
            "alternatives": [],
            "insufficient_evidence": [],
            "reason": f"company_id {company_id} does not resolve to a real Company row",
        }

    icp_config = get_icp_config(db, tenant_id)
    results = evaluate_icp_matches_for_company(company, icp_config)

    confirmed = [r for r in results if r["matched"]]
    insufficient = [r for r in results if not r["matched"] and r["missing_information"]]
    # Cleanly-failed ICPs (evaluated, checks simply not satisfied, nothing missing) are real
    # negative evidence -- omitted entirely, same "don't report noise" discipline
    # icp_offering_matching.py's match_offerings_for_company() already established.

    if not confirmed:
        return {
            "company_id": company_id,
            "primary": None,
            "alternatives": [],
            "insufficient_evidence": [
                {"icp_id": r["icp_id"], "icp_name": r["icp_name"], "missing_information": r["missing_information"], "reasons_so_far": r["reasons"]}
                for r in insufficient
            ],
            "reason": "no ICP is confirmed for this company yet" + (
                " -- some are candidates pending more data" if insufficient else " and no evidence exists to evaluate any ICP as a candidate either"
            ),
        }

    ordered = sorted(confirmed, key=lambda r: (-len(r["reasons"]), r["icp_id"]))
    primary, *rest = ordered

    return {
        "company_id": company_id,
        "primary": {
            "icp_id": primary["icp_id"],
            "icp_name": primary["icp_name"],
            "reasons": primary["reasons"],
            "trigger_evidence": primary["trigger_evidence"],
        },
        "alternatives": [
            {"icp_id": r["icp_id"], "icp_name": r["icp_name"], "reasons": r["reasons"], "trigger_evidence": r["trigger_evidence"]}
            for r in rest
        ],
        "insufficient_evidence": [
            {"icp_id": r["icp_id"], "icp_name": r["icp_name"], "missing_information": r["missing_information"], "reasons_so_far": r["reasons"]}
            for r in insufficient
        ],
        "reason": "primary is the confirmed ICP with the most independently-satisfied checks"
        + (f"; {len(rest)} alternative confirmed match(es) also apply" if rest else ""),
    }
