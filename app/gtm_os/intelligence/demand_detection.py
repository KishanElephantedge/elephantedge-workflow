"""Demand-hypothesis detection -- Step 7, first version. Deterministic only, no LLM calls.

Implements the Step 7 design (demand-hypothesis-design.md) plus the approved gpt.txt adjustment:
a DemandHypothesis always requires an underlying ProblemHypothesis (problem_hypothesis_id is
never nullable), but that Problem Hypothesis does NOT need to already exist beforehand -- when a
single InterpretedSignal is both problem-implying and outward-looking (e.g. "we're evaluating
tools for outbound attribution"), this module reuses the EXISTING Problem-layer gate
(problem_detection.evaluate_interpreted_signal) to open/strengthen the Problem Hypothesis from
that same signal, then immediately opens/strengthens the Demand Hypothesis linked to it. No
separate earlier problem statement is required.

The Demand-layer tier map (DEMAND_EVENT_TYPE_TIERS below) is DELIBERATELY SEPARATE from the
Problem-layer tier map (problem_detection.EVENT_TYPE_TIERS) -- they answer different questions
about the same InterpretedSignal ("does this indicate an internal gap?" vs "is this outward-
looking?"), and the same event_type can and does carry two independent tier values, one per
layer. "solution_evaluation_mention" is a clean example: implied_gap for Problem (it implies a
real need), and its own, stronger "buying_intent_adjacent" tier for Demand (evaluating a solution
is itself outward-looking behavior).

Evidence that does NOT qualify for Demand (hiring_activity, problem_statement alone,
growth_hiring_mention, solution_adoption_mention, or anything else not explicitly listed) is
NEVER linked into the Demand ledger -- not even to corroborate an already-open DemandHypothesis.
This is a stricter rule than the Problem layer's own (where contextual evidence CAN corroborate
an already-open hypothesis): Demand's ledger only ever contains genuinely outward-looking
evidence, full stop, per the Step 7 design doc's own independence model (§7)."""

from datetime import datetime

import logging
import re

from sqlalchemy.orm import Session

from app.gtm_os.intelligence.demand_hypothesis import DemandHypothesis, DemandHypothesisEvidence
from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal
from app.gtm_os.intelligence.problem_detection import evaluate_interpreted_signal as evaluate_problem_signal
from app.gtm_os.intelligence.signal import GtmSignal

logger = logging.getLogger(__name__)

# event_type -> demand tier. Anything not listed here does NOT qualify as Demand evidence at all
# (DEFAULT_DEMAND_TIER = "none") -- distinct from the Problem layer's own tier map; see module
# docstring for why the same event_type can carry two different values across the two layers.
#
# Step 11B additions (linkedin_reply, see linkedin_reply_interpretation.py): "pricing_request"
# and "demo_request" are explicit, transactional asks about Elephant Edge itself -- no post-
# interpreter equivalent existed for these. Tiered "buying_intent_adjacent" (same tier as
# solution_evaluation_mention, per the Step 11B spec's own instruction to represent these as a
# stronger Demand tier rather than a new BuyingIntent object). "not_interested"/"timing_deferral"
# are deliberately NOT registered here -- see linkedin_reply_interpretation.py's module docstring.
#
# 2026-08-24 additions (linkedin_job_interpretation.py / interpretation.py's concurrent-hiring
# aggregation): "first_sales_hire_signal" (JD-content-verified: this is genuinely the company's
# first dedicated sales hire / building the function from scratch) and "concurrent_hiring_surge"
# (2+ distinct, real sales-role postings open at the same company at once -- a real, counted fact,
# not an assumption) are both real, JD/count-grounded evidence of an actual gap, not just an
# ordinary scaling hire (which stays "hiring_activity", still unlisted/non-qualifying here).
# Tiered "demand" (the minimum qualifying bar, same as solution_question) -- explicitly NOT
# "buying_intent_adjacent": a hiring decision, however strong, is not itself outward-looking
# solution-seeking behavior the way evaluating a vendor or asking a pricing question is.
DEMAND_EVENT_TYPE_TIERS: dict[str, str] = {
    "solution_question": "demand",  # general, non-vendor-specific ask -- Demand's own minimum bar
    "solution_evaluation_mention": "buying_intent_adjacent",  # actively evaluating a solution -- stronger, closer to (but not) buying intent
    "pricing_request": "buying_intent_adjacent",
    "demo_request": "buying_intent_adjacent",
    "first_sales_hire_signal": "demand",
    "concurrent_hiring_surge": "demand",
    "leadership_hire_signal": "demand",
}
DEFAULT_DEMAND_TIER = "none"

