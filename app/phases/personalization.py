"""
Phase 11 — Personalization (see phase11-personalization.md).

Priority order for the message hook is deliberately different from Phase 9's scoring priority:
scoring treats all three signals equally (no evidence yet to weight one above another), but for
writing a *sentence*, specificity matters -- a first-party declared fact (an active job posting)
is more credible to reference than an inferred proxy (headcount growth).
"""

from app.db.models import Company, Contact


def compute_personalization_hook(company: Company, contact: Contact) -> str:
    name = contact.first_name or "there"

    if company.active_head_of_sales_posting:
        return f"Hi {name} — noticed {company.name} is actively hiring for a sales leadership role."
    if company.last_funding_date:
        return f"Hi {name} — congrats on {company.name}'s recent funding."
    if (company.headcount_growth_12m_percent or 0) > 15:
        return f"Hi {name} — noticed {company.name} has been growing the team quickly."
    return f"Hi {name} — came across {company.name} and wanted to connect."
