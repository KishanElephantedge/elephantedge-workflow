"""Deterministic, observation-level interpreters -- dispatch layer.

linkedin_job and theirstack_job (Step 4, both already-structured sources) are handled inline
below, reusing the existing role-title keyword classification from app/phases/hiring_signal.py
(ROLE_KEYWORDS / _classify_role) rather than building a second, unrelated classification system.

linkedin_post (Step 6) is handled by app/gtm_os/intelligence/linkedin_post_interpretation.py --
a separate module since free-text posts need narrow phrase-pattern matching, not title/keyword
matching (see that module's own docstring for why). linkedin_reply (Step 11B) is handled by
app/gtm_os/intelligence/linkedin_reply_interpretation.py, same phrase-matching philosophy, plus
one context-dependent rule specific to conversation replies (see that module's own docstring).
web_search and company_website remain explicitly out of scope.

No LLM calls anywhere in this dispatch layer or the interpreters it calls.

Every interpreter here answers only "what does this single raw signal appear to say" -- never
whether it constitutes demand, an opportunity, ICP fit, offering/playbook fit, urgency, or buying
intent. Those are later, not-yet-built layers."""

from sqlalchemy.orm import Session

from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal
from app.gtm_os.intelligence.linkedin_post_interpretation import interpret_linkedin_post_signal
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


def _interpret_job_signal(signal: GtmSignal) -> InterpretedSignal | None:
    """Shared deterministic logic for both job-posting sources -- linkedin_job and
    theirstack_job both store the job title the same way in extracted_info["title"] (see
    app/gtm_os/intelligence/sensing.py), so one rule covers both.

    Returns None (creates nothing) if the title doesn't match a known sales/marketing role --
    a low-confidence guess isn't a better outcome than no interpretation at all, matching the
    company_website precedent from the Step 4 design doc."""
    title = (signal.extracted_info or {}).get("title")
    if not title:
        return None
    role = _classify_role(title)
    if role is None:
        return None
    return InterpretedSignal(
        tenant_id=signal.tenant_id,
        source_signal_id=signal.id,
        event_type="hiring_activity",
        affected_function=ROLE_AFFECTED_FUNCTION.get(role, "unknown"),
        business_change=f"{signal.company_name_raw or 'This company'} appears to be hiring for {ROLE_LABEL.get(role, role)} role.",
        evidence_excerpt=title,
        extraction_method="deterministic:role_title_keyword_match",
        extraction_confidence="high",
        company_id=signal.company_id,
        company_name_raw=signal.company_name_raw,
        contact_id=signal.contact_id,
        person_name_raw=signal.person_name_raw,
        observed_at=signal.observed_at,
    )


def interpret_linkedin_job_signal(signal: GtmSignal) -> InterpretedSignal | None:
    if signal.source != "linkedin_job":
        raise ValueError(f"expected a linkedin_job signal, got {signal.source!r}")
    return _interpret_job_signal(signal)


def interpret_theirstack_job_signal(signal: GtmSignal) -> InterpretedSignal | None:
    if signal.source != "theirstack_job":
        raise ValueError(f"expected a theirstack_job signal, got {signal.source!r}")
    return _interpret_job_signal(signal)


_INTERPRETERS = {
    "linkedin_job": interpret_linkedin_job_signal,
    "theirstack_job": interpret_theirstack_job_signal,
    "linkedin_post": interpret_linkedin_post_signal,
    "linkedin_reply": interpret_linkedin_reply_signal,
}


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
    already_interpreted = {
        row[0]
        for row in db.query(InterpretedSignal.source_signal_id).filter(InterpretedSignal.tenant_id == tenant_id).all()
    }
    query = (
        db.query(GtmSignal)
        .filter(GtmSignal.tenant_id == tenant_id, GtmSignal.source.in_(sources))
        .order_by(GtmSignal.id)
        .limit(limit)
    )
    created = []
    for signal in query:
        if signal.id in already_interpreted:
            continue
        interpreter = _INTERPRETERS.get(signal.source)
        if interpreter is None:
            continue
        result = interpreter(signal)
        if result is not None:
            db.add(result)
            created.append(result)
    db.commit()
    return created
