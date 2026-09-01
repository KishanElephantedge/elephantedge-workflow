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
from app.phases.partner_icp import get_structured_icp
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
    )

    # Apply the partner's OWN stated bands as real filters (2026-09-01). Previously the only
    # pre-filter was "has an industry", and the LLM was handed a paragraph and asked to honour
    # numbers inside it. It cannot: confirmed against two real partners whose lists went out.
    # Remy Piazza's stored ICP literally says "$3M - $30M ARR with less than 100 employees" and
    # he was still sent generic IT/security companies that had NO revenue figure at all. Abbas
    # Shivji rejected 2 of 3 for geography and headcount that were both already in his ICP.
    #
    # A band only filters when it is KNOWN to be violated -- a company with no revenue on file is
    # kept, not dropped. Excluding unknowns would silently discard most of the table (only 595 of
    # 777 companies carry a revenue figure) and would hide good accounts behind missing data.
    icp = get_structured_icp(profile) or {}
    revenue_max = icp.get("revenue_max_usd")
    revenue_min = icp.get("revenue_min_usd")
    if isinstance(revenue_min, int):
        query = query.filter((Company.estimated_revenue_higher_usd.is_(None)) | (Company.estimated_revenue_higher_usd >= revenue_min))
    if isinstance(revenue_max, int):
        query = query.filter((Company.estimated_revenue_lower_usd.is_(None)) | (Company.estimated_revenue_lower_usd <= revenue_max))
    employee_min, employee_max = icp.get("employee_min"), icp.get("employee_max")
    if isinstance(employee_min, int):
        query = query.filter((Company.employee_count.is_(None)) | (Company.employee_count >= employee_min))
    if isinstance(employee_max, int):
        query = query.filter((Company.employee_count.is_(None)) | (Company.employee_count <= employee_max))

    industries = [i for i in (icp.get("industries") or []) if isinstance(i, str) and len(i.strip()) >= 3]
    if industries:
        # OR across the partner's own industry keywords, still keeping companies whose industry
        # text simply does not mention them out -- this is a funnel into a bounded LLM call, not
        # the final verdict, so it errs toward recall.
        # Word-boundary-ish match, not a bare substring: "%technology%" also matches
        # "Biotechnology Research", which put a biotech company into a SaaS partner's pool on the
        # first live run. Matching " technology" / "technology " / exact keeps the keyword a word
        # rather than a fragment of a longer one.
        def _kw_clauses(keyword: str):
            kw = keyword.strip().lower()
            return [
                Company.industry.ilike(f"{kw}%"),        # starts the field
                Company.industry.ilike(f"% {kw}%"),      # preceded by a space
                Company.industry.ilike(f"%,{kw}%"),      # preceded by a comma
            ]

        clauses = [c for kw in industries[:12] for c in _kw_clauses(kw)]
        combined = clauses[0]
        for clause in clauses[1:]:
            combined = combined | clause

        # Industry is a SOFT filter: keep it only if it actually leaves something behind.
        #
        # A partner states their ICP in business-model language ("B2B SaaS", "HealthTech",
        # "Life Sciences") while Company.industry holds LinkedIn's taxonomy ("Software
        # Development", "IT Services and IT Consulting"). Those vocabularies do not overlap, so a
        # hard AND on industry silently emptied the pool for 48 of 156 partners -- Abbas Shivji
        # went to zero candidates on an ICP that 751 companies satisfy on revenue and 663 on
        # headcount. Returning nothing is the worst outcome here: it reads as "no good accounts
        # exist" when it really means "we asked in the wrong vocabulary".
        #
        # The numeric bands are objective and stay hard. Industry falls back to the LLM, which
        # reads the company's full description and can bridge the two vocabularies.
        narrowed = query.filter(combined)
        if narrowed.limit(1).count():
            query = narrowed

    return (
        query.order_by(Company.hot_lead.desc().nulls_last(), Company.created_at.desc())
        .limit(CANDIDATE_LIMIT)
        .all()
    )


