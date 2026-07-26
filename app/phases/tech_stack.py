"""
Outbound-channel and tech-stack detection (Signal Framework v2, section 4).

Uses `findymail_lookup_technologies_by_domain` -- bills 0.69 credits per result found, free
when nothing detected (same billing pattern as bloomberry_search_job_postings). One call per
company, classified locally against three keyword lists: general outbound/CRM tooling, AI SDR
tools specifically, and a broader AI-in-sales signal.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Company
from app.deepline_client import DeeplineError, execute_tool, extract_rows

# General outbound/CRM/sequencer tooling -- evidence of an existing outbound motion (LinkedIn/
# email), per Signal Framework v2's "should already be using LinkedIn/email as outbound" check.
OUTBOUND_TOOL_KEYWORDS = [
    "apollo", "clay", "hubspot", "outreach", "salesloft", "lemlist", "instantly",
    "salesforce", "linkedin sales navigator", "zoominfo", "lusha",
]

# AI SDR / autonomous outbound tools specifically -- a bonus signal, not required.
AI_SDR_TOOL_KEYWORDS = ["11x", "artisan", "regie", "aisdr", "piper", "qualified.com"]

# Broader AI-in-sales signal, independent of a specific named tool.
AI_SALES_KEYWORDS = ["ai", "artificial intelligence", "gpt", "llm", "machine learning"] + AI_SDR_TOOL_KEYWORDS


def check_tech_stack(company: Company, db: Session) -> dict:
    if not company.domain:
        return {"tech_stack": [], "has_outbound_tooling": None, "has_ai_sdr_tool": None, "error": "no domain"}

    error = None
    tech_names: list[str] = []
    try:
        response = execute_tool("findymail_lookup_technologies_by_domain", {"domain": company.domain})
        rows = extract_rows(response, "technologies")
        tech_names = [str(t.get("name", t) if isinstance(t, dict) else t).lower() for t in rows]
    except DeeplineError as e:
        error = str(e)

    combined = " ".join(tech_names)
    has_outbound_tooling = any(kw in combined for kw in OUTBOUND_TOOL_KEYWORDS) if tech_names else None
    has_ai_sdr_tool = any(kw in combined for kw in AI_SDR_TOOL_KEYWORDS) if tech_names else None

    company.detected_tech_stack = tech_names
    company.has_outbound_tooling = has_outbound_tooling
    company.has_ai_sdr_tool = has_ai_sdr_tool
    company.tech_stack_checked_at = datetime.utcnow()
    db.commit()

    return {
        "tech_stack": tech_names,
        "has_outbound_tooling": has_outbound_tooling,
        "has_ai_sdr_tool": has_ai_sdr_tool,
        "error": error,
    }


def run_tech_stack_check(batch_id: int, db: Session) -> dict:
    companies = db.query(Company).filter(Company.batch_id == batch_id).all()
    checked = 0
    skipped = 0
    errors = []
    for company in companies:
        if company.tech_stack_checked_at:
            skipped += 1
            continue
        result = check_tech_stack(company, db)
        checked += 1
        if result["error"]:
            errors.append(f"{company.name}: {result['error']}")
    return {"companies_checked": checked, "companies_skipped_already_checked": skipped, "errors": errors}
