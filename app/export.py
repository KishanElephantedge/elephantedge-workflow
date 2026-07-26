"""Generates a CSV export of the decision-makers found in a batch -- attached to the
pre-outreach approval-window email so it can be reviewed before anything gets pushed to a
real outreach channel."""

import csv
import io

from sqlalchemy.orm import Session

from app.db.models import Batch, Company


def generate_decision_makers_csv(batch_id: int, db: Session) -> bytes:
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    companies = db.query(Company).filter(Company.batch_id == batch_id).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "Company", "Domain", "Tier", "Score", "Hiring Signal", "Hiring Signal Reasoning",
        "Decision Maker", "Title", "Thread Role", "LinkedIn URL",
    ])
    for company in companies:
        tier = company.score.tier if company.score else ""
        score = company.score.total_score if company.score else ""
        if company.contacts:
            for contact in company.contacts:
                writer.writerow([
                    company.name, company.domain, tier, score,
                    company.hiring_signal_role or "", company.hiring_signal_reasoning or "",
                    f"{contact.first_name or ''} {contact.last_name or ''}".strip(),
                    contact.title or "", contact.thread_role or "", contact.linkedin_url or "",
                ])
        else:
            writer.writerow([company.name, company.domain, tier, score, company.hiring_signal_role or "", company.hiring_signal_reasoning or "", "", "", "", ""])

    return buffer.getvalue().encode("utf-8")
