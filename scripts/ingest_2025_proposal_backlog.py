"""One-off ingestion of the 2025 Sales Progress backlog (Meetings <> Proposals <> Pipeline sheet)
into the new `proposals` table, cross-matched against the real proposal documents Majji shared
via Drive (already extracted to plain text at ~/Downloads/extracted_proposals_data.md).

Deliberately does NOT touch `companies`/`contacts` -- see Proposal's own docstring in
app/db/models.py for why. Re-run safe: skips any company_name+source pair already imported.
"""
import re
import sys

sys.path.insert(0, ".")

from app.db.session import SessionLocal
from app.db.models import Proposal

ELEPHANT_EDGE_TENANT_ID = 2
DOC_DUMP_PATH = "/Users/kishanbm/Downloads/extracted_proposals_data.md"

# Hand-transcribed from the "Sales Progress - Meetings" sheet PDF (the "Meetings <> Proposals <>
# Pipeline" tab). status/icp_fit are the sheet's own calls, not re-derived. monthly_value is
# whatever number appeared in the sheet's rightmost column, unit unconfirmed.
ROWS = [
    # -- summary rows at the very top of the sheet, no meeting/date/status attached --
    dict(company_name="Vtricks", sent_period="2025 - Proposals/Pipeline", status="unknown", monthly_value=1.2),
    dict(company_name="SilCarb", sent_period="2025 - Proposals/Pipeline", status="unknown", monthly_value=2.5),
    dict(company_name="TechGenie", sent_period="2025 - Proposals/Pipeline", status="unknown", monthly_value=2.5),

    # -- January - February --
    dict(company_name="Kushal", linkedin_url="https://www.linkedin.com/company/quosphere/", sent_period="January-February 2025",
         what_they_asked_for="He wanted us to help him launch his product", why_not_closed="Budget", icp_fit="no", status="sent"),
    dict(company_name="E-Shipz", linkedin_url="https://www.eshipz.com/", sent_period="January-February 2025", icp_fit="yes", status="unknown"),
    dict(company_name="Flixer", linkedin_url="https://www.linkedin.com/company/flixir-solutions/people/", sent_period="January-February 2025", icp_fit="no", status="unknown"),
    dict(company_name="Manohar", linkedin_url="https://www.linkedin.com/in/manohardurai/", sent_period="January-February 2025", icp_fit="yes", status="in_pipeline", monthly_value=2.5),
    dict(company_name="Sparkplus", linkedin_url="https://www.linkedin.com/company/sparkplustech/", sent_period="January-February 2025", icp_fit="yes", status="sent"),
    dict(company_name="WowLabz", linkedin_url="https://wowlabz.com/", sent_period="January-February 2025", icp_fit="yes", status="sent"),
    dict(company_name="DynamicMonks", linkedin_url="https://www.dynamicsmonk.com/", sent_period="January-February 2025", icp_fit="yes", status="unknown"),
    dict(company_name="BSE Tech", linkedin_url="https://www.bsetec.com/", sent_period="January-February 2025", icp_fit="yes", status="in_pipeline"),
    dict(company_name="BotGauge", linkedin_url="https://www.botgauge.com/", sent_period="January-February 2025", icp_fit="yes", status="in_pipeline"),
    dict(company_name="Uday", linkedin_url="https://www.linkedin.com/in/dubhaskar/", sent_period="January-February 2025", icp_fit="no", status="sent"),
    dict(company_name="HummingWave", linkedin_url="https://www.hummingwave.com/", sent_period="January-February 2025", icp_fit="yes", status="unknown"),
    dict(company_name="Scoop", linkedin_url="https://www.linkedin.com/company/scoop-apps/", sent_period="January-February 2025", icp_fit="yes", status="in_pipeline"),

    # -- March --
    dict(company_name="Aroopa", linkedin_url="https://www.linkedin.com/company/aroopa-inc/people/", sent_period="March 2025",
         why_not_closed="Already Outbound team working", icp_fit="yes", status="unknown"),
    dict(company_name="Geojit Tech", linkedin_url="https://www.linkedin.com/company/gtl-archived/about/", sent_period="March 2025",
         why_not_closed="2nd meeting/ Proposal stage", icp_fit="yes", status="in_pipeline", monthly_value=2.5),
    dict(company_name="BindBee", linkedin_url="https://www.linkedin.com/company/bindbee/", sent_period="March 2025",
         why_not_closed="Wants quick results at enterprise level", icp_fit="yes", status="unknown"),
    dict(company_name="Hyreo", linkedin_url="https://www.linkedin.com/company/hyreo/people/", sent_period="March 2025", icp_fit="yes", status="unknown"),
    dict(company_name="Zero Pixels", linkedin_url="http://linkedin.com/company/zero-pixels/about/", sent_period="March 2025",
         why_not_closed="2nd meeting Scheduled", icp_fit="yes", status="unknown", monthly_value=2.5),
    dict(company_name="Cyber Square", linkedin_url="https://www.linkedin.com/company/cybersquare-aiandroboticsforschools/about/", sent_period="March 2025", status="unknown"),
    dict(company_name="Unico Connect", linkedin_url="https://www.linkedin.com/company/unico-connect/", sent_period="March 2025", icp_fit="yes", status="in_pipeline", monthly_value=2),
    dict(company_name="SEQATO", linkedin_url="https://www.linkedin.com/company/seqato/", sent_period="March 2025", icp_fit="yes", status="sent", monthly_value=1.2),
    dict(company_name="Softonics", sent_period="March 2025", status="sent", monthly_value=2.5),

    # -- April / May --
    dict(company_name="Mitigata", sent_period="April-May 2025", status="unknown"),
    dict(company_name="Nablasol", sent_period="April-May 2025", status="unknown", monthly_value=1.5),
    dict(company_name="Vodex", sent_period="April-May 2025", status="unknown", monthly_value=2.2),
]