MATCHING_PROMPT = """You are helping Elephant Edge decide which of ITS OWN prospect companies \
would be a genuinely relevant lead to hand to a GTM partner (a fractional CMO/CRO or consultant \
in our ecosystem), based on what that partner is known to specialize in.

Partner's known industry: {industry}
Partner's known buyer/who they sell to: {sells_to}
Additional context about the partner: {reasoning}

Candidate companies (id, name, industry, employee_count, location, hiring_signal) -- pick ONLY \
from this list, never invent a company. hiring_signal, when present, names the sales role being \
hired and how many qualifying sales roles are open AT ONCE at that company right now -- this is \
a real urgency signal (a company hiring several sales roles simultaneously needs help NOW, not \
someday), so when it's a close call between two similarly-fitting companies, prefer the one with \
the stronger hiring signal, and mention the urgency explicitly in your reasoning when you use it \
(e.g. "actively hiring 4 sales roles at once -- high urgency to reach out"):
{candidates}

Pick up to {cap} companies from the list above that are the STRONGEST fit for this partner's \
stated specialty -- not just "vaguely B2B," a real fit based on industry/buyer alignment (and, \
as a tiebreaker/urgency signal, hiring intensity where relevant). If fewer than {cap} are a \
genuinely strong fit, return fewer. If NONE are a good fit, say so honestly rather than forcing \
weak matches.

Return JSON exactly:
{{"matches": [{{"company_id": <int>, "reasoning": "<one sentence citing what makes this a fit>", "confidence": "high" | "medium" | "low"}}], "insufficient_candidates": <true if no real matches found, else false>}}"""


def _hiring_signal_text(c: Company) -> str:
    """Real, already-computed data -- hiring_signal_posting_count is a free byproduct of
    Discovery (see app/phases/apify_discovery.py's domain_posting_counts), the same field
    hot_leads.py already uses for its "N sales roles open at once" hot-lead reason. Nothing new
    to capture here -- just surfacing an existing signal to this prompt too."""
    if not c.hiring_signal_role:
        return "none on file"
    role = c.hiring_signal_role.replace("_", " ")
    count = c.hiring_signal_posting_count
    if count and count > 1:
        return f"hiring for {role} -- {count} qualifying sales roles open at once"
    return f"hiring for {role}"


def _format_candidates(companies: list[Company]) -> str:
    return "\n".join(
        f"- id={c.id}, name={c.name!r}, industry={c.industry!r}, employee_count={c.employee_count}, "
        f"location={c.location!r}, hiring_signal={_hiring_signal_text(c)!r}"
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


CRO_BACKGROUND_TERMS = ["cro", "go-to-market strategist", "sales acceleration leader"]


def is_cro_focused(profile: LinkedinMonitorProfile) -> bool:
    """True when GTM University's own data tags this partner as sales/CRO-background -- the
    scope the lead asked for ("let's first focus on CROs (Sales background)"). Deliberately NOT
    broadened to any generic "sales" or "revenue" substring match -- checked the real data and
    that would have pulled in a bunch of CMO-primary partners who just mention "revenue" as part
    of a marketing-alignment title (e.g. "cmo, fractional cmo... revenue growth consultant"),
    exactly the group meant to be excluded. CRO_BACKGROUND_TERMS is an explicit, human-reviewed
    allowlist instead -- add a term here only after confirming (like go-to-market
    strategist/sales acceleration leader were) that it doesn't also sweep in marketing-primary
    people. Real distribution as of this filter's last update: 40 of 157 classified partners
    match; the rest either name a different function (CMO, RevOps-only, etc.) or have no GTM
    University data at all (older LinkedIn-only profiles) -- both cases return False, a
    deliberate exclusion of unknowns for this first pass, not a bug."""
    keywords = ((profile.gtm_university_data or {}).get("keywords") or "").lower()
    return any(term in keywords for term in CRO_BACKGROUND_TERMS)


def run_partner_matching_sweep(db: Session, tenant_id: int, only_new_profiles: bool = True, profile_id: int | None = None) -> dict:
    """For each classified, active, CRO-background partner (see is_cro_focused -- an explicit
    lead decision to scope to CROs first, not a technical limitation) without a pending
    'proposed' batch (only_new_profiles default True -- re-matching everyone on every tick would
    re-spend LLM calls for partners already awaiting review), proposes up to the configured cap
    of company matches. Safe to call repeatedly: existing (profile_id, company_id) pairs are
    skipped before insert, so a re-run never duplicates a recommendation.

    profile_id, when given, scopes this to exactly ONE partner AND skips the CRO filter -- a
    human explicitly opening a specific partner's card and clicking "run" is a deliberate choice
    that should always be honored, CRO-tagged or not; the CRO restriction only governs the
    unscoped bulk/scheduled sweep, not an explicit single-partner request. The schedule's own
    "enabled" pause flag is still honored either way (a paused schedule means no matching runs at
    all, single-partner or not) -- and only the fully-unscoped call (no profile_id) applies the
    only_new_profiles skip-list, since a deliberate single-partner request should always run for
    that partner regardless of whether they already have a pending batch."""
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
    if profile_id is None:
        # CRO-only scope for the bulk/scheduled path -- gtm_university_data is JSON, filtered in
        # Python rather than a JSON-path SQL clause; the partner pool is small (~150), so this
        # costs nothing meaningful and keeps is_cro_focused as the single source of truth.
        profiles = [p for p in profiles if is_cro_focused(p)]

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
