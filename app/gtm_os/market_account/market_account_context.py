"""Market -> Account context -- Batch 11. Deterministic, no LLM, no embeddings, no fuzzy
matching. See app/gtm_os/market_account/__init__.py for the one relationship this module
recognizes as defensible (Case B: the same GtmSignal is both company-linked and topic-linked).

WHAT DOES NOT CREATE A RELATIONSHIP (Part 3/5's own explicit list, enforced by construction, not
by a runtime check): a trending topic alone; text-based company-name matching against article
content; any signal that lacks a real `company_id` (HN/RSS signals never carry one -- confirmed
by inspecting sensing.py, see module docstring below); any inferred/fuzzy similarity between a
company's industry/description and a topic's name. None of these code paths exist in this module.

REUSES, DOES NOT DUPLICATE:
  - trend_intelligence.evaluate_topic_trend() -- the single source of truth for trend
    classification (state/counts/explanation), called unmodified per topic found via the bridge.
  - ContentTopic/ContentTopicEvidence/GtmSignal -- read-only, no new evidence rows ever written.
  - Company/Contact -- read-only company identity, mirroring the same minimal-fetch pattern
    icp_matching.py and sales_agent.py already use independently (no shared helper exists for a
    bare company_id -> identity lookup, so this is not a duplicated structure, just a plain read).

NOT THE SAME AS strategy.py's market_context() (Batch 6): that function deliberately returns
GENERAL, tenant-wide trending topics, NOT linked to any specific company -- used only as loose
contextual color in GtmStrategy's reasoning_note. This module is the opposite and stricter: it
only ever returns topics with a real, row-level, company-specific link. Both are correct, for
different purposes; neither duplicates the other.

DATA REALITY (Part 17): as of this batch, only linkedin_job/theirstack_job/linkedin_reply/
web_search/company_website signals ever carry a real company_id (confirmed via
app/gtm_os/intelligence/sensing.py). hackernews_story/rss_article signals -- the two sources that
produce the large majority of real ContentTopicEvidence today -- never set company_id. So this
bridge is REAL and CORRECT, but is expected to find few or zero connections against current real
data until a company-linked source's signal also happens to match a configured topic. This is
reported honestly in the final report, not worked around."""

from app.db.models import Company, Contact
from app.gtm_os.content.topic import ContentTopic, ContentTopicEvidence
from app.gtm_os.content.trend_config import get_trend_config
from app.gtm_os.content.trend_intelligence import evaluate_topic_trend
from app.gtm_os.icp.icp_matching import ICPMatch
from app.gtm_os.intelligence.signal import GtmSignal


def get_market_context_for_company(db, tenant_id: int, company_id: int) -> dict:
    """Pure, read-only. Returns market context ONLY for topics with a real, row-level,
    company-specific evidence link (see module docstring) -- never a company-wide "here's
    what's trending" dump."""
    result = {
        "company_id": company_id,
        "market_context": [],
        "missing_information": [],
    }

    company = db.get(Company, company_id)
    if company is None:
        result["missing_information"].append(f"company_id {company_id} does not resolve to a real Company row")
        return result

    # The bridge itself: GtmSignal rows that are BOTH linked to this company (via their own
    # company_id) AND linked to a ContentTopic (via ContentTopicEvidence) -- the same
    # observation, viewed from two angles, never an inference.
    bridge_rows = (
        db.query(ContentTopicEvidence, GtmSignal)
        .join(GtmSignal, ContentTopicEvidence.gtm_signal_id == GtmSignal.id)
        .filter(
            ContentTopicEvidence.tenant_id == tenant_id,
            GtmSignal.tenant_id == tenant_id,
            GtmSignal.company_id == company_id,
        )
        .all()
    )

    if not bridge_rows:
        result["missing_information"].append(
            "no GtmSignal linked to this company is also linked to any ContentTopic -- "
            "no defensible market-to-account relationship exists yet for this company"
        )
        return result

    evidence_by_topic: dict[int, list[tuple[ContentTopicEvidence, GtmSignal]]] = {}
    for evidence, signal in bridge_rows:
        evidence_by_topic.setdefault(evidence.content_topic_id, []).append((evidence, signal))

    trend_config = get_trend_config(db, tenant_id)

    for content_topic_id, rows in evidence_by_topic.items():
        topic = db.get(ContentTopic, content_topic_id)
        if topic is None:
            continue  # defensive -- should not happen given the FK, never fabricate a topic that doesn't exist

        trend_result = evaluate_topic_trend(db, tenant_id, topic, trend_config)

        account_evidence = [
            {
                "gtm_signal_id": signal.id,
                "source": signal.source,
                "matched_term": evidence.matched_term,
                "match_method": evidence.match_method,
                "observed_at": signal.observed_at,
            }
            for evidence, signal in rows
        ]

        result["market_context"].append({
            "content_topic_id": topic.id,
            "topic_name": topic.canonical_name,
            "origin": topic.origin,
            "trend_state": trend_result["state"],
            "trend_explanation": trend_result["explanation"],
            "supporting_market_observations": {
                "recent_observation_count": trend_result["recent_observation_count"],
                "recent_independent_entity_count": trend_result["recent_independent_entity_count"],
                "recent_source_diversity": trend_result["recent_source_diversity"],
            },
            "account_link_reason": (
                f"{len(rows)} GtmSignal(s) directly linked to company_id={company_id} are ALSO "
                f"linked to this ContentTopic via ContentTopicEvidence -- the same observation "
                f"serves as both account evidence and market evidence, not an inference"
            ),
            "account_evidence": account_evidence,
        })

    return result


