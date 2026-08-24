"""Problem-hypothesis detection -- Step 5, first version. Deterministic only, no LLM calls.

Implements the tightened entry rule from the Step 5 approval:

    A declared problem can open a Problem Hypothesis directly.
    An implied signal may open or contribute to a Problem Hypothesis only when the implication
    genuinely indicates a gap/need rather than merely growth or organizational change.
    Contextual/inferred signals can only corroborate an already-supported hypothesis.

Applied to what Step 4 actually produces today: linkedin_job and theirstack_job interpretations
only ever carry a job TITLE (no JD body text, no headcount/first-vs-Nth-hire context -- that data
deliberately isn't part of the GtmSignal/InterpretedSignal chain, see Step 3/4). Without that
context, a title match (even "Head of Sales" or "first SDR hire"-shaped titles) cannot be
reliably distinguished from an ordinary scaling/organizational hire -- exactly the four example
patterns the Step 5 approval named as things that must NOT be treated as automatic proof. So
every hiring_activity interpretation currently produced is classified as CONTEXTUAL tier: it can
strengthen an already-open hypothesis, but it can never open one by itself.

No source currently produces declared or implied_gap tier evidence (that requires an unstructured
source -- LinkedIn posts, Reddit -- explicitly not built yet). This means, honestly, no new
ProblemHypothesis will be opened from real data until such a source exists. The mechanism below
is still complete and correct for when one does; it does not special-case "no declared source
exists yet" -- it just naturally produces zero open hypotheses from hiring evidence alone, which
is the intended, conservative behavior."""

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.gtm_os.intelligence.company_resolution import ensure_company_resolved_if_needed
from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal
from app.gtm_os.intelligence.problem_hypothesis import ProblemHypothesis, ProblemHypothesisEvidence
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.orchestration.control import get_control_config

# event_type -> evidence tier. Anything not listed here defaults to "contextual" (the safe,
# under-claiming default -- see EVIDENCE_TIER_RANK for why that's conservative, not arbitrary).
#
# Step 6 additions (linkedin_post, see linkedin_post_interpretation.py):
#   - "problem_statement": an explicit, self-reported difficulty (phrase-matched, e.g.
#     "struggling to") -- this is genuinely declared evidence, same standing as the Step 5
#     design doc always intended for a direct problem statement, now that a source producing it
#     actually exists.
#   - "solution_question": a person publicly asking how others solve/handle something --
#     genuinely implies an unmet need without being as direct as a stated complaint, so
#     implied_gap (can open a hypothesis), not contextual.
#   - "growth_hiring_mention": a descriptive mention of growing a team -- doesn't establish a gap
#     on its own (matches the Step 5 tightened rule's own examples), so stays contextual, same as
#     job-posting hiring evidence.
#
# Step 7 addition/reclassification (per the approved gpt.txt adjustment): "solution_evaluation_mention"
# now ONLY covers ONGOING solution-seeking language ("evaluating tools for X") -- the RESOLVED/
# past-tense case ("just signed up for X") was split out into its own event_type,
# "solution_adoption_mention", so the two can be tiered differently. Actively evaluating a
# solution for something is itself strong enough to imply the underlying need exists (promoted
# to implied_gap, same standing as solution_question) -- this is what lets one interpreted signal
# both open/strengthen a ProblemHypothesis AND qualify as DemandHypothesis evidence without a
# separate earlier problem statement, per Step 7's own worked example. "solution_adoption_mention"
# keeps the ORIGINAL Step 6 reasoning (past-tense adoption can mean the need is already resolved,
# possibly by a competitor) and stays contextual.
#
# "market_pattern_observation" (2026-08-24 addition, linkedin_post_interpretation.py): a person
# clearly positioned/knowledgeable in the space describing a recurring problem pattern, but not
# phrased as their own first-person claim -- real signal, but weaker than a declared problem.
# Deliberately left OUT of this map so it falls through to DEFAULT_EVIDENCE_TIER ("contextual"),
# same conservative default hiring_activity already relies on -- listed here only so a future
# reader doesn't miss that this event_type exists and is intentionally contextual, not forgotten.
#
# "first_sales_hire_signal" (2026-08-24 addition, linkedin_job_interpretation.py): NOT a blanket
# promotion of hiring_activity -- this is a separate event_type that only ever gets created when
# the REAL job description text itself (not the title, not company size) indicates the company is
# hiring its first dedicated salesperson or building the sales function from scratch. That is
# exactly the context this module's own docstring named as the thing missing that would otherwise
# justify treating a hiring signal as more than contextual -- now that the JD text is actually
# read, this is real, JD-grounded evidence, not an unstructured title guess. implied_gap, same
# standing as solution_question/solution_evaluation_mention: strong enough to open a hypothesis,
# not as direct as a declared first-person problem statement.
#
# "concurrent_hiring_surge" (2026-08-24 addition, interpretation.py's company-level job-signal
# aggregation): 2+ distinct, real sales-role job postings open at the same company at once -- a
# counted fact, not an inference from a single title. Real, active scaling pressure, same
# implied_gap standing as first_sales_hire_signal -- an ordinary SINGLE hire stays "hiring_activity"
# (contextual) exactly as before; only genuinely concurrent multi-role hiring is promoted.
EVENT_TYPE_TIERS: dict[str, str] = {
    "hiring_activity": "contextual",
    "problem_statement": "declared",
    "solution_question": "implied_gap",
    "solution_evaluation_mention": "implied_gap",
    "solution_adoption_mention": "contextual",
    "growth_hiring_mention": "contextual",
    "first_sales_hire_signal": "implied_gap",
    "concurrent_hiring_surge": "implied_gap",
    "leadership_hire_signal": "implied_gap",
    # "market_pattern_observation" intentionally absent -- see comment above.
}
DEFAULT_EVIDENCE_TIER = "contextual"

