"""
Phase 9 -- Opportunity Scoring, rebuilt around the 18-variable weighted GTM Fit Score Kishan's
lead specified, replacing Signal Framework v2's 5-category/100-point model.

Weights sum to 175 (not 100), plus a -20 negative-signal modifier -- kept as a raw scale per
explicit instruction, with hot/warm/cool tier thresholds set at the same 90/80/70 PERCENTAGES
of 175 (158/140/123) rather than re-using the old absolute 90/80/70 cutoffs.

Four variables have no data source yet and score 0 until built (per explicit instruction to
implement now rather than block on missing data):
  I - Tenure of the team           (needs per-person tenure data, not currently collected)
  L - Sales & marketing spend      (no ad-spend/budget data source wired in)
  P - SMB or Enterprise motion     (needs customer/deal-size classification, not built)
  Q - LinkedIn+Email as primary outbound channel (needs outbound-channel detection, not built)
These four alone are 50 of the 175 points -- max achievable today is 125/175, which is BELOW
the "warm" threshold (140). Until these are built, no company can score hot or warm; only
cool/excluded are reachable. This is a known, deliberate consequence, not a bug.

E (Location) is specified as an optional filter, not a scored variable -- Discovery's own
hq_country="USA" gate already covers this, so it isn't scored here at all.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Company, Score

MAX_POSSIBLE_SCORE = 175  # sum of all positive weights (A..Q, excluding filter-only E)
TIER_HOT_THRESHOLD = 158  # 90% of 175
TIER_WARM_THRESHOLD = 140  # 80% of 175
TIER_COOL_THRESHOLD = 123  # 70% of 175 (122.5 rounded up)

RECENT_FUNDING_MONTHS = 24


def _tier_for_score(score: float) -> str:
    if score >= TIER_HOT_THRESHOLD:
        return "hot"
    if score >= TIER_WARM_THRESHOLD:
        return "warm"
    if score >= TIER_COOL_THRESHOLD:
        return "cool"
    return "excluded"


def _company_size_score(company: Company) -> float:
    """A: full marks for the tightest original ICP band (11-50), partial credit for the
    wider band Discovery now also allows (51-200)."""
    count = company.employee_count
    if count is None:
        return 0
    if 11 <= count <= 50:
        return 10
    if 51 <= count <= 200:
        return 6
    return 0


def _hiring_sdr_ae_score(company: Company) -> float:
    """B: hiring signal role is the direct real-data proxy for 'hiring SDRs/AEs from JDs'."""
    role = company.hiring_signal_role
    if role in ("sdr", "ae"):
        return 15
    if role in ("head_of_sales", "gtm"):
        return 8
    return 0


def _revenue_score(company: Company) -> float:
    """C: already gated in-band by Discovery's own filter, so this is mostly confirmatory."""
    lower = company.estimated_revenue_lower_usd
    higher = company.estimated_revenue_higher_usd
    if lower is None and higher is None:
        return 0
    return 10


def _employer_growth_score(company: Company) -> float:
    """D: headcount_growth_12m_percent, captured free from Discovery's Crustdata response."""
    growth = company.headcount_growth_12m_percent
    if growth is None:
        return 0
    if growth > 15:
        return 10
    if growth > 0:
        return 5
    return 0


def _recent_funding_score(company: Company) -> float:
    """F: recency of last funding round, not just its existence (that's a separate signal
    the old model used -- here it's specifically about how RECENT it is)."""
    if not company.last_funding_date:
        return 0
    months_ago = (datetime.utcnow() - company.last_funding_date).days / 30
    if months_ago <= RECENT_FUNDING_MONTHS:
        return 10
    return 5


def _sales_headcount_score(company: Company) -> float:
    """G: reuses the team-composition gate's own tier (hiring_signal.assess_team_composition)
    -- a smaller existing sales team is the favorable case here (real need), not a larger one,
    consistent with what the team-gap check already established."""
    tier = company.team_fit_tier
    if tier == "primary":
        return 15
    if tier == "secondary":
        return 7
    if tier == "excluded":
        return 0
    return 5  # not yet assessed -- neutral default, not a penalty


def _open_sales_roles_score(company: Company) -> float:
    """H: we don't track a literal count of open sales roles, only the single best match --
    approximated by role match strength plus JD product-fit depth as a proxy for "more than
    one real signal of active outbound hiring", not a literal role count."""
    if not company.hiring_signal_role:
        return 0
    points = 10
    if company.product_fit_jd_categories:
        points += 5
    return min(points, 15)


def _marketing_headcount_score(company: Company) -> float:
    """J: same "smaller existing team = more need" logic as G, applied to marketing percent
    directly (team_fit_tier only reflects the sales-side threshold check)."""
    percent = company.marketing_headcount_percent
    if percent is None:
        return 0
    if percent <= 4:
        return 10
    if percent <= 8:
        return 5
    return 0


def _gtm_headcount_score(company: Company) -> float:
    """K: distinct from G/J (existing headcount) -- this scores a GTM-specific role
    actively being hired for, a direct signal of GTM function being built out right now."""
    return 10 if company.hiring_signal_role == "gtm" else 0


def _tech_stack_score(company: Company) -> float:
    """M: existing sales tooling (Clay, Apollo, HubSpot, Outreach, etc.) -- additive signal
    of outbound maturity, not exclusionary, same direction as the old model's treatment."""
    return 5 if company.has_outbound_tooling else 0


