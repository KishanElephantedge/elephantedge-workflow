"""
Rich hiring-signal classification (Signal Framework v2, section 3) -- replaces the simple
"does a posting exist" check with a real classification of *which* role is being hired, whether
it's a first-ever hire or scaling an existing team, and what that implies.

Uses the same `bloomberry_search_job_postings` tool already wired in buying_signal.py -- it
already returns the full JD text (`description` field), which was simply never read before.
One combined call per company (single OR keyword across all role categories), classified
locally by title-matching -- no additional paid call per role category.
"""

from sqlalchemy.orm import Session

from app.db.models import Company
from app.deepline_client import execute_tool, extract_rows

ROLE_KEYWORDS = {
    "head_of_sales": ["head of sales", "vp sales", "vp of sales", "director of sales", "sales manager", "cro", "chief revenue officer"],
    "sdr": ["sdr", "sales development representative", "bdr", "business development representative"],
    "ae": ["account executive", " ae "],
    "marketing": ["head of marketing", "marketing manager", "growth marketing"],
    "gtm": ["head of gtm", "gtm engineer", "gtm operations"],
}

COMBINED_JOB_KEYWORD = (
    "Head of Sales OR VP Sales OR VP of Sales OR Director of Sales OR Sales Manager OR CRO OR "
    "SDR OR Sales Development Representative OR BDR OR Business Development Representative OR "
    "Account Executive OR Head of Marketing OR Marketing Manager OR Growth Marketing OR "
    "Head of GTM OR GTM Engineer OR GTM Operations"
)

# First-hire language patterns -- direct textual evidence a JD is for a brand-new role, not a
# replacement or team expansion. Confirms/strengthens the headcount-based inference below.
FIRST_HIRE_PHRASES = ["first sales hire", "first-ever", "build from scratch", "founding", "ground floor", "build our sales"]

TOFU_KEYWORD = "top of the funnel"

# Sales headcount % below this is treated as "no team or a small team exists" for
# hire-type inference (Signal Framework v2's own wording), consistent with Discovery's
# 2-10% qualifying band -- near the low end of that band counts as "small team".
SMALL_TEAM_THRESHOLD_PERCENT = 4.0


def _classify_role(title: str) -> str | None:
    title_lower = f" {title.lower()} "
    for role, keywords in ROLE_KEYWORDS.items():
        if any(kw in title_lower for kw in keywords):
            return role
    return None


def _infer_hire_type(company: Company, role: str) -> str:
    """No API tells us directly whether this is a company's 1st or 5th hire for a role --
    inferred from existing team size (Discovery's role_distribution_percent), the same
    approach used manually in the Lucky Robots worked example (account-gap-analysis-methodology.md)."""
    if role in ("head_of_sales", "sdr", "ae"):
        percent = company.sales_headcount_percent
    else:
        percent = company.marketing_headcount_percent
    if percent is None:
        return "unknown"
    return "first_hire" if percent <= SMALL_TEAM_THRESHOLD_PERCENT else "multiple_hire"


def _classify_signal(company: Company, role: str, hire_type: str, description: str) -> tuple[str, str]:
    """Returns (strength, reasoning), per Signal Framework v2's tables (section 3)."""
    description_lower = description.lower()
    first_hire_confirmed = hire_type == "first_hire" or any(p in description_lower for p in FIRST_HIRE_PHRASES)

    no_sales_team = (company.sales_headcount_percent or 0) <= SMALL_TEAM_THRESHOLD_PERCENT
    no_marketing_team = (company.marketing_headcount_percent or 0) <= SMALL_TEAM_THRESHOLD_PERCENT

    if role == "head_of_sales" and first_hire_confirmed:
        return "strong", "First Head of Sales hire, no/small team exists -- transfer of revenue-generation ownership from founder. Ask: what was the previous revenue motion?"
    if role == "sdr":
        if first_hire_confirmed:
            return "strong", "First SDR hire -- testing outbound. Cross-check recent funding to corroborate intent."
        return "medium", "Multiple/parallel SDR hire -- outbound already working, doubling down. Ask: why hire more instead of augmenting?"
    if role == "ae":
        if first_hire_confirmed:
            return "medium", "First AE hire -- an SDR or marketing team may already exist; confirms a pipeline exists."
        if no_sales_team and no_marketing_team:
            return "strong", "Multiple AE hires, no SDR or marketing team -- AE owns the full sales cycle end-to-end."
        return "medium", "Multiple AE hires, with SDR -- needs more closing efficiency."
    if role == "marketing":
        if no_sales_team:
            return "weak", "Marketing hire, no sales team -- focus shift toward inbound/PLG. Ask: why hire more instead of augmenting?"
        return "medium", "Marketing hire, with sales team -- needs more inbound/TOFU pipeline. Ask: why hire more instead of augmenting?"
    if role == "gtm":
        return "medium", "GTM hire, with sales team -- focus shift toward augmenting/automating sales process and marketing. Ask: how much are they spending on this?"
    return "weak", "Matched a role posting but couldn't confidently classify hire type."


def classify_hiring_signal(company: Company, db: Session) -> dict:
    """Fetches active postings (one combined call) and classifies the strongest match found.
    Persists results directly on the Company row."""
    if not company.domain:
        return {"role": None, "hire_type": None, "strength": None, "reasoning": "no domain"}

    response = execute_tool(
        "bloomberry_search_job_postings",
        {
            "domain": company.domain,
            "keyword": COMBINED_JOB_KEYWORD,
            "search_job_title_only": False,  # need description text too, not just title matches
            "active_only": True,
            "limit": 3,
        },
    )
    jobs = extract_rows(response, "jobs")

    best_role = None
    best_hire_type = None
    best_strength = None
    best_reasoning = None
    best_title = None
    tofu_found = False

    strength_rank = {"strong": 3, "medium": 2, "weak": 1}
    for job in jobs:
        title = job.get("title") or job.get("normalized_job_title") or ""
        description = job.get("description") or ""
        if TOFU_KEYWORD in description.lower():
            tofu_found = True
        role = _classify_role(title) or _classify_role(job.get("normalized_job_title") or "")
        if not role:
            continue
        hire_type = _infer_hire_type(company, role)
        strength, reasoning = _classify_signal(company, role, hire_type, description)
        if best_strength is None or strength_rank.get(strength, 0) > strength_rank.get(best_strength, 0):
            best_role, best_hire_type, best_strength, best_reasoning, best_title = role, hire_type, strength, reasoning, title

    company.active_job_title = best_title
    company.hiring_signal_role = best_role
    company.hiring_signal_hire_type = best_hire_type
    company.hiring_signal_strength = best_strength
    company.hiring_signal_reasoning = (best_reasoning or "") + (" [TOFU pipeline keyword found in JD]" if tofu_found else "")
    db.commit()

    return {
        "role": best_role,
        "hire_type": best_hire_type,
        "strength": best_strength,
        "reasoning": company.hiring_signal_reasoning,
        "postings_checked": len(jobs),
    }