# Tiers that are allowed to OPEN a new hypothesis. Contextual evidence can only ever be linked as
# supporting/contradicting evidence on a hypothesis that already exists.
OPENING_TIERS = {"declared", "implied_gap"}

EVIDENCE_TIER_RANK = {"contextual": 0, "implied_gap": 1, "declared": 2}


def classify_evidence_tier(interpreted_signal: InterpretedSignal) -> str:
    return EVENT_TYPE_TIERS.get(interpreted_signal.event_type, DEFAULT_EVIDENCE_TIER)


def _dedup_key_for(db: Session, interpreted_signal: InterpretedSignal) -> str | None:
    raw = db.get(GtmSignal, interpreted_signal.source_signal_id)
    return raw.dedup_key if raw else None


def _generic_problem_statement(interpreted_signal: InterpretedSignal) -> str:
    company = interpreted_signal.company_name_raw or "This company"
    function = interpreted_signal.affected_function or "an unspecified function"
    return f"{company} appears to show a possible internal gap or need related to {function}, based on declared/implied evidence."


def _recompute_confidence(db: Session, hypothesis: ProblemHypothesis) -> None:
    """Recomputes the structured confidence object from every evidence row currently linked to
    this hypothesis. Deliberately produces a dict of descriptive fields, never a single number
    (Step 5 design doc §10)."""
    links = (
        db.query(ProblemHypothesisEvidence)
        .filter(ProblemHypothesisEvidence.problem_hypothesis_id == hypothesis.id)
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

    best_tier = "contextual"
    for link in links:
        if EVIDENCE_TIER_RANK.get(link.evidence_tier, 0) > EVIDENCE_TIER_RANK.get(best_tier, 0):
            best_tier = link.evidence_tier

    hypothesis.confidence = {
        "best_evidence_tier": best_tier,
        "evidence_count": len(links),
        "independent_evidence_count": len(links),  # dedup already enforced at link time -- every linked row is independent by construction
        "distinct_evidence_types": distinct_event_types,
        "distinct_people_count": len(distinct_people),
        "has_contradiction": len(contradicting) > 0,
        "supporting_count": len(supporting),
        "contradicting_count": len(contradicting),
    }


def _get_open_hypothesis(
    db: Session, tenant_id: int, company_id: int | None, affected_function: str, person_name_raw: str | None = None
) -> ProblemHypothesis | None:
    """Real, resolved company_id is still the primary grouping key when available -- unchanged.

    UNRESOLVED-COMPANY FALLBACK: when company_id is None, this used to always return None,
    meaning every piece of unresolved-company evidence opened a brand-new ProblemHypothesis --
    confirmed against real production data (2026-08-18): the same real person (Jennie Marshall,
    a real LinkedIn poster with no resolvable company) produced two separate near-duplicate
    hypotheses from what should have been one accumulating claim. Falls back to matching on
    person_name_raw (passed through unchanged from InterpretedSignal.person_name_raw, never
    re-derived) -- the one real identity signal available when company resolution fails, already
    flowing through this pipeline, not invented for this fix. Still scoped to company_id IS NULL
    so a resolved-company hypothesis is never matched by name instead. If person_name_raw is also
    unavailable, still returns None (opens a new hypothesis) -- there is no real identity left to
    group on, and guessing would be worse than opening a fresh, honestly-unlinked one."""
    if company_id is not None:
        return (
            db.query(ProblemHypothesis)
            .filter(
                ProblemHypothesis.tenant_id == tenant_id,
                ProblemHypothesis.company_id == company_id,
                ProblemHypothesis.affected_function == affected_function,
            )
            .first()
        )
    if not person_name_raw:
        return None
    return (
        db.query(ProblemHypothesis)
        .filter(
            ProblemHypothesis.tenant_id == tenant_id,
            ProblemHypothesis.company_id.is_(None),
            ProblemHypothesis.affected_function == affected_function,
            ProblemHypothesis.person_name_raw == person_name_raw,
        )
        .first()
    )


def _already_linked(db: Session, hypothesis: ProblemHypothesis, dedup_key: str | None) -> bool:
    """Independence enforcement (Step 5 design doc §6): the same underlying GtmSignal (identified
    by dedup_key) is never linked to the same hypothesis twice, regardless of which structured
    source (linkedin_job/theirstack_job) produced it."""
    if dedup_key is None:
        return False
    linked_ids = [
        row[0]
        for row in db.query(ProblemHypothesisEvidence.interpreted_signal_id)
        .filter(ProblemHypothesisEvidence.problem_hypothesis_id == hypothesis.id)
        .all()
    ]
    if not linked_ids:
        return False
    linked_signals = db.query(InterpretedSignal).filter(InterpretedSignal.id.in_(linked_ids)).all()
    existing_dedup_keys = {_dedup_key_for(db, s) for s in linked_signals}
    return dedup_key in existing_dedup_keys


def evaluate_interpreted_signal(
    db: Session,
    tenant_id: int,
    interpreted_signal: InterpretedSignal,
    role: str = "supporting",
) -> ProblemHypothesis | None:
    """Applies the tightened entry rule to a single InterpretedSignal. Returns the
    ProblemHypothesis it was linked to, or None if it wasn't linked anywhere (contextual-tier
    evidence with no existing hypothesis to attach to -- the intended, conservative outcome for
    every current hiring_activity interpretation until a declared/implied_gap source exists)."""
    tier = classify_evidence_tier(interpreted_signal)
    # Real config toggle (2026-08-24) -- was a hardcoded False default before, meaning nothing
    # set anywhere could ever turn paid company-resolution enrichment on. Now reads
    # control.company_resolution.allow_paid_enrichment, same opt-in-by-config discipline as
    # every other paid-enrichment gate in this codebase.
    allow_paid_enrichment = get_control_config(db, tenant_id).get("company_resolution", {}).get("allow_paid_enrichment", False)
    ensure_company_resolved_if_needed(db, tenant_id, interpreted_signal, opening_tier=tier in OPENING_TIERS, allow_paid_enrichment=allow_paid_enrichment)
    dedup_key = _dedup_key_for(db, interpreted_signal)
    affected_function = interpreted_signal.affected_function or "unknown"

    hypothesis = _get_open_hypothesis(
        db, tenant_id, interpreted_signal.company_id, affected_function, interpreted_signal.person_name_raw
    )

    if hypothesis is None:
        if tier not in OPENING_TIERS:
            return None  # contextual evidence cannot open a hypothesis -- Step 5's tightened rule
        hypothesis = ProblemHypothesis(
            tenant_id=tenant_id,
            company_id=interpreted_signal.company_id,
            company_name_raw=interpreted_signal.company_name_raw,
            person_name_raw=interpreted_signal.person_name_raw if interpreted_signal.company_id is None else None,
            affected_function=affected_function,
            problem_statement=interpreted_signal.business_change or _generic_problem_statement(interpreted_signal),
            reasoning_note=f"Opened by {tier}-tier evidence: {interpreted_signal.business_change}",
            first_observed_at=interpreted_signal.observed_at,
            last_updated_at=datetime.utcnow(),
        )
        db.add(hypothesis)
        try:
            db.flush()  # need hypothesis.id before creating the evidence link
        except IntegrityError:
            # Race: a concurrent call opened the same hypothesis (same company_id, or same
            # person_name_raw when company_id is unresolved) between our read above and this
            # insert -- caught by the unique constraints added alongside this fix (see
            # ensure_indexes() in app/db/session.py). Roll back our attempted insert and re-read
            # the real, now-existing row instead of leaving two competing hypotheses.
            db.rollback()
            hypothesis = _get_open_hypothesis(
                db, tenant_id, interpreted_signal.company_id, affected_function, interpreted_signal.person_name_raw
            )
            if hypothesis is None:
                raise
    elif _already_linked(db, hypothesis, dedup_key):
        return hypothesis  # same underlying event already counted -- not linked again, not double-counted

    evidence = ProblemHypothesisEvidence(
        tenant_id=tenant_id,
        problem_hypothesis_id=hypothesis.id,
        interpreted_signal_id=interpreted_signal.id,
        role=role,
        evidence_tier=tier,
        note=f"{interpreted_signal.event_type} ({tier}): {interpreted_signal.business_change}",
    )
    db.add(evidence)
    try:
        db.flush()
    except IntegrityError:
        # Race: the same InterpretedSignal was concurrently linked to a hypothesis by another
        # call (unique constraint on interpreted_signal_id -- one signal evidences at most one
        # ProblemHypothesis, see problem_hypothesis.py). Not an error case: return whichever
        # hypothesis actually won, rather than leaving a duplicate evidence row.
        db.rollback()
        existing = (
            db.query(ProblemHypothesisEvidence)
            .filter(ProblemHypothesisEvidence.interpreted_signal_id == interpreted_signal.id)
            .first()
        )
        return db.query(ProblemHypothesis).filter(ProblemHypothesis.id == existing.problem_hypothesis_id).first() if existing else None

    if interpreted_signal.observed_at and (hypothesis.first_observed_at is None or interpreted_signal.observed_at < hypothesis.first_observed_at):
        hypothesis.first_observed_at = interpreted_signal.observed_at
    hypothesis.last_updated_at = datetime.utcnow()

    _recompute_confidence(db, hypothesis)
    db.commit()
    return hypothesis


def run_problem_hypothesis_sweep(
    db: Session,
    tenant_id: int,
    sources: list[str] | None = None,
    limit: int = 200,
) -> list[ProblemHypothesis]:
    """Evaluates InterpretedSignal rows for this tenant that haven't been linked into any
    ProblemHypothesis yet.

    Real bug fix (2026-08-24, confirmed live): the default `sources` was still
    ["linkedin_job", "theirstack_job"] only -- a stale restriction left over from Step 4, before
    linkedin_post interpretation (Step 6) existed. This silently meant every post-based signal,
    including declared-tier problem_statement (the STRONGEST possible evidence), was never
    evaluated by this sweep at all when called with its default sources -- confirmed live: Dale
    Zwizinski's real declared problem_statement had zero ProblemHypothesis link despite being
    interpreted, because the sweep never even looked at linkedin_post signals. Now matches
    demand_detection.run_demand_hypothesis_sweep's own (already-correct) default, plus
    linkedin_reply (Step 11B, also never added here)."""
    sources = sources or ["linkedin_job", "theirstack_job", "linkedin_post", "linkedin_reply"]
    # Real bug fix (2026-08-24, confirmed live -- same starvation bug found and fixed in
    # interpretation.run_interpretation_sweep the same day): .limit(limit) was applied BEFORE
    # excluding already-linked signals (filtered in Python afterward), so once the backlog
    # exceeded `limit`, genuinely NEW interpreted signals were never reached at all. Now
    # excluded at the SQL level, so `limit` genuinely bounds "how many NEW signals to evaluate."
    already_linked_subquery = (
        db.query(ProblemHypothesisEvidence.interpreted_signal_id)
        .filter(ProblemHypothesisEvidence.tenant_id == tenant_id)
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
        hypothesis = evaluate_interpreted_signal(db, tenant_id, signal)
        if hypothesis is not None and hypothesis not in touched:
            touched.append(hypothesis)
    return touched
