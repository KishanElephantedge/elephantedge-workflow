"""GTM Partner -> Company matching -- extends the Targets watch-list from a pure signal
monitor into a lead-recommendation engine. For each classified GTM partner (see
app/phases/gtm_partner_classification.py, and as of 2026-08-19 also synced directly from GTM
University's own public partner directory -- see LinkedinMonitorProfile.gtm_university_data),
proposes a capped number of Elephant Edge companies from our own pipeline that fit that
partner's stated industry/buyer, so a human can review, approve, and eventually hand the
partner a personal "here are N companies that might be relevant for you" message.

Runs on its own daily schedule (same Parameter-backed, UI-editable pattern as
linkedin_monitor.py's poll interval -- see get_match_schedule/set_match_schedule below), not
just on manual trigger, per the explicit direction that this should work the way GTM
University's own AI matching does: an ongoing automated process, not a one-off script.

NO EMBEDDINGS -- consistent with every other matcher in this codebase (see
app/gtm_os/icp/icp_offering_matching.py and friends). Matching is a bounded, grounded LLM call
per partner (not per pair), same honest "insufficient_candidates" escape hatch as
gtm_partner_classification.py's "insufficient_evidence" -- never forces N matches when the
evidence doesn't support them. Candidate-narrowing (deterministic, free) runs before the LLM
call so a daily automated sweep stays cost-sane: this is what keeps automation safe, not in
tension with it."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Batch, Company, LinkedinMonitorProfile, Parameter, PartnerCompanyRecommendation
from app.llm_client import generate_json
from app.phases.linkedin_monitor import ScheduleConfigError

MATCH_CAP_PARAMETER_KEY = "gtm_partner_match_cap"
DEFAULT_MATCH_CAP = 3


def get_match_cap(db: Session, tenant_id: int) -> int:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == MATCH_CAP_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, dict) and isinstance(param.value.get("cap"), int):
        return param.value["cap"]
    return DEFAULT_MATCH_CAP


def set_match_cap(db: Session, tenant_id: int, cap: int) -> int:
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise ScheduleConfigError("cap must be a positive integer")
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == MATCH_CAP_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = {"cap": cap}
    else:
        param = Parameter(tenant_id=tenant_id, key=MATCH_CAP_PARAMETER_KEY, value={"cap": cap}, description="Max companies auto-matched per GTM partner per sweep, editable from Targets > Settings")
        db.add(param)
    db.commit()
    return cap


MATCH_SCHEDULE_PARAMETER_KEY = "gtm_partner_match_schedule"

# Daily by default -- matches how the user described this working ("it daily identifies few
# companies... this should be dynamic"), same shape as linkedin_monitor.py's DEFAULT_SCHEDULE.
DEFAULT_MATCH_SCHEDULE = {"days": 1, "hours": 0, "minutes": 0, "enabled": True}
MIN_MATCH_SCHEDULE_MINUTES = 60  # a partner-matching LLM pass has no reason to run more than hourly


def get_match_schedule(db: Session, tenant_id: int) -> dict:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == MATCH_SCHEDULE_PARAMETER_KEY)
        .first()
    )
    schedule = dict(param.value) if param and isinstance(param.value, dict) and param.value else dict(DEFAULT_MATCH_SCHEDULE)
    for key, default in DEFAULT_MATCH_SCHEDULE.items():
        schedule.setdefault(key, default)
    schedule["interval_minutes"] = schedule["days"] * 1440 + schedule["hours"] * 60 + schedule["minutes"]
    return schedule


def set_match_schedule(db: Session, tenant_id: int, days: int, hours: int, minutes: int, enabled: bool) -> dict:
    for name, value in (("days", days), ("hours", hours), ("minutes", minutes)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ScheduleConfigError(f"{name!r} must be a non-negative integer")
    if not isinstance(enabled, bool):
        raise ScheduleConfigError("'enabled' must be true/false")
    total_minutes = days * 1440 + hours * 60 + minutes
    if total_minutes < MIN_MATCH_SCHEDULE_MINUTES:
        raise ScheduleConfigError(f"interval must be at least {MIN_MATCH_SCHEDULE_MINUTES} minutes total (got {total_minutes})")

    value = {"days": days, "hours": hours, "minutes": minutes, "enabled": enabled}
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == MATCH_SCHEDULE_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = value
    else:
        param = Parameter(tenant_id=tenant_id, key=MATCH_SCHEDULE_PARAMETER_KEY, value=value, description="GTM partner company-matching poll interval + pause/resume, editable from Targets > Settings")
        db.add(param)
    db.commit()
    return get_match_schedule(db, tenant_id)


# How many candidate companies to hand the LLM per partner -- bounds prompt size/cost regardless
# of how large the companies table grows; deterministic pre-filter below decides WHICH ones make
# the cut, not the LLM.
CANDIDATE_LIMIT = 150


def _get_candidate_companies(db: Session, tenant_id: int, profile: LinkedinMonitorProfile) -> list[Company]:
    """Deterministic, free pre-filter -- only companies with a real industry on file (nothing to
    match against otherwise), most recently discovered / hot-lead first so a bounded LLM call
    sees the most relevant slice rather than an arbitrary one. No embeddings, no fuzzy scoring
    here -- just a cheap funnel so the LLM step (per-partner, not per-pair) stays affordable at
    any company-table size."""
    query = (
        db.query(Company)
        .join(Batch)
        .filter(Batch.tenant_id == tenant_id)
        .filter(Company.industry.isnot(None))
        .order_by(Company.hot_lead.desc().nulls_last(), Company.created_at.desc())
        .limit(CANDIDATE_LIMIT)
    )
    return query.all()


MATCHING_PROMPT = """You are helping Elephant Edge decide which of ITS OWN prospect companies \
would be a genuinely relevant lead to hand to a GTM partner (a fractional CMO/CRO or consultant \
in our ecosystem), based on what that partner is known to specialize in.

