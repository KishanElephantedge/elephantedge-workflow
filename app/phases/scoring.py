"""
Phase 9 — Opportunity Scoring, rebuilt around Signal Framework v2's simplified 5-category model
(Need / Ability to Pay / Outbound Maturity / Product Fit / Buying Intent, summing to 100),
replacing the earlier 3-signal/110-point rubric. The advisor's own note in the framework
recommended this simplified version over the 17-variable weighted one -- adopted directly
rather than re-deriving a third model.

Each category's point allocation below is this implementation's own breakdown within the
framework's stated weights (30/20/20/20/10) -- the framework specifies the category weights and
what belongs in each, not exact sub-point values, so these are a reasoned first pass, not a
value handed down verbatim, and should be revisited once real outcome data exists.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Company, Score

TIER_A_THRESHOLD = 90  # contact immediately
TIER_B_THRESHOLD = 80  # high priority
TIER_C_THRESHOLD = 70  # good fit -- below this is low priority/nurture

HIRING_STRENGTH_POINTS = {"strong": 25, "medium": 15, "weak": 5}


def _tier_for_score(score: float) -> str:
    if score >= TIER_A_THRESHOLD:
        return "hot"  # Tier A, reusing existing tier vocabulary (hot/warm/cool/excluded)
    if score >= TIER_B_THRESHOLD:
        return "warm"  # Tier B
    if score >= TIER_C_THRESHOLD:
        return "cool"  # Tier C
    return "excluded"  # low priority / nurture


def _need_score(company: Company) -> float:
    """Do they actually need more pipeline? Hiring signal strength is the primary driver,
    headcount growth is a smaller corroborating signal."""
    points = HIRING_STRENGTH_POINTS.get(company.hiring_signal_strength, 0)
    if (company.headcount_growth_12m_percent or 0) > 15:
        points += 5
    return min(points, 30)


def _ability_to_pay_score(company: Company) -> float:
    """Revenue is already gated to $2.5M-$5M by Discovery, so its own discriminating power
    within that band is low -- funding history is the real differentiator here."""
    points = 5  # base: already confirmed in-band by Discovery's hard gate
    if company.last_funding_date:
        points += 10
    if (company.crunchbase_total_investment_usd or 0) > 0:
        points += 5
    return min(points, 20)


def _outbound_maturity_score(company: Company) -> float:
    points = 0
    if company.has_outbound_tooling:
        points += 15
    if company.has_ai_sdr_tool:
        points += 5
    return min(points, 20)


def _product_fit_score(company: Company, has_primary_contact: bool) -> float:
    points = 0
    if company.industry_classification == "tech":
        points += 10
    if company.geography_tier == "tier_1":
        points += 5
    if has_primary_contact:  # founder-led sales -- Elephant Edge's core Product Fit criterion
        points += 5
    return min(points, 20)


def _buying_intent_score(company: Company) -> float:
    points = 0
    if company.last_funding_date:
        points += 4
    if company.hiring_signal_hire_type == "first_hire":
        points += 4
    if company.active_head_of_sales_posting:
        points += 2
    return min(points, 10)


def score_company(company: Company, db: Session) -> Score:
    has_primary_contact = any(c.thread_role == "founder_ceo" for c in company.contacts)

    need = _need_score(company)
    ability_to_pay = _ability_to_pay_score(company)
    outbound_maturity = _outbound_maturity_score(company)
    product_fit = _product_fit_score(company, has_primary_contact)
    buying_intent = _buying_intent_score(company)

    total = need + ability_to_pay + outbound_maturity + product_fit + buying_intent
    tier = _tier_for_score(total)

    score = company.score
    if score is None:
        score = Score(company_id=company.id)
        db.add(score)

    # Reusing the shared Score table's existing columns for the new categories -- signal_strength
    # now holds Need, financial_growth holds Ability to Pay, icp_fit holds Product Fit,
    # greenfield_legacy holds Buying Intent, stacking_bonus holds Outbound Maturity. Column names
    # are stale (Synefi-era), but adding a schema migration to rename shared columns is a bigger,
    # separate change -- breakdown (JSON) is the authoritative source of truth for what each
    # number actually means going forward.
    score.signal_strength = need
    score.financial_growth = ability_to_pay
    score.stacking_bonus = outbound_maturity
    score.icp_fit = product_fit
    score.greenfield_legacy = buying_intent
    score.decision_maker_match = 5 if has_primary_contact else 0
    score.total_score = total
    score.tier = tier
    score.computed_at = datetime.utcnow()
    score.breakdown = {
        "need": need,
        "ability_to_pay": ability_to_pay,
        "outbound_maturity": outbound_maturity,
        "product_fit": product_fit,
        "buying_intent": buying_intent,
        "total": total,
        "tier_label": {"hot": "Tier A", "warm": "Tier B", "cool": "Tier C", "excluded": "Low priority/nurture"}[tier],
        "hiring_signal_role": company.hiring_signal_role,
        "hiring_signal_reasoning": company.hiring_signal_reasoning,
    }
    db.commit()
    db.refresh(score)
    return score


def run_scoring(batch_id: int, db: Session) -> dict:
    """Phase 9 entrypoint. Re-scores every company in the batch each run (cheap local
    computation, no Deepline cost) -- a company's score can legitimately change if run again
    after Decision Maker resolves a contact it didn't have before, or after Buying
    Signal/tech-stack checks complete."""
    companies = db.query(Company).filter(Company.batch_id == batch_id).all()
    tier_counts = {"hot": 0, "warm": 0, "cool": 0, "excluded": 0}
    for company in companies:
        score = score_company(company, db)
        tier_counts[score.tier] += 1

    return {
        "companies_scored": len(companies),
        "tier_counts": tier_counts,
    }