# Tiers that qualify evidence for the Demand ledger at all -- "none" is excluded from both
# opening AND corroborating (stricter than the Problem layer's contextual-can-corroborate rule).
DEMAND_QUALIFYING_TIERS = {"demand", "buying_intent_adjacent"}
DEMAND_TIER_RANK = {"none": 0, "demand": 1, "buying_intent_adjacent": 2}


def classify_demand_tier(interpreted_signal: InterpretedSignal) -> str:
    return DEMAND_EVENT_TYPE_TIERS.get(interpreted_signal.event_type, DEFAULT_DEMAND_TIER)


def _dedup_key_for(db: Session, interpreted_signal: InterpretedSignal) -> str | None:
    raw = db.get(GtmSignal, interpreted_signal.source_signal_id)
    return raw.dedup_key if raw else None


def _get_open_demand(db: Session, tenant_id: int, problem_hypothesis_id: int) -> DemandHypothesis | None:
    return (
        db.query(DemandHypothesis)
        .filter(DemandHypothesis.tenant_id == tenant_id, DemandHypothesis.problem_hypothesis_id == problem_hypothesis_id)
        .first()
    )


def _already_linked(db: Session, hypothesis: DemandHypothesis, dedup_key: str | None) -> bool:
    """Independence enforcement, same mechanism as ProblemHypothesisEvidence: the same underlying
    GtmSignal (identified by dedup_key) is never linked to the same DemandHypothesis twice,
    regardless of which structured source produced it -- e.g. a LinkedIn job posting and its
    TheirStack copy of the same real posting are the same event, not two independent signals."""
    if dedup_key is None:
        return False
    linked_ids = [
        row[0]
        for row in db.query(DemandHypothesisEvidence.interpreted_signal_id)
        .filter(DemandHypothesisEvidence.demand_hypothesis_id == hypothesis.id)
        .all()
    ]
    if not linked_ids:
        return False
    linked_signals = db.query(InterpretedSignal).filter(InterpretedSignal.id.in_(linked_ids)).all()
    existing_dedup_keys = {_dedup_key_for(db, s) for s in linked_signals}
    return dedup_key in existing_dedup_keys


def _recompute_confidence(db: Session, hypothesis: DemandHypothesis) -> None:
    links = (
        db.query(DemandHypothesisEvidence)
        .filter(DemandHypothesisEvidence.demand_hypothesis_id == hypothesis.id)
        .all()
    )
    supporting = [link for link in links if link.role == "supporting"]
    contradicting = [link for link in links if link.role == "contradicting"]

    interpreted_ids = [link.interpreted_signal_id for link in links]
    interpreted_rows = (
        db.query(InterpretedSignal).filter(InterpretedSignal.id.in_(interpreted_ids)).all() if interpreted_ids else []
    )
    distinct_event_types = sorted({row.event_type for row in interpreted_rows})
    distinct_people = {row.person_name_raw for row in interpreted_rows if row.person_name_raw}

    best_tier = "none"
    for link in links:
        if DEMAND_TIER_RANK.get(link.evidence_tier, 0) > DEMAND_TIER_RANK.get(best_tier, 0):
            best_tier = link.evidence_tier

    hypothesis.confidence = {
        "best_evidence_tier": best_tier,
        "evidence_count": len(links),
        "independent_evidence_count": len(links),  # dedup already enforced at link time
        "distinct_evidence_types": distinct_event_types,
        "distinct_people_count": len(distinct_people),
        "has_contradiction": len(contradicting) > 0,
        "supporting_count": len(supporting),
        "contradicting_count": len(contradicting),
    }


def _generic_demand_statement(interpreted_signal: InterpretedSignal) -> str:
    company = interpreted_signal.company_name_raw or "This company"
    function = interpreted_signal.affected_function or "an unspecified function"
    return f"{company} appears to be actively looking for ways to address a {function}-related need."



# Seniority that can credibly speak FOR a company. A job posting is a company action -- the
# company paid to publish it -- so it needs no such check. A POST is one person speaking, and a
# junior employee's claim about company-wide intent is not the company's intent.
# Values are matched case-insensitively as substrings, since providers vary ("Owner", "C-Level",
# "CXO", "VP of Sales", "Head of Growth").
DECISION_MAKER_SENIORITY_MARKERS = (
    "owner", "founder", "c_level", "c-level", "clevel", "cxo", "chief", "partner",
    "vp", "vice president", "head", "director", "president",
)


