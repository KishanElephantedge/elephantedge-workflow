"""GTM Governance Readout -- Batch 14. Pure, read-only, tenant-scoped aggregation. Every number
in evaluate_gtm_governance() comes from calling an already-existing sweep/eval function verbatim,
or a single direct COUNT query over an already-existing column -- nothing here recomputes a
decision another module owns, and nothing here is persisted.

REUSED VERBATIM (no reimplementation, no second version of any metric):
    run_account_agent_sweep()          (Batch 12) -- account_distribution + the pipeline funnel's
                                                       cumulative company-state counts
    run_offering_matching_sweep()      (Batch 9)  -- offering_matched pipeline stage
    run_gtm_motion_sweep()             (Batch 10) -- motion_ready pipeline stage
    run_execution_readiness_sweep()    (Batch 13) -- execution_readiness + message-lifecycle
                                                       pipeline stages
    evaluate_learning_readout()        (Batch 7)  -- learning section, verbatim
    run_trend_intelligence_sweep()     (Batch 6)  -- market_intelligence trend-state distribution
    run_market_account_context_sweep() (Batch 11) -- market_intelligence account-bridge coverage
    get_offering_config() / get_gtm_motion_config() / get_business_context() -- read directly to
        surface already-known configuration gaps (e.g. offering_config's own "no applicable_icps"
        marker, gtm_motion_config's own "no applicable_icps/offerings" marker) -- never a second
        config system, just reading what each already stores.

PIPELINE FUNNEL (Part 3): built by taking run_account_agent_sweep()'s own mutually-exclusive
"furthest state reached" bucket counts and summing them CUMULATIVELY from the strongest state
downward -- e.g. "reached icp_matched or further" = icp_matched + opportunity_identified +
strategy_ready + sales_ready bucket counts. This is arithmetic over Batch 12's own real numbers,
not a new classification: no company is ever placed in a stage its own account_status doesn't
already support. offering_matched/motion_ready/execution-lifecycle stages are appended using
their own sweep's own denominator (companies for the first two, opportunities for the
execution-lifecycle ones) -- each stage's `denominator`/`source` names exactly which population
and function it came from, so a reader never mistakes one denominator for another.

BOTTLENECK DETECTION (Part 4): the largest drop is computed ONLY across the single, consistent,
company-denominator cumulative funnel (companies -> identified_or_further -> ... ->
sales_ready_or_further) -- never mixed with the opportunity-denominator execution stages, which
would make a "drop" meaningless (different populations). The reason attached to a detected drop
is only ever a fact already surfaced elsewhere in this same readout (e.g. an unconfigured ICP/
offering/motion, or a real missing_information string) -- never an invented causal explanation.

CONFIGURATION vs DATA vs OPERATIONAL (Part 14) -- kept in three separate lists, never collapsed:
    configuration_gaps -- a Parameter-backed rule is absent/incomplete (offering has no
        applicable_icps, motion has no applicable_icps/offerings, business_context goal fields
        are still None)
    data_gaps          -- a real Company/Contact field is missing on real rows (revenue,
        employee_count, no known contact) -- counted via direct COUNT queries, nothing inferred
    operational_issues -- a real recorded operational failure (AutonomousRun.status == "failed"
        with a real error_message) for this tenant. Empty when none exist -- never fabricated to
        fill the category (Part 21's own explicit instruction).

PERSISTENCE DECISION: NO new table. evaluate_gtm_governance() is pure and computed on demand --
every input is a cheap, already-indexed query or an already-existing sweep call. Nothing here
needs its own history; each underlying layer already owns whatever history it needs."""

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, JSON, String
from sqlalchemy.orm import Session

from app.db.models import AutonomousRun, Base, Batch, Company, Contact
from app.gtm_os.account_agent.account_agent import ACCOUNT_STATES_ORDER, run_account_agent_sweep
from app.gtm_os.content.topic import ContentTopic, ContentTopicEvidence
from app.gtm_os.content.trend_intelligence import run_trend_intelligence_sweep
from app.gtm_os.context.business_context import get_business_context
from app.gtm_os.execution.execution_readiness import run_execution_readiness_sweep
from app.gtm_os.gtm_motion.gtm_motion import run_gtm_motion_sweep
from app.gtm_os.gtm_motion.gtm_motion_config import get_gtm_motion_config
from app.gtm_os.icp.icp_offering_matching import run_offering_matching_sweep
from app.gtm_os.learning.evaluation import evaluate_learning_readout
from app.gtm_os.market_account.market_account_context import run_market_account_context_sweep
from app.gtm_os.opportunity.offering_config import get_offering_config

