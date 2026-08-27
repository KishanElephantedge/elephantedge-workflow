"""Deterministic, observation-level interpreters -- dispatch layer.

linkedin_job and theirstack_job (Step 4, both already-structured sources) are handled inline
below, reusing the existing role-title keyword classification from app/phases/hiring_signal.py
(ROLE_KEYWORDS / _classify_role) rather than building a second, unrelated classification system.

linkedin_post (Step 6) is handled by app/gtm_os/intelligence/linkedin_post_interpretation.py --
a separate module since free-text posts need phrase/semantic matching, not title/keyword matching
(see that module's own docstring for why, and for its two-tier phrase-then-LLM-fallback design,
2026-08-24). linkedin_reply (Step 11B) is handled by
app/gtm_os/intelligence/linkedin_reply_interpretation.py, same phrase-matching philosophy, plus
one context-dependent rule specific to conversation replies (see that module's own docstring).
web_search and company_website remain explicitly out of scope.

linkedin_job/theirstack_job/linkedin_reply interpretation below is purely deterministic, no LLM
calls. linkedin_post interpretation (dispatched separately below, not through _INTERPRETERS) can
make an LLM call as its second-tier fallback -- see linkedin_post_interpretation.py.

Every interpreter here answers only "what does this raw signal (or, for linkedin_post, this
person's grouped signals) appear to say" -- never whether it constitutes demand, an opportunity,
ICP fit, offering/playbook fit, urgency, or buying intent. Those are later, not-yet-built
layers."""

from sqlalchemy.orm import Session

from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal
from app.gtm_os.intelligence.linkedin_job_interpretation import FIRST_HIRE_EVENT_TYPE, classify_job_description
from app.gtm_os.intelligence.linkedin_post_interpretation import (
    interpret_linkedin_post_signal,
    interpret_linkedin_post_signals_grouped,
)
from app.gtm_os.intelligence.linkedin_reply_interpretation import interpret_linkedin_reply_signal
from app.gtm_os.intelligence.signal import GtmSignal
from app.phases.hiring_signal import _classify_role

ROLE_AFFECTED_FUNCTION = {
    "head_of_sales": "sales",
    "sdr": "sales",
    "ae": "sales",
    "gtm": "sales",
    "marketing": "marketing",
}

ROLE_LABEL = {
    "head_of_sales": "a Head of Sales / sales leadership",
    "sdr": "an SDR/BDR (outbound)",
    "ae": "an Account Executive",
    "gtm": "a GTM",
    "marketing": "a marketing",
}


def _interpret_job_signal(signal: GtmSignal, db=None, tenant_id: int | None = None) -> InterpretedSignal | None:
    """Shared deterministic logic for both job-posting sources -- linkedin_job and
    theirstack_job both store the job title the same way in extracted_info["title"] (see
    app/gtm_os/intelligence/sensing.py), so one rule covers both.

    Returns None (creates nothing) if the title doesn't match a known sales/marketing role --
    a low-confidence guess isn't a better outcome than no interpretation at all, matching the
    company_website precedent from the Step 4 design doc.

    2026-08-24 addition: for linkedin_job specifically (never theirstack_job, which has no JD
    body captured), when db/tenant_id are supplied and a real job description is on file
    (extracted_info["description_text"]), an additional semantic pass (see
    linkedin_job_interpretation.classify_job_description) may promote the base "hiring_activity"
    event to "first_sales_hire_signal" -- but only when the REAL JD text itself supports it. The
    base title-matched result is unchanged either way; this only ever upgrades what event_type/
    business_change/evidence gets attached to it.

    2026-08-24 second addition: a role classified as "head_of_sales" is itself a distinct, real
    ICP-3 trigger (icp_config.py: icp_3 "Needs Fractional Leadership" triggers on exactly this
    role type) -- a leadership/VP-level hire is not the same kind of ambiguous signal an ordinary
    IC rep hire (sdr/ae) is, so it is promoted deterministically to "leadership_hire_signal"
    (no LLM, reuses the role classification already computed above) UNLESS the JD-content pass
    already promoted it further to first_sales_hire_signal. Ordinary sdr/ae/gtm/marketing roles
    are NOT touched by this -- they stay plain hiring_activity, contextual tier, exactly as
    before; this is deliberately narrow to the one role type that is itself a distinct, real
    trigger, not a blanket promotion of every hire."""
    title = (signal.extracted_info or {}).get("title")
    if not title:
        return None
    role = _classify_role(title)
    if role is None:
        return None

    event_type = "hiring_activity"
    business_change = f"{signal.company_name_raw or 'This company'} appears to be hiring for {ROLE_LABEL.get(role, role)} role."
    evidence_excerpt = title
    extraction_method = "deterministic:role_title_keyword_match"

    if role == "head_of_sales":
        event_type = "leadership_hire_signal"
        business_change = (
            f"{signal.company_name_raw or 'This company'} is hiring for {ROLE_LABEL.get(role, role)} -- "
            f"a leadership-level sales hire, not an ordinary rep hire."
        )
        extraction_method = "deterministic:head_of_sales_role_match"

    if signal.source == "linkedin_job" and db is not None and tenant_id is not None:
        description_text = (signal.extracted_info or {}).get("description_text")
        promotion = classify_job_description(title, description_text, db, tenant_id) if description_text else None
        if promotion is not None:
            event_type = promotion["event_type"]
            business_change = (
                f"{signal.company_name_raw or 'This company'}'s job description for {ROLE_LABEL.get(role, role)} "
                f"indicates this is a first sales hire / building the sales function from scratch."
            )
            evidence_excerpt = promotion["quote"]
            extraction_method = "llm_semantic:first_sales_hire_signal"

    return InterpretedSignal(
        tenant_id=signal.tenant_id,
        source_signal_id=signal.id,
        event_type=event_type,
        affected_function=ROLE_AFFECTED_FUNCTION.get(role, "unknown"),
        business_change=business_change,
        evidence_excerpt=evidence_excerpt,
        extraction_method=extraction_method,
        extraction_confidence="high" if event_type == "hiring_activity" else "medium",
        company_id=signal.company_id,
        company_name_raw=signal.company_name_raw,
        contact_id=signal.contact_id,
        person_name_raw=signal.person_name_raw,
        observed_at=signal.observed_at,
    )


