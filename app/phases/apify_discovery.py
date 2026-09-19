"""
Apify Discovery -- validated live 2026-08-05/06 as a real second discovery source alongside
jd_first (TheirStack). Same jobs-first flow inversion as jd_first_discovery.py (search job
postings directly, firmographics folded into the same query), but sourced from Apify's
LinkedIn Jobs Scraper actor (fantastic-jobs/advanced-linkedin-job-search-api) instead of
TheirStack -- a real-time, hourly-updated LinkedIn-native index, found live to have a
meaningfully denser and fresher pool than TheirStack at the same hour of day (8/8 and 5/5 real
qualifying companies across two real test days, vs. TheirStack's 2/5 on the intervening real
production day).

Employee band tightened to 25-50 (not the shared 11-50 EMPLOYEE_COUNT_MIN/jd_first band) --
validated live against real LinkedIn-follower-count/founding-year/funding-language proxies
that this band skews meaningfully toward better-funded, more mature companies than 11-24,
since neither Apify nor the free Crustdata identify call expose a real revenue or funding
number to filter on directly.

Industry filter is a required, hard-won detail: Apify's own industryFilter param silently
returns zero results (not an error) if any entry contains a comma -- confirmed live that
"Technology, Information and Internet" alone broke an otherwise-identical query. Only
comma-free LinkedIn industry names are used here for that reason.

No separate Crustdata company_identify call here (unlike jd_first) -- Apify's own
org_linkedin_website/org_linkedin_headcount are already real, LinkedIn-sourced values per
company, so that extra lookup isn't needed.
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

# Signal persistence reuses V2's own model and helpers rather than duplicating them, so the row
# discovery writes is byte-identical in shape to the one sense_linkedin_jobs() writes -- see
# _persist_posting_as_signal() below. app/phases/ already imports app/gtm_os/ in several places
# (partner_pipeline, autonomous_orchestrator, calendar_sync), so this crosses no new boundary.
from app.gtm_os.intelligence.sensing import _dedup_key, _parse_dt
from app.gtm_os.intelligence.signal import GtmSignal
from app.phases.company_profile_check import fetch_public_company_profile, profile_rejection_reason
from app.apify_budget_guard import STATUS_ALLOWED as APIFY_BUDGET_ALLOWED, check_apify_budget
from app.apify_client import COST_PER_JOB_USD, ApifyError, estimate_cost_usd, search_linkedin_jobs
from app.apify_client import _get_api_key as _get_apify_api_key
from app.db.models import Company
from app.phases.discovery import _existing_domains
from app.phases.hiring_signal import (
    TOFU_KEYWORD, _classify_role, _classify_signal, _detect_product_fit_signals, _infer_hire_type,
    assess_team_composition,
)

# 2026-09-07 real fix: locationSearch/countries_derived filter the JOB POSTING's location, not
# the company's own headquarters -- confirmed live that a company can post a US-market-targeted
# role ("... - USA Market") while the company itself is headquartered elsewhere, and that role
# still matches a "United States" job search. org_linkedin_headquarters (a real, LinkedIn-sourced
# field already present in the actor's own response, just never read before this fix) is the
# actual company HQ. US state names/abbreviations, same as a plain resident of this codebase
# would check by eye -- not exhaustive of every US territory, but real US company HQs
# overwhelmingly resolve to "City, State" or "City, State, US".
_US_STATES = {
    "alabama", "al", "alaska", "ak", "arizona", "az", "arkansas", "ar", "california", "ca",
    "colorado", "co", "connecticut", "ct", "delaware", "de", "florida", "fl", "georgia", "ga",
    "hawaii", "hi", "idaho", "id", "illinois", "il", "indiana", "in", "iowa", "ia", "kansas", "ks",
    "kentucky", "ky", "louisiana", "la", "maine", "me", "maryland", "md", "massachusetts", "ma",
    "michigan", "mi", "minnesota", "mn", "mississippi", "ms", "missouri", "mo", "montana", "mt",
    "nebraska", "ne", "nevada", "nv", "new hampshire", "nh", "new jersey", "nj", "new mexico", "nm",
    "new york", "ny", "north carolina", "nc", "north dakota", "nd", "ohio", "oh", "oklahoma", "ok",
    "oregon", "or", "pennsylvania", "pa", "rhode island", "ri", "south carolina", "sc",
    "south dakota", "sd", "tennessee", "tn", "texas", "tx", "utah", "ut", "vermont", "vt",
    "virginia", "va", "washington", "wa", "west virginia", "wv", "wisconsin", "wi", "wyoming",
    "wy", "district of columbia", "dc",
}


def _is_us_headquarters(org_linkedin_headquarters: str | None) -> bool | None:
    """True/False when the field lets us tell; None when there's genuinely nothing to check
    against (missing data is not the same as a non-US company, so this is never coerced to
    False on its own -- caller decides what to do with an unknown).

    Structural, not a blocklist -- several US state postal codes ARE real ISO country codes
    (IN=Indiana/India, CA=California/Canada), so the same 2-letter token is genuinely ambiguous
    out of context. What disambiguates it is SEGMENT COUNT: LinkedIn's own shape is either
    "City, State" (2 segments, no separate country slot -- confirmed live: "Austin, TX") or
    "City, Region, Country" (3 segments, the LAST one is a country marker -- confirmed live:
    "Bengaluru, Karnataka, IN" and "Toronto, ON, CA", neither a US company). So a trailing
    2-letter code is only trusted as a bare state abbreviation when there is NO separate country
    segment already claiming that slot -- with 3+ segments, the last one must read as an actual
    US country marker ("US"/"USA"/"United States"), never a coincidentally-matching state code."""
    if not org_linkedin_headquarters:
        return None
    segments = [s.strip().lower() for s in org_linkedin_headquarters.split(",") if s.strip()]
    if not segments:
        return None
    if segments[-1] in ("us", "usa", "united states"):
        return True
    if len(segments) >= 3:
        # The last segment is playing the country role here -- it already claimed that slot, so
        # it not being "US"/"USA"/"United States" (checked above) means this is NOT a US company,
        # regardless of whether the code also happens to spell a US state's postal abbreviation.
        return False
    # Exactly 2 segments (or 1): no separate country slot, so the last segment is free to be
    # read as a bare US state -- either the full name or its 2-letter postal code.
    last = segments[-1]
    if last in _US_STATES:
        return True
    return None

APIFY_TITLE_SEARCH = [
    "SDR", "BDR", "AE", "Sales Development Representative",
    "Business Development Representative", "Business Development Manager", "Account Executive",
    "Head of Sales", "VP Sales", "VP of Sales", "Director of Sales", "Sales Director",
    "Sales Manager", "CRO", "Chief Revenue Officer", "Head of GTM", "GTM Engineer",
]

# Validated live 2026-08-06 -- see module docstring. Comma-free LinkedIn industry names only.
APIFY_INDUSTRY_FILTER = [
    "Software Development", "Computer Software", "Computer and Network Security",
    "IT Services and IT Consulting", "Data Infrastructure and Analytics",
]

APIFY_EMPLOYEE_MIN = 25
APIFY_EMPLOYEE_MAX = 50
APIFY_DISCOVERY_LIMIT_CAP = 150

# Postings bought per company wanted, when a tenant has not set its own. 20 is what the old
# `target * 20` rule used, kept as the default so no existing tenant's yield changes the day this
# became configurable -- lowering it is a per-tenant decision backed by that tenant's own measured
# keep rate, not a global guess applied to everyone at once.
DEFAULT_DISCOVERY_OVERSAMPLE = 20

# A floor on the fetch itself, not on the fetch-per-company. One posting rarely yields one company
# (several postings per employer, plus already-seen domains), so a 1-2 company ask still needs a
# real sample to have anything to choose from. Deliberately small: the old floor was 100, which is
# what made a 5-company ask cost the same as a 20-company one.
MIN_DISCOVERY_LIMIT = 25


def get_discovery_oversample(db: Session, tenant_id: int) -> int:
    """Postings this tenant buys per company it wants. Per-tenant because the right number is a
    property of the ICP being searched: a narrow one returns more duplicate/irrelevant postings per
    usable company than a broad one, so a single global constant either overpays for the broad
    tenants or under-fetches for the narrow ones."""
    from app.db.models import Parameter

    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id, Parameter.key == "discovery_oversample")
        .first()
    )
    if param and param.value and isinstance(param.value.get("postings_per_company"), int):
        value = param.value["postings_per_company"]
        if value > 0:
            return value
    return DEFAULT_DISCOVERY_OVERSAMPLE

# Location was a hardcoded ["United States"] literal at the three call sites that run a job
# search. Nothing about the actor requires it -- locationSearch takes any list -- so this was an
# unstated assumption, not a capability limit, and it made the whole company table US-only: 0 of
# 777 companies are confirmed non-US. That surfaced when Abbas Shivji's ICP turned out to read
# "United Kingdom / European Union / EMEA", so no filtering could ever produce an account he
# would accept; the companies were never discovered in the first place.
APIFY_DEFAULT_LOCATION_SEARCH = ["United States"]


def _normalize_domain(website: str) -> str:
    domain = (website or "").lower().strip()
    domain = domain.replace("https://", "").replace("http://", "")
    domain = domain.split("/")[0]
    return domain.replace("www.", "")


def _persist_posting_as_signal(db: Session, tenant_id: int, company: Company, job: dict) -> GtmSignal | None:
    """Keep the posting we just paid for, linked to the company it just created.

    WHY THIS EXISTS (measured on production 2026-09-19, before this was added):
    1,066 of 1,175 companies had NO gtm_signal at all, and 352 of 506 linkedin_job signals had
    no company_id. Discovery bought a posting, created a Company from it, wrote the V1-shaped
    Company.hiring_signal_* columns, and discarded the posting itself. But V2's whole chain
    (interpretation -> problem -> demand -> opportunity) reads GtmSignal, never those columns --
    so every company discovered this way was structurally incapable of producing an opportunity.
    That is why runs 147/149/150 completed "cleanly" with signals_created: 0 and 0 opportunities.

    The company_id is the expensive half. Discovery creates the Company FROM this exact posting,
    so the link is known here, for free. Leaving it NULL is what pushed the signal into
    company_resolution.py's PAID Deepline path to re-derive a link we already had -- and when
    that spend is budget-blocked, resolution records "unresolved" and the evidence is stranded
    permanently. 352 production signals are in exactly that state.

    Shape is deliberately IDENTICAL to sense_linkedin_jobs() in
    app/gtm_os/intelligence/sensing.py (same source/signal_type/source_ref/extracted_info keys,
    same _dedup_key), so interpretation.py, problem_detection.py and demand_detection.py consume
    it with no changes at all. A near-miss variant would be silently ignored downstream.

    Returns None (never raises) when the posting carries no usable identifier, or when this
    posting was already sensed -- the same "already_sensed" guard sense_linkedin_jobs() uses,
    since a repeat observation of one posting must not become a second signal (a real
    2026-08-24 bug: the same Codeable BDR posting stored twice)."""
    source_ref = str(job.get("id") or job.get("jobUrl") or job.get("url") or "")
    if not source_ref:
        return None

    already_sensed = (
        db.query(GtmSignal)
        .filter(GtmSignal.tenant_id == tenant_id, GtmSignal.source == "linkedin_job", GtmSignal.source_ref == source_ref)
        .first()
    )
    if already_sensed:
        # Backfill the link if this posting was sensed earlier WITHOUT a company (the 352-signal
        # case above) and we now know the company for certain. Never overwrite an existing link.
        if already_sensed.company_id is None:
            already_sensed.company_id = company.id
            already_sensed.company_resolution_status = "resolved"
            already_sensed.company_resolution_method = "explicit"
            already_sensed.company_resolved_at = datetime.utcnow()
            db.commit()
        return None

    signal = GtmSignal(
        tenant_id=tenant_id,
        source="linkedin_job",
        source_ref=source_ref,
        signal_type="job_posting",
        observed_at=_parse_dt(job.get("date_posted") or job.get("postedAt") or job.get("datePosted")),
        company_id=company.id,
        company_name_raw=job.get("organization") or job.get("organizationName") or job.get("companyName"),
        raw_evidence=job,
        extracted_info={
            "title": job.get("title"),
            "location": job.get("location"),
            "organization_domain": job.get("org_linkedin_website"),
            "organization_headcount": job.get("org_linkedin_headcount"),
            "description_text": job.get("description_text"),
            "seniority": job.get("seniority"),
            "ai_experience_level": job.get("ai_experience_level"),
            "ai_core_responsibilities": job.get("ai_core_responsibilities"),
            "ai_requirements_summary": job.get("ai_requirements_summary"),
            "organization_industry": job.get("org_linkedin_industry"),
            "organization_founded_date": job.get("org_linkedin_founded_date"),
            "organization_description": job.get("org_linkedin_description"),
        },
        dedup_key=_dedup_key("linkedin_job", source_ref),
        # Resolved by construction, not by a paid lookup -- "explicit" is the same method
        # company_resolution.py records when the source itself already names the company.
        company_resolution_status="resolved",
        company_resolution_method="explicit",
        company_resolution_reason="linked at discovery: this posting created this company row",
        company_resolved_at=datetime.utcnow(),
    )
    db.add(signal)
    db.commit()
    return signal


def run_apify_discovery(
    batch_id: int, db: Session, tenant_id: int, target: int = 5, time_range: str = "7d",
    location_search: list[str] | None = None, title_search: list[str] | None = None,
    employee_min: int | None = None, employee_max: int | None = None,
    industry_filter: list[str] | None = None, limit: int | None = None,
    budget_tenant_id: int | None = None,
) -> dict:
    """Entrypoint -- single synchronous Apify actor call (no budget_guard/paging loop like
    jd_first, since the actor's own `limit` already bounds spend deterministically: N results
    costs exactly N*$0.005 + $0.01, no per-page uncertainty).

    time_range default changed from "24h" to "7d" (2026-08-10) -- confirmed live that "24h"
    produced only 1 real posting on an otherwise-normal day, well short of the 5-10/day
    target. "7d" is the actor's own recommended default. Safe for a daily-running cron: we
    already dedup against every domain ever seen (seen_domains, above), not just today's
    batch, so a wider window can only mean re-fetching (and paying to re-check, then
    discarding) postings already seen on a prior day -- never a duplicate Company row."""
    seen_domains = _existing_domains(tenant_id, db)

    # Participate in the SHARED Apify budget (2026-08-31). The actor's own `limit` already bounds
    # this call's worst case (~$0.76 at the 150 cap), but that ceiling was invisible to
    # check_apify_budget, so discovery could spend against the account while every other source
    # believed the daily/monthly cap was intact -- and the BudgetGuard passed in by V2's
    # run_v2_discovery_if_due tracks the DEEPLINE balance, not Apify, so it never covered this
    # either. Checked before the call, using the same worst-case estimate the limit implies.
    # How many postings to buy per company wanted. This is the whole unit economics of discovery:
    # the actor bills per posting RETURNED, and the keep loop below stops the moment it has
    # `target` companies, so every posting past that point is paid for and thrown away.
    #
    # Was `max(target * 20, 100)` -- a flat 100 floor on top of the multiplier, which made a small
    # ask cost the same as a large one. Measured live 2026-09-11: Elephant Edge's 25-company run
    # billed 185 postings for 24 companies ($0.04/company), while each 5-company partner run billed
    # exactly 100 for 5 ($0.10/company) -- 2.5x worse per company purely because the floor, not the
    # target, decided the fetch. The floor is gone so the fetch scales with what was actually asked
    # for, and the multiplier is per-tenant config (get_discovery_oversample) because the right
    # value is a property of the tenant's ICP -- a narrow ICP genuinely needs more postings per
    # keep than a broad one, and that is measurable per tenant rather than guessable globally.
    oversample = get_discovery_oversample(db, tenant_id)
    discovery_limit = min(limit or max(target * oversample, MIN_DISCOVERY_LIMIT), APIFY_DISCOVERY_LIMIT_CAP)
    # The budget is checked against whoever OWNS the Apify account, which is not always the tenant
    # the companies are being written for. Partner discovery writes into a partner's own tenant (a
    # data boundary, deliberately isolated from Elephant Edge's pipeline) while spending Elephant
    # Edge's Apify credits -- checking the partner tenant found no budget configured and blocked a
    # run that was properly funded.
    budget = check_apify_budget(db, budget_tenant_id or tenant_id, estimate_cost_usd(discovery_limit))
    if budget["status"] != APIFY_BUDGET_ALLOWED:
        # A budget block is a real, intentional skip -- reported like the ApifyError path below
        # (a normal empty result the caller already handles), never a crash.
        return {
            "companies_discovered": 0, "postings_checked": 0, "rejection_breakdown": {},
            "budget_stopped_early": True, "api_error": f"apify_budget_blocked: {budget['reason']}",
            "estimated_cost_usd": 0.0,
        }

    effective_emp_min = employee_min if employee_min is not None else APIFY_EMPLOYEE_MIN
    effective_emp_max = employee_max if employee_max is not None else APIFY_EMPLOYEE_MAX

    try:
        api_key = _get_apify_api_key(db, budget_tenant_id or tenant_id)
        jobs = search_linkedin_jobs(
            api_key,
            title_search=title_search or APIFY_TITLE_SEARCH,
            location_search=location_search or APIFY_DEFAULT_LOCATION_SEARCH,
            organization_employees_gte=effective_emp_min,
            organization_employees_lte=effective_emp_max,
            industry_filter=industry_filter or APIFY_INDUSTRY_FILTER,
            time_range=time_range,
            remove_agency=True,
            # Apify bills per actual result returned, not per limit requested -- so this cap
            # is a REAL worst-case cost ceiling (150 * $0.005 + $0.01 ~= $0.76 for discovery
            # alone), not just an oversample target. Uncapped target*20 would let a real
            # higher-inventory day cost proportionally more as target grows (e.g. target=15 ->
            # limit=300 -> up to ~$1.51) -- fixed 2026-08-11 before raising target past 5.
            # It's fine (explicitly confirmed, not a bug) if this cap means fewer than
            # `target` companies get kept on a low-inventory day -- the run still completes
            # normally with however many it found, never crashes or stops the pipeline.
            limit=discovery_limit,
        )
    except ApifyError as e:
        return {
            "companies_discovered": 0, "postings_checked": 0, "rejection_breakdown": {},
            "budget_stopped_early": False, "api_error": str(e), "estimated_cost_usd": 0.0,
        }

    # Hiring velocity (added 2026-08-13, Signal Framework v2's advisor-recommended top signal):
    # how many qualifying postings a company has open AT ONCE in this same pull, not just the
    # one posting that gets kept per company below. Free -- `jobs` already holds every posting
    # from this single Apify call in memory; this just counts before the per-domain dedup loop
    # discards the rest.
    domain_posting_counts: dict[str, int] = {}
    for job in jobs:
        domain = _normalize_domain(job.get("org_linkedin_website") or "")
        if not domain:
            continue
        if _classify_role(job.get("title") or "") or _detect_product_fit_signals(job.get("description_text") or ""):
            domain_posting_counts[domain] = domain_posting_counts.get(domain, 0) + 1

    # WHAT WE ACTUALLY PAID FOR (added 2026-09-16). Every posting in `jobs` is already billed
    # ($0.005 each) before a single line below runs -- the actor bills per posting RETURNED, and
    # the `break` in the keep loop below saves nothing, it just stops us looking at postings we
    # already own. So the only lever on discovery cost is raising the share of returned postings
    # that can become a NEW company, and until now that share was unmeasurable: the keep loop's
    # already-seen skip (`domain in seen_domains`) `continue`d WITHOUT recording anything, so a
    # run that re-bought a whole week of postings it already had reported an empty
    # rejection_breakdown and looked identical to a run that genuinely found nothing new.
    #
    # This pre-pass is free (pure in-memory arithmetic over postings already fetched and paid
    # for) and deliberately walks EVERY posting, not just the ones before the keep loop's break,
    # because the denominator we need is "what did we pay for", not "what did we look at". It
    # only counts -- no paid call is made or moved here, so instrumenting a run cannot itself
    # cost anything or change which companies get kept.
    paid_postings = len(jobs)
    postings_no_domain = 0
    postings_already_owned = 0
    postings_duplicate_in_run = 0
    domains_new: set[str] = set()
    _domains_seen_in_prepass: set[str] = set()
    for job in jobs:
        domain = _normalize_domain(job.get("org_linkedin_website") or "")
        if not domain:
            postings_no_domain += 1
            continue
        if domain in seen_domains:
            postings_already_owned += 1
            continue
        if domain in _domains_seen_in_prepass:
            postings_duplicate_in_run += 1
            continue
        _domains_seen_in_prepass.add(domain)
        domains_new.add(domain)

    seen_this_run: set[str] = set()
    kept: list[Company] = []
    rejection_counts: dict[str, int] = {}

    for job in jobs:
        if len(kept) >= target:
            break
        domain = _normalize_domain(job.get("org_linkedin_website") or "")
        if not domain or domain in seen_domains or domain in seen_this_run:
            continue
        seen_this_run.add(domain)

        headcount = job.get("org_linkedin_headcount")
        # Check against the band THIS RUN asked for, not the module defaults. Parameterising the
        # actor call without parameterising this safety net made it silently contradict the search:
        # a partner run for a $20-150M ICP asked LinkedIn for 125-3771 employees and then rejected
        # all 50 results for not being 25-50, reporting "0 companies discovered" as if nothing matched.
        if headcount is not None and not (effective_emp_min <= headcount <= effective_emp_max):
            # Real safety net: the actor's own filter has shown occasional stale-bucket
            # mismatches (validated live -- RADICL/QED Investors passed organizationSizeFilter
            # "11-50" despite org_linkedin_headcount 67/75) -- trust the raw number over the
            # filter having already applied it correctly.
            rejection_counts["headcount_out_of_target"] = rejection_counts.get("headcount_out_of_target", 0) + 1
            continue

        org_headquarters = job.get("org_linkedin_headquarters")
        if _is_us_headquarters(org_headquarters) is False:
            # Real, confirmed miss (2026-09-07): a company hiring for a "... - USA Market" role
            # is not the same as a US-headquartered company -- locationSearch/countries_derived
            # only filter the job posting's own location, never the org's real HQ. Only rejects
            # on a CONFIRMED non-US headquarters string; missing data (None) falls through
            # unfiltered rather than discarding a company we simply have no HQ text for.
            rejection_counts["non_us_headquarters"] = rejection_counts.get("non_us_headquarters", 0) + 1
            continue

        title = job.get("title") or ""
        description = job.get("description_text") or ""
        role = _classify_role(title)
        product_fit_categories = _detect_product_fit_signals(description)
        if not role and not product_fit_categories:
            rejection_counts["no_local_classification_match"] = rejection_counts.get("no_local_classification_match", 0) + 1
            continue

        # Free check against the company's own public LinkedIn page, deliberately placed after every
        # other free filter and before the first paid step (assess_team_composition below). Catches
        # what the actor's fields cannot show: declared size far above the member count, a non-US
        # country behind a bare city HQ, recruiting firms posting clients' jobs, and subsidiaries.
        # See company_profile_check.py for the batch-127 cases each rule came from.
        profile = fetch_public_company_profile(job.get("organization_url"))
        if profile is None:
            rejection_counts["profile_unverified_kept"] = rejection_counts.get("profile_unverified_kept", 0) + 1
        profile_reason = profile_rejection_reason(profile, effective_emp_min, effective_emp_max)
        if profile_reason:
            rejection_counts[profile_reason] = rejection_counts.get(profile_reason, 0) + 1
            continue

        company = Company(
            batch_id=batch_id,
            name=job.get("organization") or "Unknown",
            domain=domain,
            industry=job.get("org_linkedin_industry") or None,
            employee_count=headcount,
            location=org_headquarters or None,
            source="apify:fantastic-jobs_advanced-linkedin-job-search-api",
            active_job_title=title or None,
            product_fit_jd_categories=product_fit_categories or None,
            linkedin_url=job.get("organization_url") or None,
            hiring_signal_posting_count=domain_posting_counts.get(domain),
            tofu_keyword_found=TOFU_KEYWORD in description.lower(),
        )
        db.add(company)
        db.commit()
        db.refresh(company)

        # Team composition runs BEFORE hire-type classification (reordered 2026-08-14) --
        # assess_team_composition persists sales_headcount_percent/marketing_headcount_percent
        # as a free byproduct of its own paid calls, and _infer_hire_type() below needs those
        # percentages to be real, not None, or it always falls back to "unknown".
        team_fit = assess_team_composition(company, db)
        if team_fit["tier"] == "excluded":
            rejection_counts["full_team"] = rejection_counts.get("full_team", 0) + 1
            db.delete(company)
            db.commit()
            continue

        if role:
            hire_type = _infer_hire_type(company, role)
            strength, reasoning = _classify_signal(company, role, hire_type, description)
            if TOFU_KEYWORD in description.lower():
                reasoning += " [TOFU pipeline keyword found in JD]"
            reasoning += f" [posting: {job.get('url')}] [headcount: {headcount}] [industry: {job.get('org_linkedin_industry')}]"
            company.hiring_signal_role = role
            company.hiring_signal_hire_type = hire_type
            company.hiring_signal_strength = strength
            company.hiring_signal_reasoning = reasoning
            db.commit()

        # Keep the evidence we already paid for, linked to the company it produced. Placed
        # after the team-composition gate above so a company that gets deleted never leaves an
        # orphaned signal behind, and after the hiring_signal_* writes so the Company row is
        # complete first. Never allowed to break discovery: a signal-write failure must not
        # discard a company that was otherwise successfully found and paid for.
        try:
            _persist_posting_as_signal(db, tenant_id, company, job)
        except Exception:  # noqa: BLE001 -- evidence persistence is additive, never fatal to discovery
            db.rollback()
            logging.getLogger(__name__).exception(
                "failed to persist linkedin_job signal for company_id=%s; company kept", company.id
            )

        seen_domains.add(domain)
        kept.append(company)

    return {
        "companies_discovered": len(kept),
        "postings_checked": len(jobs),
        "rejection_breakdown": rejection_counts,
        "budget_stopped_early": False,
        "estimated_cost_usd": estimate_cost_usd(len(jobs)),
        "api_error": None,
        # The real unit economics of this call, so a run can be judged on what it bought rather
        # than only on what it kept (see the pre-pass above). `wasted_cost_usd` is the money spent
        # on postings that could never have produced a new company no matter how good the filters
        # below are -- companies we already own, duplicate postings from the same employer, and
        # postings with no resolvable company domain at all.
        "cost_efficiency": {
            "paid_postings": paid_postings,
            "requested_limit": discovery_limit,
            "postings_already_owned": postings_already_owned,
            "postings_duplicate_in_run": postings_duplicate_in_run,
            "postings_no_domain": postings_no_domain,
            "new_domains_available": len(domains_new),
            "companies_kept": len(kept),
            "wasted_cost_usd": round(
                (postings_already_owned + postings_duplicate_in_run + postings_no_domain) * COST_PER_JOB_USD, 4
            ),
            "cost_per_company_usd": (
                round(estimate_cost_usd(paid_postings) / len(kept), 4) if kept else None
            ),
        },
    }