# Strongest -> weakest, the reverse of account_agent.ACCOUNT_STATES_ORDER, used to compute
# cumulative "reached this state or further" funnel counts (see module docstring's PIPELINE
# FUNNEL section).
_STATES_STRONGEST_FIRST = list(reversed(ACCOUNT_STATES_ORDER))

# Every real `stage` value _pipeline_stages()/_detect_bottleneck() can produce -> a plain-English
# label. Added alongside the V2 UI audit (2026-08-18) so the Briefing bottleneck card never has to
# render a raw internal stage key like "icp_matched_or_further" -- the backend, which owns these
# names, is the one place they're spelled out, not duplicated as a second map in the frontend.
_STAGE_LABELS = {
    "companies": "Companies",
    "identified_or_further": "Identified",
    "icp_matched_or_further": "ICP matched",
    "opportunity_identified_or_further": "Opportunity identified",
    "strategy_ready_or_further": "Strategy ready",
    "sales_ready_or_further": "Sales ready",
    "offering_matched": "Offering matched",
    "motion_ready": "Motion ready",
    "execution_ready": "Execution ready",
    "message_ready": "Message ready",
    "approved": "Approved",
}


def _stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage, stage.replace("_", " ").title())


def _company_universe_count(db: Session, tenant_id: int) -> int:
    return db.query(Company.id).join(Batch, Company.batch_id == Batch.id).filter(Batch.tenant_id == tenant_id).count()


def _pct(count: int, denominator: int | None) -> float | None:
    if not denominator:
        return None
    return round(count / denominator * 100, 2)


def _pipeline_stages(total_companies: int, account_sweep: dict, offering_sweep: dict, motion_sweep: dict, execution_sweep: dict) -> list[dict]:
    account_state_stages = []
    cumulative = 0
    for state in _STATES_STRONGEST_FIRST:  # sales_ready ... insufficient_context
        cumulative += account_sweep.get(state, 0)
        if state == "insufficient_context":
            continue  # "reached insufficient_context or further" always == total companies -- not a meaningful funnel step
        stage_name = f"{state}_or_further"
        account_state_stages.append({
            "stage": stage_name,
            "count": cumulative,
            "denominator": total_companies,
            "percentage": _pct(cumulative, total_companies),
            "source": "run_account_agent_sweep (Batch 12), cumulative over account_status buckets",
        })
    account_state_stages.reverse()  # present weakest -> strongest, matching the funnel reading order

    stages = [{"stage": "companies", "count": total_companies, "denominator": None, "percentage": None, "source": "Company"}]
    stages.extend(account_state_stages)

    offering_denominator = offering_sweep.get("companies_evaluated", 0)
    stages.append({
        "stage": "offering_matched",
        "count": offering_sweep.get("has_candidate_offering", 0),
        "denominator": offering_denominator,
        "percentage": _pct(offering_sweep.get("has_candidate_offering", 0), offering_denominator),
        "source": "run_offering_matching_sweep (Batch 9)",
    })

    motion_denominator = motion_sweep.get("companies_evaluated", 0)
    stages.append({
        "stage": "motion_ready",
        "count": motion_sweep.get("recommended", 0),
        "denominator": motion_denominator,
        "percentage": _pct(motion_sweep.get("recommended", 0), motion_denominator),
        "source": "run_gtm_motion_sweep (Batch 10)",
    })

    execution_denominator = execution_sweep.get("evaluated", 0)
    message_ready_count = execution_sweep.get("message_generation_available", 0) + execution_sweep.get("ready_for_review", 0) + execution_sweep.get("approved", 0)
    stages.append({
        "stage": "execution_ready",
        "count": total_companies and (account_sweep.get("opportunity_identified", 0) + account_sweep.get("strategy_ready", 0) + account_sweep.get("sales_ready", 0)),
        "denominator": total_companies,
        "percentage": None,  # deliberately not computed -- this stage's numerator (companies with an Opportunity) and the execution sweep's own denominator (Opportunities) are different populations; see module docstring's BOTTLENECK DETECTION note
        "source": "run_account_agent_sweep (Batch 12) -- companies with >=1 Opportunity",
    })
    stages.append({
        "stage": "message_ready",
        "count": message_ready_count,
        "denominator": execution_denominator,
        "percentage": _pct(message_ready_count, execution_denominator),
        "source": "run_execution_readiness_sweep (Batch 13), denominator = Opportunities evaluated",
    })
    stages.append({
        "stage": "approved",
        "count": execution_sweep.get("approved", 0),
        "denominator": execution_denominator,
        "percentage": _pct(execution_sweep.get("approved", 0), execution_denominator),
        "source": "run_execution_readiness_sweep (Batch 13), denominator = Opportunities evaluated",
    })
    for s in stages:
        s["label"] = _stage_label(s["stage"])
    return stages