def interpret_linkedin_job_signal(signal: GtmSignal, db=None, tenant_id: int | None = None) -> InterpretedSignal | None:
    if signal.source != "linkedin_job":
        raise ValueError(f"expected a linkedin_job signal, got {signal.source!r}")
    return _interpret_job_signal(signal, db=db, tenant_id=tenant_id)


def interpret_theirstack_job_signal(signal: GtmSignal) -> InterpretedSignal | None:
    if signal.source != "theirstack_job":
        raise ValueError(f"expected a theirstack_job signal, got {signal.source!r}")
    return _interpret_job_signal(signal)


def interpret_website_visitor_signal(signal: GtmSignal) -> InterpretedSignal | None:
    """Channels Intelligence step 4 -- purely deterministic, no LLM call, no role/keyword
    classification to run (a page visit carries no title/text to classify). event_type
    "website_visit" is deliberately absent from problem_detection.py's EVENT_TYPE_TIERS so it
    falls to the DEFAULT_EVIDENCE_TIER ("contextual") -- see sense_website_visitors()'s own
    docstring for why that's the correct, intended tier, not an oversight."""
    if signal.source != "website_visitor":
        raise ValueError(f"expected a website_visitor signal, got {signal.source!r}")

    info = signal.extracted_info or {}
    company = signal.company_name_raw or "An unidentified company"
    page = info.get("page_path")
    business_change = f"{company} visited the website" + (f" (page: {page})" if page else "") + "."

    return InterpretedSignal(
        tenant_id=signal.tenant_id,
        source_signal_id=signal.id,
        event_type="website_visit",
        affected_function="unknown",
        business_change=business_change,
        evidence_excerpt=page or "(page not captured)",
        extraction_method="deterministic:ip_identified_website_visit",
        extraction_confidence="low",
        company_id=signal.company_id,
        company_name_raw=signal.company_name_raw,
        contact_id=signal.contact_id,
        person_name_raw=signal.person_name_raw,
        observed_at=signal.observed_at,
    )


_INTERPRETERS = {
    "linkedin_job": interpret_linkedin_job_signal,
    "theirstack_job": interpret_theirstack_job_signal,
    "linkedin_post": interpret_linkedin_post_signal,
    "linkedin_reply": interpret_linkedin_reply_signal,
    "website_visitor": interpret_website_visitor_signal,
}


def _linkedin_post_group_key(signal: GtmSignal) -> tuple | None:
    """Real per-person identity key for grouping linkedin_post signals (2026-08-24 addition, see
    interpret_linkedin_post_signals_grouped). Prefers the LinkedIn profile identifier already
    captured in extracted_info (author_profile_url/author_urn/author_profile_id -- the same
    stable fields company_resolution.py already treats as the trustworthy per-person key) over
    person_name_raw, which is a raw, unnormalized string (e.g. "Sai S.", "Ramas Krishnan (RK)")
    that risks silently merging different people who happen to render the same name, or failing
    to merge the same person whose name renders differently across posts. Two signals only ever
    group together when their non-null identifiers are identical -- a signal with no identifiable
    person at all (key is None) is never grouped with anything."""
    info = signal.extracted_info or {}
    profile_id = info.get("author_profile_url") or info.get("author_urn") or info.get("author_profile_id")
    if profile_id:
        return ("profile", profile_id)
    if signal.person_name_raw:
        return ("name", signal.person_name_raw, signal.company_id or signal.company_name_raw)
    return None