Partner's known industry: {industry}
Partner's known buyer/who they sell to: {sells_to}
Additional context about the partner: {reasoning}

Candidate companies (id, name, industry, employee_count, location) -- pick ONLY from this list, \
never invent a company:
{candidates}

Pick up to {cap} companies from the list above that are the STRONGEST fit for this partner's \
stated specialty -- not just "vaguely B2B," a real fit based on industry/buyer alignment. If \
fewer than {cap} are a genuinely strong fit, return fewer. If NONE are a good fit, say so \
honestly rather than forcing weak matches.

Return JSON exactly:
{{"matches": [{{"company_id": <int>, "reasoning": "<one sentence citing what makes this a fit>", "confidence": "high" | "medium" | "low"}}], "insufficient_candidates": <true if no real matches found, else false>}}"""


def _format_candidates(companies: list[Company]) -> str:
    return "\n".join(
        f"- id={c.id}, name={c.name!r}, industry={c.industry!r}, employee_count={c.employee_count}, location={c.location!r}"
        for c in companies
    )


def _llm_match_companies(db: Session, tenant_id: int, profile: LinkedinMonitorProfile, candidates: list[Company], cap: int) -> dict:
    """One bounded generate_json() call per partner, never per (partner, company) pair -- see
    module docstring. Falls back to insufficient_candidates=True (never a fabricated match) if
    the LLM call itself fails, same "classifier unavailable" precedent as
    gtm_partner_classification.classify_profile()."""
    if not candidates:
        return {"matches": [], "insufficient_candidates": True, "reason": "no candidate companies with a known industry"}
    try:
        result = generate_json(
            MATCHING_PROMPT.format(
                industry=profile.industry or "(unknown)",
                sells_to=profile.sells_to or "(unknown)",
                reasoning=profile.classification_reasoning or "(none)",
                candidates=_format_candidates(candidates),
                cap=cap,
            ),
            db, tenant_id, max_tokens=800,
        )
    except Exception as e:  # noqa: BLE001 -- one partner's LLM failure must never abort the sweep
        return {"matches": [], "insufficient_candidates": True, "reason": f"matcher unavailable: {e}"}

    matches = result.get("matches") or []
    valid_ids = {c.id for c in candidates}
    # Never trust an id the LLM invented -- only real candidate ids we actually offered it.
    matches = [m for m in matches if isinstance(m, dict) and m.get("company_id") in valid_ids][:cap]
    return {"matches": matches, "insufficient_candidates": bool(result.get("insufficient_candidates")) and not matches}


def run_partner_matching_sweep(db: Session, tenant_id: int, only_new_profiles: bool = True, profile_id: int | None = None) -> dict:
    """For each classified, active partner without a pending 'proposed' batch (only_new_profiles
    default True -- re-matching everyone on every tick would re-spend LLM calls for partners
    already awaiting review), proposes up to the configured cap of company matches. Safe to call
    repeatedly: existing (profile_id, company_id) pairs are skipped before insert, so a re-run
    never duplicates a recommendation.

    profile_id, when given, scopes this to exactly ONE partner -- this is the real per-partner
    "run" action the Recommended Companies detail view uses (a person clicking into one partner's
    card and asking for matches must never fan out and spend LLM calls on everyone else too). The
    schedule's own "enabled" pause flag is still honored either way (a paused schedule means no
    matching runs at all, single-partner or not) -- and only the fully-unscoped call (no
    profile_id) applies the only_new_profiles skip-list, since a deliberate single-partner
    request should always run for that partner regardless of whether they already have a pending
    batch."""
    schedule = get_match_schedule(db, tenant_id)
    if not schedule["enabled"]:
        return {"partners_evaluated": 0, "recommendations_created": 0, "status": "paused"}

    cap = get_match_cap(db, tenant_id)

    query = (
        db.query(LinkedinMonitorProfile)
        .filter(LinkedinMonitorProfile.tenant_id == tenant_id)
        .filter(LinkedinMonitorProfile.active.is_(True))
        .filter(LinkedinMonitorProfile.classification_status == "classified")
    )
    if profile_id is not None:
        query = query.filter(LinkedinMonitorProfile.id == profile_id)
    elif only_new_profiles:
        already_proposed_profile_ids = {
            r[0] for r in (
                db.query(PartnerCompanyRecommendation.profile_id)
                .filter(PartnerCompanyRecommendation.tenant_id == tenant_id)
                .filter(PartnerCompanyRecommendation.status == "proposed")
                .distinct()
            )
        }
        if already_proposed_profile_ids:
            query = query.filter(LinkedinMonitorProfile.id.notin_(already_proposed_profile_ids))
    profiles = query.order_by(LinkedinMonitorProfile.id).all()

    partners_evaluated = 0
    recommendations_created = 0
    for profile in profiles:
        partners_evaluated += 1
        candidates = _get_candidate_companies(db, tenant_id, profile)
        result = _llm_match_companies(db, tenant_id, profile, candidates, cap)

        existing_pairs = {
            r[0] for r in (
                db.query(PartnerCompanyRecommendation.company_id)
                .filter(PartnerCompanyRecommendation.profile_id == profile.id)
            )
        }
        for match in result["matches"]:
            company_id = match["company_id"]
            if company_id in existing_pairs:
                continue
            db.add(PartnerCompanyRecommendation(
                tenant_id=tenant_id, profile_id=profile.id, company_id=company_id,
                match_reasoning=match.get("reasoning"), match_confidence=match.get("confidence"),
            ))
            existing_pairs.add(company_id)
            recommendations_created += 1
        db.commit()

    return {"partners_evaluated": partners_evaluated, "recommendations_created": recommendations_created}


def get_recommendations_for_profile(db: Session, tenant_id: int, profile_id: int) -> list[PartnerCompanyRecommendation]:
    return (
        db.query(PartnerCompanyRecommendation)
        .filter(PartnerCompanyRecommendation.tenant_id == tenant_id)
        .filter(PartnerCompanyRecommendation.profile_id == profile_id)
        .order_by(PartnerCompanyRecommendation.created_at.desc())
        .all()
    )


def get_all_recommendations(db: Session, tenant_id: int, status: str | None = None) -> list[PartnerCompanyRecommendation]:
    query = db.query(PartnerCompanyRecommendation).filter(PartnerCompanyRecommendation.tenant_id == tenant_id)
    if status:
        query = query.filter(PartnerCompanyRecommendation.status == status)
    return query.order_by(PartnerCompanyRecommendation.created_at.desc()).all()


def update_recommendation_status(db: Session, tenant_id: int, recommendation_id: int, status: str) -> PartnerCompanyRecommendation | None:
    if status not in ("proposed", "approved", "rejected"):
        raise ScheduleConfigError(f"invalid status {status!r}")
    rec = (
        db.query(PartnerCompanyRecommendation)
        .filter(PartnerCompanyRecommendation.id == recommendation_id)
        .filter(PartnerCompanyRecommendation.tenant_id == tenant_id)
        .first()
    )
    if not rec:
        return None
    rec.status = status
    rec.reviewed_at = datetime.utcnow()
    db.commit()
    return rec