def _author_can_speak_for_company(db: Session, interpreted_signal: InterpretedSignal) -> tuple[bool, str]:
    """Post-only gate (2026-08-31). Returns (allowed, reason).

    Jobs are unaffected: a posted job req is a company ACTION, already company-level evidence.
    This only governs whether a linkedin_post may open COMPANY-level demand, which is what
    triggers outreach at that company.

    Rationale from real data: sampling declared-tier posts found ads, opinions, and a consultant
    quoting a CLIENT's numbers -- none of them statements about the author's own company. Even
    for a genuine first-person claim, it only carries company weight if the author is senior
    enough to make it. Gal Aga (CEO Co-Founder, seniority "Owner") does; an SDR posting the same
    words does not.

    Unknown seniority is NOT treated as senior. Author role is captured free on the same
    search_contact call that resolves the company, so "unknown" means identity was never
    established that way -- and an unverified speaker cannot be assumed to speak for the company."""
    raw = db.get(GtmSignal, interpreted_signal.source_signal_id)
    if raw is None or raw.source != "linkedin_post":
        return True, "not a post -- company-level by construction"

    role = (raw.extracted_info or {}).get("author_role") or {}
    haystack = " ".join(str(v).lower() for v in (role.get("seniority"), role.get("title")) if v)
    if haystack and any(marker in haystack for marker in DECISION_MAKER_SENIORITY_MARKERS):
        return True, f"post author is a decision maker ({role.get('title') or role.get('seniority')})"

    # Second path: the author NAMES their own company in the post. Attribution is then explicit
    # rather than inferred from who they are -- "we at Acme are hiring 6 GTM roles" states whose
    # hiring it is, whatever the speaker's title. Only reachable alongside the first-person
    # ownership check the hiring detector already applies, so this cannot admit someone naming a
    # company they merely commented on.
    if _post_names_own_company(db, raw):
        return True, "post explicitly names the author's own company -- attribution is stated, not inferred"

    if not haystack:
        return False, "post author's role is unknown and the post does not name their company -- cannot confirm they speak for it"
    return False, f"post author is not a decision maker ({role.get('title') or role.get('seniority')}) and the post does not name their company"


def _post_names_own_company(db: Session, raw: GtmSignal) -> bool:
    """True when the post text mentions the company the signal actually resolved to.

    Matched against the RESOLVED company (or the enrichment-verified name), never against an
    arbitrary company mentioned in passing -- so a post discussing some other business cannot
    satisfy this. Short names are skipped: a two- or three-character token matches far too much
    ordinary text to be evidence of anything."""
    from app.db.models import Company

    name = None
    if raw.company_id:
        company = db.get(Company, raw.company_id)
        name = company.name if company else None
    name = name or raw.company_name_raw
    if not name or len(name.strip()) < 4:
        return False

    text = ((raw.extracted_info or {}).get("text") or "") + " " + ((raw.raw_evidence or {}).get("text") or "")

    # Case-SENSITIVE, word-boundary match. Many real company names are ordinary English words --
    # "Aligned", "Locate", "Watchful" all appear in this tenant's own data -- so a lowercase
    # substring match would read "our teams are aligned" as naming the company Aligned. A brand
    # is written as a proper noun when it is being named, so requiring the original casing
    # separates "Aligned's brand" (naming it) from "we are aligned" (an adjective).
    return re.search(rf"\b{re.escape(name.strip())}\b", text) is not None