def _promote_concurrent_hiring_signals(job_results: list[tuple[GtmSignal, InterpretedSignal]]) -> None:
    """2026-08-24 addition, explicit instruction: a company with 2+ DISTINCT, real sales-role job
    postings open at once is a stronger, genuinely counted signal than any one ordinary hire --
    real scaling pressure, not an assumption. Groups this sweep's freshly-created linkedin_job
    InterpretedSignal rows by company_name_raw (the real identity key for this source -- job
    signals carry no profile URL); for any company with 2+ postings, every one of them that is
    still plain "hiring_activity" (i.e. not already promoted to first_sales_hire_signal by the JD
    classifier) is promoted to "concurrent_hiring_surge" -- implied_gap/demand-qualifying tier
    (problem_detection.py/demand_detection.py), same standing as first_sales_hire_signal. A single
    ordinary hire at a company with no other concurrent posting is left exactly as-is. Only counts
    postings within THIS sweep's batch -- a company whose other concurrent postings were already
    interpreted in an earlier sweep is not retroactively re-evaluated here (same documented scope
    limitation as interpret_linkedin_post_signals_grouped)."""
    by_company: dict[str, list[tuple[GtmSignal, InterpretedSignal]]] = {}
    for signal, result in job_results:
        if signal.company_name_raw:
            by_company.setdefault(signal.company_name_raw, []).append((signal, result))

    for company, items in by_company.items():
        if len(items) < 2:
            continue
        titles = [(s.extracted_info or {}).get("title") or "unknown role" for s, _ in items]
        for signal, result in items:
            if result.event_type != "hiring_activity":
                continue  # already promoted (first_sales_hire_signal) or otherwise -- leave as-is
            result.event_type = "concurrent_hiring_surge"
            result.business_change = (
                f"{company} has {len(items)} distinct sales-role job postings open at once "
                f"({', '.join(titles)}) -- a real concurrent hiring surge, not a single ordinary hire."
            )
            result.extraction_method = "deterministic:concurrent_hiring_count"
            result.extraction_confidence = "medium"


def run_interpretation_sweep(
    db: Session,
    tenant_id: int,
    sources: list[str] | None = None,
    limit: int = 200,
) -> list[InterpretedSignal]:
    """Interprets GtmSignal rows for this tenant that don't have an InterpretedSignal yet, for
    the given sources only (default: both sources this version supports). No new schema/marker
    column is added -- "already interpreted" is a NOT EXISTS check against interpreted_signals,
    kept deliberately simple for this first version (an open decision in the Step 4 design doc,
    not resolved with a schema change here since a small sweep can just query for it)."""
    sources = sources or list(_INTERPRETERS.keys())
    # Real bug fix (2026-08-24, confirmed live): the previous query applied .limit(limit) BEFORE
    # excluding already-interpreted signals, filtering them out only in Python afterward. Once
    # the total signal backlog across these sources exceeded `limit` (confirmed live: 233 real
    # signals vs limit=200), the oldest-N fetched were almost entirely already-interpreted, and
    # genuinely NEW signals were never reached at all -- a real, silent starvation bug, not a
    # capacity limit working as intended. Now excludes already-interpreted rows at the SQL level
    # (a NOT IN subquery) before the limit is ever applied, so `limit` genuinely bounds "how many
    # NEW signals to interpret this call," matching what every caller already assumes it does.
    already_interpreted_subquery = db.query(InterpretedSignal.source_signal_id).filter(InterpretedSignal.tenant_id == tenant_id).subquery()
    query = (
        db.query(GtmSignal)
        .filter(GtmSignal.tenant_id == tenant_id, GtmSignal.source.in_(sources))
        .filter(~GtmSignal.id.in_(db.query(already_interpreted_subquery.c.source_signal_id)))
        .order_by(GtmSignal.id)
        .limit(limit)
    )
    created = []
    linkedin_post_signals = []
    linkedin_job_results: list[tuple[GtmSignal, InterpretedSignal]] = []
    for signal in query:
        if signal.source == "linkedin_post":
            # Deferred to the grouped path below (2026-08-24 addition) instead of dispatched
            # individually here -- see interpret_linkedin_post_signals_grouped's own docstring.
            linkedin_post_signals.append(signal)
            continue
        interpreter = _INTERPRETERS.get(signal.source)
        if interpreter is None:
            continue
        result = interpreter(signal, db=db, tenant_id=tenant_id) if signal.source == "linkedin_job" else interpreter(signal)
        if result is not None:
            db.add(result)
            created.append(result)
            if signal.source == "linkedin_job":
                linkedin_job_results.append((signal, result))

    if linkedin_job_results:
        _promote_concurrent_hiring_signals(linkedin_job_results)

    if linkedin_post_signals:
        groups: dict[tuple, list[GtmSignal]] = {}
        for signal in linkedin_post_signals:
            key = _linkedin_post_group_key(signal)
            if key is None:
                groups.setdefault(("__ungrouped__", signal.id), []).append(signal)
            else:
                groups.setdefault(key, []).append(signal)
        for group_signals in groups.values():
            for result in interpret_linkedin_post_signals_grouped(group_signals, db=db, tenant_id=tenant_id):
                db.add(result)
                created.append(result)

    db.commit()
    return created