# company_name (sheet) -> substrings to look for among "# FILE: ..." headers in the doc dump.
# Deliberately manual, not fuzzy-matched -- a wrong auto-match would attach the wrong client's
# real proposal content to a different company's row.
DOC_MATCHES = {
    "SilCarb": ["SilCarb"],
    "TechGenie": ["TechGene"],
    "Geojit Tech": ["Geojit"],
    "Zero Pixels": ["Zero Pixels"],
    "Unico Connect": ["Unico Connect", "UNICO Connect"],
    "SEQATO": ["SEQATO"],
    "Softonics": ["Softnotions"],
    "Vodex": ["Vodex"],
}


def load_doc_dump():
    with open(DOC_DUMP_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    parts = re.split(r"^# FILE: (.+)$", content, flags=re.MULTILINE)
    # parts[0] is anything before the first header (should be empty); after that it alternates
    # [filename, body, filename, body, ...]
    docs = {}
    for i in range(1, len(parts), 2):
        filename = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        docs[filename] = body.strip()
    return docs


def find_doc_for_company(company_name, docs):
    substrings = DOC_MATCHES.get(company_name)
    if not substrings:
        return None, None
    for filename, body in docs.items():
        for s in substrings:
            if s.lower() in filename.lower():
                return filename, body
    return None, None


def main():
    docs = load_doc_dump()
    print(f"Loaded {len(docs)} documents from {DOC_DUMP_PATH}")

    db = SessionLocal()
    try:
        created, skipped, matched_docs = 0, 0, 0
        for row in ROWS:
            existing = (
                db.query(Proposal)
                .filter(
                    Proposal.tenant_id == ELEPHANT_EDGE_TENANT_ID,
                    Proposal.company_name == row["company_name"],
                    Proposal.source == "2025_backlog",
                )
                .first()
            )
            if existing:
                skipped += 1
                continue

            filename, body = find_doc_for_company(row["company_name"], docs)
            if filename:
                matched_docs += 1

            proposal = Proposal(
                tenant_id=ELEPHANT_EDGE_TENANT_ID,
                company_name=row["company_name"],
                linkedin_url=row.get("linkedin_url"),
                what_they_asked_for=row.get("what_they_asked_for"),
                why_not_closed=row.get("why_not_closed"),
                icp_fit=row.get("icp_fit", "unknown"),
                monthly_value=row.get("monthly_value"),
                status=row.get("status", "unknown"),
                sent_period=row.get("sent_period"),
                proposal_document_filename=filename,
                proposal_document_text=body,
                source="2025_backlog",
            )
            db.add(proposal)
            created += 1

        db.commit()
        print(f"Created: {created}, skipped (already imported): {skipped}, matched to a real doc: {matched_docs}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
