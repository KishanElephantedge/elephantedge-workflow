"""Offering Recommendation -- "offering recommendation should follow the problem" composition
layer (GTM-OS architecture upgrade, Part 4).

NOT A THIRD OFFERING MATCHER. Reuses exactly two existing matchers, unmodified:
    match_offerings_for_company()  (icp_offering_matching.py, Batch 9)  -- ICP -> offering
    match_offerings()              (offering_matcher.py, Batch 4/6/7)   -- Opportunity(Problem/
                                                                            Demand) -> offering
and one existing composition, unmodified:
    get_icp_candidates_for_company() (icp_candidates.py) -- primary vs alternative ICP

This module's only new logic is combining those three real facts into the flow the spec asks for:
    Company -> ICP candidates -> Problem/Demand (via this company's real Opportunities)
             -> applicable offerings -> most relevant offering(s)

An offering is the STRONGEST recommendation only when BOTH matchers agree:
    (a) it's a candidate_match for the company's PRIMARY ICP (icp_offering_matching.py), AND
    (b) it's a candidate_match for at least one of the company's real Opportunities
        (offering_matcher.py -- i.e. it also fits the actual affected_function evidence)
That is real problem-alignment, not just configuration. An offering with only (a) -- ICP-fit, no
Opportunity to check problem-alignment against yet -- is still reported, honestly labeled
"icp_only": a real, weaker basis, never silently upgraded to "strongest".

No recommendation score anywhere -- every result carries its basis (icp_and_problem | icp_only)
and the real evidence strings behind it, per the spec's own "explanation more important than an
arbitrary percentage" instruction."""

from sqlalchemy.orm import Session

from app.gtm_os.icp.icp_candidates import get_icp_candidates_for_company
from app.gtm_os.icp.icp_offering_matching import match_offerings_for_company
from app.gtm_os.opportunity.offering_matcher import match_offerings
from app.gtm_os.opportunity.opportunity import Opportunity


def _offering_candidates_for_icp(icp_offering_result: dict, icp_id: str) -> set[str]:
    for icp_entry in icp_offering_result["icp_matches"]:
        if icp_entry["icp_id"] == icp_id:
            return {o["offering"] for o in icp_entry["offerings"] if o["status"] == "candidate_match"}
    return set()


def _problem_aligned_offerings(db: Session, tenant_id: int, company_id: int) -> dict[str, list[str]]:
    """offering name -> list of real opportunity_statement strings whose Opportunity had a
    candidate_match for that offering via offering_matcher.py (affected_function alignment)."""
    aligned: dict[str, list[str]] = {}
    opportunities = (
        db.query(Opportunity)
        .filter(Opportunity.tenant_id == tenant_id, Opportunity.company_id == company_id)
        .order_by(Opportunity.id)
        .all()
    )
    for opp in opportunities:
        for result in match_offerings(db, tenant_id, opp):
            if result["status"] == "candidate_match":
                aligned.setdefault(result["offering"], []).append(opp.opportunity_statement)
    return aligned


def _recommend_for_icp(icp_id: str, icp_name: str, icp_offering_result: dict, problem_aligned: dict[str, list[str]]) -> list[dict]:
    offerings = sorted(_offering_candidates_for_icp(icp_offering_result, icp_id))
    out = []
    for offering in offerings:
        statements = problem_aligned.get(offering, [])
        if statements:
            out.append({
                "offering": offering,
                "basis": "icp_and_problem",
                "explanation": (
                    f"{icp_name} fits this account. Offering {offering!r} is applicable to {icp_name} "
                    f"and directly addresses an identified problem/demand: {statements[0]}"
                ),
            })
        else:
            out.append({
                "offering": offering,
                "basis": "icp_only",
                "explanation": (
                    f"Offering {offering!r} is configured as applicable to {icp_name}, but no Opportunity "
                    "exists yet to confirm problem/demand alignment -- based on ICP configuration only."
                ),
            })
    # real problem+demand alignment outranks configuration-only alignment -- never an arbitrary score
    out.sort(key=lambda r: 0 if r["basis"] == "icp_and_problem" else 1)
    return out


def get_offering_recommendation_for_company(db: Session, tenant_id: int, company_id: int) -> dict:
    """Pure, read-only. company -> ICP candidates -> applicable offerings -> problem/demand
    alignment -> strongest recommendation, exactly the flow the spec asks for, entirely from
    already-existing matchers."""
    icp_candidates = get_icp_candidates_for_company(db, tenant_id, company_id)
    if icp_candidates["primary"] is None:
        return {
            "company_id": company_id,
            "primary_recommendation": None,
            "alternative_recommendations": [],
            "reason": "no confirmed ICP for this company -- cannot recommend an offering without knowing which ICP applies",
        }

    icp_offering_result = match_offerings_for_company(db, tenant_id, company_id)
    problem_aligned = _problem_aligned_offerings(db, tenant_id, company_id)

    primary_icp = icp_candidates["primary"]
    primary_candidates = _recommend_for_icp(primary_icp["icp_id"], primary_icp["icp_name"], icp_offering_result, problem_aligned)

    alternative_recommendations = []
    for alt_icp in icp_candidates["alternatives"]:
        alt_candidates = _recommend_for_icp(alt_icp["icp_id"], alt_icp["icp_name"], icp_offering_result, problem_aligned)
        if alt_candidates:
            alternative_recommendations.append({
                "icp_id": alt_icp["icp_id"],
                "icp_name": alt_icp["icp_name"],
                "offerings": alt_candidates,
            })

    if not primary_candidates:
        return {
            "company_id": company_id,
            "primary_recommendation": None,
            "alternative_recommendations": alternative_recommendations,
            "reason": f"{primary_icp['icp_name']} is the primary ICP, but no offering is configured as applicable to it yet",
        }

    return {
        "company_id": company_id,
        "primary_recommendation": {
            "icp_id": primary_icp["icp_id"],
            "icp_name": primary_icp["icp_name"],
            "offering": primary_candidates[0]["offering"],
            "basis": primary_candidates[0]["basis"],
            "explanation": primary_candidates[0]["explanation"],
        },
        "primary_icp_other_offerings": primary_candidates[1:],
        "alternative_recommendations": alternative_recommendations,
        "reason": "strongest recommendation is the primary ICP's highest-basis offering (real problem/demand alignment beats configuration-only alignment)",
    }