def evaluate_interpreted_signal_for_demand(
    db: Session,
    tenant_id: int,
    interpreted_signal: InterpretedSignal,
    role: str = "supporting",
) -> DemandHypothesis | None:
    """Applies the Demand gate to a single InterpretedSignal. Returns the DemandHypothesis it was
    linked to, or None if it doesn't qualify (tier == "none") or no underlying problem could be
    established for it (see below). Non-qualifying evidence is NEVER linked into the Demand
    ledger, whether opening or corroborating -- stricter than the Problem layer's own rule."""
    demand_tier = classify_demand_tier(interpreted_signal)
    if demand_tier not in DEMAND_QUALIFYING_TIERS:
        return None  # e.g. hiring_activity, problem_statement alone, growth_hiring_mention, solution_adoption_mention

    # Post-only: a person must be senior enough to speak for their employer before their post
    # becomes company-level demand and triggers outreach there. Jobs pass untouched.
    allowed, reason = _author_can_speak_for_company(db, interpreted_signal)
    if not allowed:
        logger.info("demand_detection: interpreted_signal=%s not opened -- %s", interpreted_signal.id, reason)
        return None

    # Ensure the underlying problem exists -- reuses the EXISTING Problem-layer gate with the
    # SAME signal, so a signal that both implies a problem and shows outward-looking evidence
    # (e.g. "we're evaluating tools for outbound attribution") opens/strengthens BOTH in one pass,
    # with no separate earlier problem statement required. If the Problem layer can't establish a
    # problem from this signal (e.g. its Problem-tier is contextual and no problem is already
    # open), Demand cannot exist either -- Demand always requires an underlying problem.
    problem_hypothesis = evaluate_problem_signal(db, tenant_id, interpreted_signal, role=role)
    if problem_hypothesis is None:
        return None

    dedup_key = _dedup_key_for(db, interpreted_signal)
    demand_hypothesis = _get_open_demand(db, tenant_id, problem_hypothesis.id)

    if demand_hypothesis is None:
        demand_hypothesis = DemandHypothesis(
            tenant_id=tenant_id,
            company_id=interpreted_signal.company_id,
            company_name_raw=interpreted_signal.company_name_raw,
            problem_hypothesis_id=problem_hypothesis.id,
            affected_function=problem_hypothesis.affected_function,
            demand_statement=interpreted_signal.business_change or _generic_demand_statement(interpreted_signal),
            reasoning_note=f"Opened by {demand_tier}-tier evidence: {interpreted_signal.business_change}",
            first_observed_at=interpreted_signal.observed_at,
            last_updated_at=datetime.utcnow(),
        )
        db.add(demand_hypothesis)
        db.flush()
    elif _already_linked(db, demand_hypothesis, dedup_key):
        return demand_hypothesis  # same underlying event already counted -- not linked again

    evidence = DemandHypothesisEvidence(
        tenant_id=tenant_id,
        demand_hypothesis_id=demand_hypothesis.id,
        interpreted_signal_id=interpreted_signal.id,
        role=role,
        evidence_tier=demand_tier,
        note=f"{interpreted_signal.event_type} ({demand_tier}): {interpreted_signal.business_change}",
    )
    db.add(evidence)
    db.flush()

    if interpreted_signal.observed_at and (
        demand_hypothesis.first_observed_at is None or interpreted_signal.observed_at < demand_hypothesis.first_observed_at
    ):
        demand_hypothesis.first_observed_at = interpreted_signal.observed_at
    demand_hypothesis.last_updated_at = datetime.utcnow()

    _recompute_confidence(db, demand_hypothesis)
    db.commit()
    return demand_hypothesis


def run_demand_hypothesis_sweep(
    db: Session,
    tenant_id: int,
    sources: list[str] | None = None,
    limit: int = 200,
) -> list[DemandHypothesis]:
    """Evaluates InterpretedSignal rows for this tenant that haven't been linked into any
    DemandHypothesis yet. Scoped to the sources Step 3/6 already sense: linkedin_post,
    linkedin_job, theirstack_job -- no new sensing/interpretation work needed, per Step 7's scope."""
    sources = sources or ["linkedin_post", "linkedin_job", "theirstack_job"]
    # Real bug fix (2026-08-24, confirmed live -- same starvation bug found and fixed in
    # interpretation.py and problem_detection.py the same day): .limit(limit) was applied BEFORE
    # excluding already-linked signals (filtered in Python afterward), so once the backlog
    # exceeded `limit`, genuinely NEW interpreted signals were never reached at all.
    already_linked_subquery = (
        db.query(DemandHypothesisEvidence.interpreted_signal_id)
        .filter(DemandHypothesisEvidence.tenant_id == tenant_id)
        .subquery()
    )
    query = (
        db.query(InterpretedSignal)
        .join(GtmSignal, InterpretedSignal.source_signal_id == GtmSignal.id)
        .filter(InterpretedSignal.tenant_id == tenant_id, GtmSignal.source.in_(sources))
        .filter(~InterpretedSignal.id.in_(db.query(already_linked_subquery.c.interpreted_signal_id)))
        .order_by(InterpretedSignal.id)
        .limit(limit)
    )
    touched = []
    for signal in query:
        hypothesis = evaluate_interpreted_signal_for_demand(db, tenant_id, signal)
        if hypothesis is not None and hypothesis not in touched:
            touched.append(hypothesis)
    return touched