def _detect_bottleneck(company_funnel_stages: list[dict], configuration_gaps: list[dict]) -> dict | None:
    """Largest drop across the SINGLE consistent company-denominator funnel only (Part 4) --
    never compares stages with different denominators. `reason` is only ever a fact already
    present in configuration_gaps for this same readout, never invented."""
    if len(company_funnel_stages) < 2:
        return None

    largest = None
    for prev, curr in zip(company_funnel_stages, company_funnel_stages[1:]):
        drop = prev["count"] - curr["count"]
        if drop <= 0:
            continue
        if largest is None or drop > largest["drop_count"]:
            reason = None
            reason_short = None
            for gap in configuration_gaps:
                if curr["stage"].startswith(gap.get("relates_to_stage") or "\0"):
                    reason = gap["description"]
                    reason_short = gap.get("short_description")
                    break
            largest = {
                "from": prev["stage"],
                "to": curr["stage"],
                "from_label": _stage_label(prev["stage"]),
                "to_label": _stage_label(curr["stage"]),
                "drop_count": drop,
                "reason": reason or "no specific configuration gap traced to this stage in this readout -- see data_gaps/configuration_gaps for other possibly-relevant facts",
                # Plain-English version of `reason`, for the primary UI -- None when `reason`
                # itself is the fallback "no specific gap traced" sentence above (already short).
                "reason_short": reason_short,
            }
    return largest


def _configuration_gaps(db: Session, tenant_id: int) -> list[dict]:
    """`title`/`short_description` (added alongside this UI audit, 2026-08-18) are plain-English
    restatements of the SAME fact `description` already states in full sentence form -- never a
    second, different claim. Added because the Briefing UI must not render raw sentences like
    "offering 'Sales OS' has no applicable_icps configured -- it can never produce a
    candidate_match" inline; `description`/`source` stay exactly as they were for the detail view
    (BriefingCategoryDetail.jsx), which is allowed to show the full technical explanation."""
    gaps: list[dict] = []

    offerings = get_offering_config(db, tenant_id)
    for offering in offerings:
        if not offering.get("applicable_icps"):
            gaps.append({
                "category": "configuration",
                "relates_to_stage": "offering_matched",
                "title": offering["name"],
                "short_description": "No ICP mapping configured",
                "description": f"offering {offering['name']!r} has no applicable_icps configured -- it can never produce a candidate_match",
                "source": "offering_config.get_offering_config",
            })

    motions = get_gtm_motion_config(db, tenant_id)
    for motion in motions:
        if not motion.get("applicable_icps") and not motion.get("applicable_offerings"):
            gaps.append({
                "category": "configuration",
                "relates_to_stage": "motion_ready",
                "title": f"{motion['motion'].replace('_', ' ').title()} motion",
                "short_description": "No ICP/offering mapping configured",
                "description": f"GTM motion {motion['motion']!r} has no applicable_icps or applicable_offerings configured -- it can never be recommended",
                "source": "gtm_motion_config.get_gtm_motion_config",
            })

    business_context = get_business_context(db, tenant_id)
    goals = business_context.get("goals", {})
    missing_goals = [k for k, v in goals.items() if v is None]
    if missing_goals:
        gaps.append({
            "category": "configuration",
            "relates_to_stage": None,
            "title": "Business goals",
            "short_description": f"{len(missing_goals)} goal field{'s' if len(missing_goals) != 1 else ''} not yet set",
            "description": f"business_context goals not yet configured: {sorted(missing_goals)}",
            "source": "business_context.get_business_context",
        })

    return gaps


