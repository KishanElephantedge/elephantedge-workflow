"""Hot-lead tagging -- Mode A from the approved hot-leads research (see the session's plan doc):
additive only, never a new qualification gate. A company that already made it through the
pipeline keeps its spot regardless; this only ever adds a "Hot Lead" marker on top when a real,
zero/near-zero-cost signal is present. Nothing here changes who gets discovered or contacted.

Four signals, all from data already flowing through the currently-active Apify autonomous path
(no new paid calls):

1. Hiring velocity -- 2+ qualifying postings open at once at the same company (Signal
   Framework v2's advisor-recommended top signal; see `apify_discovery.py`'s
   `domain_posting_counts` precompute).
2. The "top of the funnel pipeline generation" JD phrase (Signal Framework v2, section 3).
3. Product-fit JD category breadth -- 2+ distinct `PRODUCT_FIT_KEYWORD_CATEGORIES` matched in
   the JD reads much more like "this role is literally our product" than a single category hit.
4. Decision-maker pain-language -- Phase 13's `contact_research` (`recent_initiatives`/
   `stated_needs`/`implied_gaps`, pulled from the decision-maker's own recent LinkedIn posts)
   already gets collected for message personalization today and was never used as a signal
   until now. Only available for companies where a contact was actually found.

Deliberately NOT included in this pass (real gaps, not oversights): funding recency,
headcount-growth, and first-hire-vs-scaling classification are all part of the hot-leads
research catalogue, but none of the underlying data is actually populated on the currently-
active Apify discovery path -- adding them would require new paid calls, which is out of scope
for this additive-only pass."""

from sqlalchemy.orm import Session

from app.db.models import Company

HIRING_VELOCITY_THRESHOLD = 2
PRODUCT_FIT_CATEGORY_THRESHOLD = 2

PAIN_LANGUAGE_FIELDS = ["stated_needs", "implied_gaps", "recent_initiatives"]


def _company_signal_reasons(company: Company) -> list[str]:
    reasons = []
    if (company.hiring_signal_posting_count or 0) >= HIRING_VELOCITY_THRESHOLD:
        reasons.append(f"{company.hiring_signal_posting_count} sales roles open at once")
    if company.tofu_keyword_found:
        reasons.append('JD mentions "top of the funnel pipeline generation"')
    category_count = len(company.product_fit_jd_categories or [])
    if category_count >= PRODUCT_FIT_CATEGORY_THRESHOLD:
        reasons.append(f"{category_count} product-fit JD categories matched")
    return reasons


def _contact_pain_language_reasons(contact) -> list[str]:
    pm = contact.personalized_message
    if not pm or not isinstance(pm.contact_research, dict):
        return []
    reasons = []
    for field in PAIN_LANGUAGE_FIELDS:
        values = pm.contact_research.get(field)
        if values:
            reasons.append(f"decision-maker's LinkedIn posts show {field.replace('_', ' ')}")
    return reasons


def apply_hot_lead_tags(batch_id: int, db: Session) -> dict:
    """Call once per batch, after Phase 13 message generation has run (so decision-maker
    pain-language data, if any, is available) -- safe to call multiple times, always
    recomputes from current data rather than only ever adding."""
    companies = db.query(Company).filter(Company.batch_id == batch_id).all()
    tagged = 0
    for company in companies:
        reasons = _company_signal_reasons(company)
        for contact in company.contacts:
            reasons.extend(_contact_pain_language_reasons(contact))
        company.hot_lead = bool(reasons)
        company.hot_lead_reasoning = "; ".join(reasons) if reasons else None
        if reasons:
            tagged += 1
    db.commit()
    return {"companies_checked": len(companies), "hot_leads_tagged": tagged}