def get_account_evidence_summary(db, tenant_id: int, company_id: int) -> dict:
    """Read-only account-side identity/evidence, per Part 11's list -- company identity, known
    ICP matches, and known contacts. Deliberately minimal: a bare company_id -> identity lookup,
    not a duplicate of sales_agent.py's fuller gather_account_research() (which requires an
    Opportunity, not just a company_id, and pulls in Problem/Demand evidence out of scope here)."""
    company = db.get(Company, company_id)
    if company is None:
        return {"company_id": company_id, "missing_information": [f"company_id {company_id} does not resolve to a real Company row"]}

    icp_matches = (
        db.query(ICPMatch)
        .filter(ICPMatch.tenant_id == tenant_id, ICPMatch.company_id == company_id)
        .all()
    )
    contacts = db.query(Contact).filter(Contact.company_id == company_id).all()

    return {
        "company_id": company_id,
        "company_name": company.name,
        "industry": company.industry,
        "employee_count": company.employee_count,
        "icp_matches": [{"icp_id": m.icp_id, "reasons": m.reasons} for m in icp_matches],
        "known_contacts": [{"id": c.id, "title": c.title} for c in contacts],
        "missing_information": [] if (icp_matches or contacts) else ["no ICPMatch or Contact rows exist yet for this company"],
    }


def run_market_account_context_sweep(db, tenant_id: int, limit: int = 200) -> dict:
    """Bounded, standalone, tenant-scoped, read-only reporting sweep -- writes NOTHING (pure
    computation, see module docstring's persistence decision: no new table introduced, the whole
    bridge is a join over existing FKs). One company's failure never aborts the sweep. NOT wired
    into the scheduler (per scope boundary). Only companies with at least one GtmSignal carrying
    a company_id are worth iterating -- others can never have a bridge row by construction, so
    this scopes the sweep to real candidates rather than scanning every company blindly."""
    counts = {
        "companies_evaluated": 0,
        "has_market_context": 0,
        "no_market_context": 0,
        "failed": 0,
    }

    company_ids = [
        row[0]
        for row in db.query(GtmSignal.company_id)
        .filter(GtmSignal.tenant_id == tenant_id, GtmSignal.company_id.isnot(None))
        .distinct()
        .order_by(GtmSignal.company_id)
        .limit(limit)
        .all()
    ]

    for company_id in company_ids:
        try:
            counts["companies_evaluated"] += 1
            result = get_market_context_for_company(db, tenant_id, company_id)
            if result["market_context"]:
                counts["has_market_context"] += 1
            else:
                counts["no_market_context"] += 1
        except Exception:  # noqa: BLE001 -- one company's failure must never block the others
            # Roll back before anything else: a per-item failure must not poison the SHARED session.
            # Neon drops idle connections, and this sweep idles ~40s per LLM call under Gemini rate
            # limiting -- when that lands mid-loop the transaction is left invalid and EVERY later item
            # AND stage dies with PendingRollbackError. Confirmed live: run 122 lost its connection
            # during topic_linking, then burned 233 minutes failing everything after it. The stage-level
            # handler already rolled back; these per-item ones did not.
            db.rollback()
            counts["failed"] += 1

    return counts