# Real field -> plain-English label. Only covers the fields _data_gaps() itself actually checks
# (Company.estimated_revenue_lower_usd, Company.employee_count, the derived decision-maker-contact
# check) -- not a general-purpose field-name prettifier, just the ones this function produces.
_DATA_GAP_FIELD_LABELS = {
    "estimated_revenue_lower_usd": "revenue data",
    "employee_count": "employee count",
    "decision_maker_contact": "a decision-maker",
}


def _data_gaps(db: Session, tenant_id: int, total_companies: int) -> list[dict]:
    if total_companies == 0:
        return []

    company_ids_q = db.query(Company.id).join(Batch, Company.batch_id == Batch.id).filter(Batch.tenant_id == tenant_id)

    missing_revenue = company_ids_q.filter(Company.estimated_revenue_lower_usd.is_(None)).count()
    missing_employee_count = company_ids_q.filter(Company.employee_count.is_(None)).count()
    companies_with_contact = (
        db.query(Company.id)
        .join(Batch, Company.batch_id == Batch.id)
        .join(Contact, Contact.company_id == Company.id)
        .filter(Batch.tenant_id == tenant_id)
        .distinct()
        .count()
    )
    missing_contact = total_companies - companies_with_contact

    def _gap(field: str, missing_count: int, source: str) -> dict:
        # short_description mirrors the ChatGPT-reviewed example verbatim: "575 accounts need
        # decision-makers" / "81% of identified accounts don't have X yet" -- percentage only
        # ever shown when computed from these same two real numbers, never fabricated.
        label = _DATA_GAP_FIELD_LABELS[field]
        pct = _pct(missing_count, total_companies)
        return {
            "category": "data", "field": field, "missing_count": missing_count, "denominator": total_companies,
            "title": f"{missing_count} compan{'y' if missing_count == 1 else 'ies'} missing {label}",
            "short_description": f"{pct}% of accounts don't have {label} yet" if pct is not None else None,
            "source": source,
        }

    gaps = []
    if missing_revenue:
        gaps.append(_gap("estimated_revenue_lower_usd", missing_revenue, "Company"))
    if missing_employee_count:
        gaps.append(_gap("employee_count", missing_employee_count, "Company"))
    if missing_contact:
        gaps.append(_gap("decision_maker_contact", missing_contact, "Contact"))
    return gaps


# Deterministic (source, matched-substring) -> short title -- covers the tool names actually
# seen in real recorded AutonomousRun.error_message text for this tenant (confirmed against
# production 2026-08-18). Not a general error-message summarizer: an unmatched message keeps its
# own (truncated) text as the title rather than a guessed label.
_V1_ERROR_TITLE_PATTERNS = [
    ("crustdata", "Crustdata enrichment unavailable"),
    ("linkedin jobs search", "LinkedIn jobs search limit reached"),
    ("linkedin", "LinkedIn integration issue"),
    ("theirstack", "TheirStack search issue"),
]


def _title_for_v1_error(message: str) -> str:
    lowered = message.lower()
    for needle, title in _V1_ERROR_TITLE_PATTERNS:
        if needle in lowered:
            return title
    return message[:60] + ("…" if len(message) > 60 else "")