def _product_fit_score(company: Company) -> float:
    """N: number of distinct JD product-fit categories matched -- a JD describing our own
    product's job as a headcount line item."""
    categories = company.product_fit_jd_categories or []
    return min(len(categories) * 2, 10)


def _market_tier_score(company: Company) -> float:
    """O: Tier-1 vs Tier-2 market, from Discovery's own geography_tier classification."""
    if company.geography_tier == "tier_1":
        return 5
    if company.geography_tier == "tier_2":
        return 2
    return 0


def _negative_signal_penalty(company: Company) -> float:
    """R: the clearest real negative signal we can currently detect is the team-composition
    gate's own "excluded" verdict (a full GTM team already, or the company doesn't need us
    despite technically having an open role) -- confirmed live against SmartWinnr."""
    return -20 if company.team_fit_tier == "excluded" else 0


def score_company(company: Company, db: Session) -> Score:
    a_company_size = _company_size_score(company)
    b_hiring_sdr_ae = _hiring_sdr_ae_score(company)
    c_revenue = _revenue_score(company)
    d_employer_growth = _employer_growth_score(company)
    f_recent_funding = _recent_funding_score(company)
    g_sales_headcount = _sales_headcount_score(company)
    h_open_sales_roles = _open_sales_roles_score(company)
    i_team_tenure = 0  # not built -- no tenure data source
    j_marketing_headcount = _marketing_headcount_score(company)
    k_gtm_headcount = _gtm_headcount_score(company)
    l_sales_marketing_spend = 0  # not built -- no spend data source
    m_tech_stack = _tech_stack_score(company)
    n_product_fit = _product_fit_score(company)
    o_market_tier = _market_tier_score(company)
    p_smb_enterprise_motion = 0  # not built -- no deal-size/customer classification
    q_outbound_channel = 0  # not built -- no primary-outbound-channel detection
    r_negative_signals = _negative_signal_penalty(company)

    total = (
        a_company_size + b_hiring_sdr_ae + c_revenue + d_employer_growth + f_recent_funding
        + g_sales_headcount + h_open_sales_roles + i_team_tenure + j_marketing_headcount
        + k_gtm_headcount + l_sales_marketing_spend + m_tech_stack + n_product_fit
        + o_market_tier + p_smb_enterprise_motion + q_outbound_channel + r_negative_signals
    )
    tier = _tier_for_score(total)

    score = company.score
    if score is None:
        score = Score(company_id=company.id)
        db.add(score)

    # Reusing the shared Score table's existing columns -- signal_strength holds B+H (need/
    # hiring-driven points), financial_growth holds C+D+F, icp_fit holds A+N+O, stacking_bonus
    # holds G+J+K+M (team-composition + tooling), greenfield_legacy unused (0) under this
    # model. breakdown (JSON) is the authoritative source of truth for what each number means.
    score.signal_strength = b_hiring_sdr_ae + h_open_sales_roles
    score.financial_growth = c_revenue + d_employer_growth + f_recent_funding
    score.icp_fit = a_company_size + n_product_fit + o_market_tier
    score.stacking_bonus = g_sales_headcount + j_marketing_headcount + k_gtm_headcount + m_tech_stack
    score.greenfield_legacy = 0
    score.decision_maker_match = 0
    score.total_score = total
    score.tier = tier
    score.computed_at = datetime.utcnow()
    score.breakdown = {
        "A_company_size": a_company_size,
        "B_hiring_sdr_ae": b_hiring_sdr_ae,
        "C_revenue": c_revenue,
        "D_employer_growth": d_employer_growth,
        "F_recent_funding": f_recent_funding,
        "G_sales_headcount": g_sales_headcount,
        "H_open_sales_roles": h_open_sales_roles,
        "I_team_tenure": i_team_tenure,
        "J_marketing_headcount": j_marketing_headcount,
        "K_gtm_headcount": k_gtm_headcount,
        "L_sales_marketing_spend": l_sales_marketing_spend,
        "M_tech_stack": m_tech_stack,
        "N_product_fit": n_product_fit,
        "O_market_tier": o_market_tier,
        "P_smb_enterprise_motion": p_smb_enterprise_motion,
        "Q_outbound_channel": q_outbound_channel,
        "R_negative_signals": r_negative_signals,
        "total": total,
        "max_possible": MAX_POSSIBLE_SCORE,
        "tier_label": {"hot": "Hot", "warm": "Warm", "cool": "Cool", "excluded": "Low priority/nurture"}[tier],
        "unbuilt_variables": ["I_team_tenure", "L_sales_marketing_spend", "P_smb_enterprise_motion", "Q_outbound_channel"],
        "hiring_signal_role": company.hiring_signal_role,
        "hiring_signal_reasoning": company.hiring_signal_reasoning,
        "product_fit_jd_categories": company.product_fit_jd_categories,
        "team_fit_tier": company.team_fit_tier,
        "team_fit_reasoning": company.team_fit_reasoning,
    }
    db.commit()
    db.refresh(score)
    return score


def run_scoring(batch_id: int, db: Session) -> dict:
    """Phase 9 entrypoint. Re-scores every company in the batch each run (cheap local
    computation, no Deepline cost)."""
    companies = db.query(Company).filter(Company.batch_id == batch_id).all()
    tier_counts = {"hot": 0, "warm": 0, "cool": 0, "excluded": 0}
    for company in companies:
        score = score_company(company, db)
        tier_counts[score.tier] += 1

    return {
        "companies_scored": len(companies),
        "tier_counts": tier_counts,
    }