def _operational_issues(db: Session, tenant_id: int, limit: int = 5) -> list[dict]:
    """Only reports REAL, already-recorded run failures for this tenant -- never infers
    operational health from anything else. Empty when no failed run exists.

    V2 UI audit finding (2026-08-18): this used to read ONLY AutonomousRun -- the V1 discovery
    pipeline's own run log (companies_discovered/contacts_found/credits_spent_usd -- unmistakably
    V1-shaped columns, confirmed by reading app/db/models.py directly). That meant the Briefing
    page's "Operational Issues" card was 100% legacy V1 failures (e.g. old Crustdata/LinkedIn-jobs
    errors) while the NEW GTM-OS pipeline's own real failures (e.g. GtmIntelligenceRun's
    theirstack_job hitting insufficient Deepline credits) were completely invisible on this page.
    Every item below now carries `system`: "v1_discovery" or "gtm_os", so the frontend can label
    them honestly instead of implying both are the same "system health." Neither source is
    removed or altered -- this only adds the missing GTM-OS half and a label distinguishing them."""
    failed_runs = (
        db.query(AutonomousRun)
        .join(Batch, AutonomousRun.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id, AutonomousRun.status == "failed")
        .order_by(AutonomousRun.started_at.desc())
        .limit(limit)
        .all()
    )
    issues = [
        {
            "category": "operational",
            "system": "v1_discovery",
            "run_id": run.id,
            "started_at": run.started_at,
            "title": _title_for_v1_error(run.error_message),
            # truncated for readability only -- the full message remains on the real
            # AutonomousRun row (Batch 3's own field), never lost, just not repeated here
            "error_message": (run.error_message[:300] + "...") if len(run.error_message) > 300 else run.error_message,
            "source": "AutonomousRun",
        }
        for run in failed_runs
        if run.error_message
    ]

    from app.gtm_os.orchestration.sweep import GtmIntelligenceRun

    non_completed_runs = (
        db.query(GtmIntelligenceRun)
        .filter(GtmIntelligenceRun.tenant_id == tenant_id, GtmIntelligenceRun.status.in_(["failed", "partial"]))
        .order_by(GtmIntelligenceRun.started_at.desc())
        .limit(limit)
        .all()
    )
    for run in non_completed_runs:
        # error_summary is the short human-readable field (sweep.py's finish_gtm_intelligence_run
        # sets it from whichever stage actually failed) -- stage_results has the full detail if a
        # future detail view needs it, deliberately not repeated here.
        message = run.error_summary or f"run {run.id} finished with status={run.status!r} but no error_summary was recorded"
        issues.append({
            "category": "operational",
            "system": "gtm_os",
            "run_id": run.id,
            "started_at": run.started_at,
            "title": "GTM-OS sweep issue",
            "error_message": (message[:300] + "...") if len(message) > 300 else message,
            "source": "GtmIntelligenceRun",
        })

    return issues


def _market_intelligence(db: Session, tenant_id: int) -> dict:
    trend_counts = run_trend_intelligence_sweep(db, tenant_id, limit=500)
    bridge_counts = run_market_account_context_sweep(db, tenant_id, limit=500)
    total_evidence = db.query(ContentTopicEvidence.id).filter(ContentTopicEvidence.tenant_id == tenant_id).count()
    total_topics = db.query(ContentTopic.id).filter(ContentTopic.tenant_id == tenant_id).count()

    note = None
    if total_evidence > 0 and bridge_counts.get("has_market_context", 0) == 0:
        note = "Market intelligence is currently not reaching account-level context."

    return {
        "total_topics": total_topics,
        "total_evidence_rows": total_evidence,
        "trend_state_distribution": {k: v for k, v in trend_counts.items() if k not in ("dry_run",)},
        "account_bridge": bridge_counts,
        "note": note,
    }


def _human_attention(configuration_gaps: list[dict], data_gaps: list[dict], operational_issues: list[dict], account_sweep: dict, execution_sweep: dict, market_intelligence: dict) -> list[dict]:
    """Deterministic list of already-computed observations (Part 11) -- never a new computation,
    purely a curated pass over what this same readout already found. Never creates a task or
    changes anything (Part 11's own boundary).

    `title`/`subtitle` (added with the V2 UI audit, 2026-08-18) reuse each source's own
    title/short_description where that source already computed one (configuration_gaps/
    data_gaps/operational_issues), so this never re-derives a claim -- it just carries the
    already-real short form through. `description` is kept unchanged for the detail view."""
    items: list[dict] = []

    if account_sweep.get("identified", 0) > 0:
        items.append({
            "category": "coverage", "title": f"{account_sweep['identified']} companies", "subtitle": "No ICP match yet",
            "description": f"{account_sweep['identified']} identified companies currently have no ICP match",
        })
    if account_sweep.get("icp_matched", 0) > 0:
        items.append({
            "category": "coverage", "title": f"{account_sweep['icp_matched']} companies", "subtitle": "ICP matched, no Opportunity yet",
            "description": f"{account_sweep['icp_matched']} ICP-matched companies have not yet produced an Opportunity",
        })

    for gap in configuration_gaps:
        items.append({"category": "configuration", "title": gap.get("title"), "subtitle": gap.get("short_description"), "description": gap["description"]})
    for gap in data_gaps:
        items.append({"category": "data", "title": gap.get("title"), "subtitle": gap.get("short_description"), "description": f"{gap['missing_count']}/{gap['denominator']} companies missing {gap['field']}"})
    for issue in operational_issues:
        items.append({
            "category": "operational", "title": issue.get("title"), "subtitle": issue.get("system") == "gtm_os" and "New GTM-OS pipeline" or "V1 discovery pipeline",
            "description": f"{issue['source']} {issue['run_id']} failed: {issue['error_message']}",
        })

    if execution_sweep.get("blocked", 0) > 0:
        items.append({
            "category": "execution", "title": f"{execution_sweep['blocked']} opportunities", "subtitle": "Blocked on a sales-readiness prerequisite",
            "description": f"{execution_sweep['blocked']} opportunities are currently blocked on a sales-readiness prerequisite",
        })
    if execution_sweep.get("ready_for_review", 0) > 0:
        items.append({
            "category": "execution", "title": f"{execution_sweep['ready_for_review']} message drafts", "subtitle": "Awaiting your review/approval",
            "description": f"{execution_sweep['ready_for_review']} message drafts are awaiting human review/approval",
        })

    if market_intelligence.get("note"):
        items.append({"category": "market_intelligence", "title": "Market intelligence", "subtitle": market_intelligence["note"], "description": market_intelligence["note"]})

    return items


def evaluate_gtm_governance(db: Session, tenant_id: int) -> dict:
    """Pure, read-only. Makes zero writes, zero LLM calls, zero external API calls. Safe to call
    repeatedly with no side effects (Part 19)."""
    total_companies = _company_universe_count(db, tenant_id)

    account_sweep = run_account_agent_sweep(db, tenant_id, limit=100_000)
    offering_sweep = run_offering_matching_sweep(db, tenant_id, limit=100_000)
    motion_sweep = run_gtm_motion_sweep(db, tenant_id, limit=100_000)
    execution_sweep = run_execution_readiness_sweep(db, tenant_id, limit=100_000)
    learning = evaluate_learning_readout(db, tenant_id)
    market_intelligence = _market_intelligence(db, tenant_id)

    configuration_gaps = _configuration_gaps(db, tenant_id)
    data_gaps = _data_gaps(db, tenant_id, total_companies)
    operational_issues = _operational_issues(db, tenant_id)

    pipeline_stages = _pipeline_stages(total_companies, account_sweep, offering_sweep, motion_sweep, execution_sweep)
    company_funnel_stages = [s for s in pipeline_stages if s["stage"] == "companies" or s["stage"].endswith("_or_further")]
    bottleneck = _detect_bottleneck(company_funnel_stages, configuration_gaps)

    account_distribution = {"account_states": {state: account_sweep.get(state, 0) for state in ACCOUNT_STATES_ORDER}, "evaluated": account_sweep.get("evaluated", 0), "failed": account_sweep.get("failed", 0)}

    human_attention = _human_attention(configuration_gaps, data_gaps, operational_issues, account_sweep, execution_sweep, market_intelligence)

    return {
        "overview": {
            "total_companies": total_companies,
            "opportunities_evaluated": execution_sweep.get("evaluated", 0),
        },
        "pipeline": {"stages": pipeline_stages},
        "bottlenecks": [bottleneck] if bottleneck else [],
        "configuration_gaps": configuration_gaps,
        "data_gaps": data_gaps,
        "operational_issues": operational_issues,
        "market_intelligence": market_intelligence,
        "account_distribution": account_distribution,
        "execution_readiness": execution_sweep,
        "learning": learning,
        "human_attention": human_attention,
    }


def run_gtm_governance_readout(db: Session, tenant_id: int) -> dict:
    """Thin, explicitly-named entry point for independent/manual invocation (Part 17) -- NOT a
    second implementation. Identical to evaluate_gtm_governance(); kept as a separate name only
    because the spec names both, and future callers may want a stable "run the readout" verb
    distinct from "evaluate governance facts." NOT wired into the scheduler -- GovernanceSnapshot
    below is; see its own docstring for why this function itself stays untouched."""
    return evaluate_gtm_governance(db, tenant_id)


class GovernanceSnapshot(Base):
    """V2 Briefing performance fix -- evaluate_gtm_governance() itself is unchanged (same
    function, same logic, same numbers); what changes is WHEN it runs. Confirmed directly against
    production (2026-08-18): a single live evaluate_gtm_governance() call synchronously runs 4+
    full sweeps (run_account_agent_sweep/run_offering_matching_sweep/run_gtm_motion_sweep/
    run_execution_readiness_sweep) over all 706 companies -- took over 5 minutes before being
    killed. Computing that inside a GET request is the actual root cause of the Briefing page's
    reported slow load, not a frontend problem.

    Same "own table when the shape genuinely differs" precedent as GtmIntelligenceRun (sweep.py)
    -- mirrors that table's shape (one row per computation, full result JSON, no attempt to make
    the snapshot queryable field-by-field) rather than reusing it, since this stores a governance
    readout, not a sweep-stage result. Every row is a real snapshot of evaluate_gtm_governance()'s
    actual output at computed_at -- nothing here recomputes, filters, or reinterprets it."""
    __tablename__ = "gtm_governance_snapshots"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)

    snapshot = Column(JSON, nullable=False)  # evaluate_gtm_governance()'s full return value, verbatim
    computed_at = Column(DateTime, default=datetime.utcnow)


def compute_and_store_governance_snapshot(db: Session, tenant_id: int) -> dict:
    """The only writer of GovernanceSnapshot. Called from the hourly scheduler
    (_scheduled_governance_snapshot, app/main.py) and from the manual "Refresh now" route
    (POST /gtm-os/briefing-governance/refresh) -- same function either way, no separate
    "scheduled" vs "manual" computation path to keep in sync.

    evaluate_gtm_governance()'s real output contains real datetime objects (e.g.
    operational_issues[].started_at, from AutonomousRun/GtmIntelligenceRun's own DateTime
    columns) -- confirmed via a local-DB test run (2026-08-18) that this table's plain JSON
    column cannot store as-is (psycopg2's default JSON adapter has no datetime encoder). Every
    value here is still real; this only normalizes datetimes to ISO strings (json's own
    `default=str`, the same fallback FastAPI's own JSON responses already apply to datetimes
    elsewhere in this app) so the round-trip through Postgres JSON is lossless for every OTHER
    field and merely re-typed (datetime -> str) for these few timestamp fields."""
    import json

    result = evaluate_gtm_governance(db, tenant_id)
    json_safe_result = json.loads(json.dumps(result, default=str))
    row = GovernanceSnapshot(tenant_id=tenant_id, snapshot=json_safe_result, computed_at=datetime.utcnow())
    db.add(row)
    db.commit()
    return json_safe_result


def get_latest_governance_snapshot(db: Session, tenant_id: int) -> tuple[dict, "datetime"] | None:
    """Read path for GET /gtm-os/briefing-governance -- a single indexed read, no sweep
    recomputation. Returns (snapshot_dict, computed_at) or None if no snapshot has ever been
    computed for this tenant yet (the route's own caller decides how to bootstrap that case,
    e.g. compute_and_store_governance_snapshot() once)."""
    row = (
        db.query(GovernanceSnapshot)
        .filter(GovernanceSnapshot.tenant_id == tenant_id)
        .order_by(GovernanceSnapshot.computed_at.desc())
        .first()
    )
    if row is None:
        return None
    return row.snapshot, row.computed_at
