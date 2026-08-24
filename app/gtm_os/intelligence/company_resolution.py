"""Person -> Company resolution -- closes the real gap the Phase 10+ acceptance audit found:
strong person-level evidence (a `linkedin_post` reaching declared/implied_gap tier) can open a
real ProblemHypothesis/DemandHypothesis, but nothing anywhere resolves that signal's author's
employer to a real Company row, so `company_id` stays permanently NULL and
opportunity.py's own hard "Part E" gate (`demand.company_id is not None`) blocks it from ever
becoming an Opportunity -- confirmed live against production (2026-08-22): the one real
"solution_evaluation_mention" signal that opened a real ProblemHypothesis/DemandHypothesis pair
sits with `company_id = NULL` forever, unreachable by any existing code path.

NOT a redesign of sensing, NOT a new discovery pipeline, NOT a decision-maker/contact search.
This module answers exactly one question -- "which existing Company, if any, does this signal's
author work for" -- and runs ONLY as a downstream step for InterpretedSignal rows that already
reached an OPENING tier (problem_detection.OPENING_TIERS: declared/implied_gap). Contextual-tier
evidence (the overwhelming majority of real signals today -- see the audit) is never resolved:
it was never going to open a hypothesis anyway, so spending resolution effort on it, even the
free tier, is waste this module deliberately avoids.

RESOLUTION ORDER (cheapest/safest first, per the approved task):
1. Signal already has a company_id -- nothing to do. Guarded by the caller
   (problem_detection.evaluate_interpreted_signal), checked again here for safety.
2. Exact, tenant-scoped, case-insensitive match on Company.name against the signal's
   company_name_raw (the existing free-text "Title at Company" guess
   reverse_discovery._guess_company_name() already produces in sensing.py -- reused unmodified,
   never re-derived). Pure DB read, no provider cost. Exactly one match -> resolved. Zero
   matches -> falls through to step 2.5/3/4 only if explicitly enabled (see allow_paid_enrichment,
   now a real config toggle -- control.company_resolution.allow_paid_enrichment, added 2026-08-24;
   was a hardcoded-False Python default before that date, meaning nothing set anywhere could ever
   turn it on).
   Two or more matches -> ambiguous, resolution stops there -- no fuzzy scoring, no guessing.
2.5. Bounded, OPT-IN-ONLY, DEEPLINE-FREE profile-based enrichment (added 2026-08-24) -- reuses
   the Google Search actor + organic-results reading confirmed live the same day for LinkedIn
   content discovery. Tried FIRST among the paid tiers specifically because it does not share
   Deepline's single point of failure (the real account balance has been negative all session).
   See _try_apify_profile_enrichment.
3. Bounded, OPT-IN-ONLY profile-based enrichment (added 2026-08-23, real gap: signals like the
   Daniel Clarke case have NO company_name_raw at all -- the headline never mentions an employer
   -- so step 2 is a structural no-op for them regardless of ordering, and step 4 below needs a
   company name it will never have). Reuses search_contact (Deepline), the EXACT tool
   decision_maker.py._run_search_contact() already uses in production for V1's real
   decision-maker flow -- same execute_tool()/DeeplineError path, no new provider. Every existing
   caller queries it with a known `domain`; this is the first caller to query by `contact_linkedin`
   alone (the signal author's real captured LinkedIn profile URL, extracted_info
   ["author_profile_url"] -- populated by sensing.py, never invented here). Tried BEFORE step 4
   because a direct profile-identity lookup is strictly more reliable than guessing a domain from
   a bare company name via Google search, and because for a no-name signal it's the ONLY paid tier
   that can possibly resolve anything.
4. Bounded, OPT-IN-ONLY paid enrichment fallback. Reuses two existing capabilities, unmodified,
   no new provider integration:
     a. search_google_ai_overview() (app/apify_client.py) to guess a domain from the company
        name -- the same tool already used elsewhere in this codebase for exactly this kind of
        "what company is this" question (reverse_discovery.py, gtm_partner_classification.py).
     b. jd_first_discovery._real_firmographics() (Deepline Crustdata company-identify, FREE per
        its own docstring) to verify that guessed domain against a real company record and get
        its actual name + professional_network_url.
   Only attempted if step 3 didn't resolve AND a company_name_raw actually exists to guess from.
   Gated behind BOTH BudgetGuard (Deepline's real balance check -- the only budget mechanism
   this codebase has) AND an explicit allow_paid_enrichment=False default. Today's own acceptance
   test found Apify spend has NO budget tracking anywhere in this codebase yet (a real production
   incident, 2026-08-22) -- until a real Apify budget guard exists (an already-agreed separate
   follow-up), this fallback must never fire automatically. Callers must opt in deliberately.
   If the verified domain matches an EXISTING Company in our own table -> resolved. If
   enrichment identifies a real company that does not yet exist in our table, this is
   deliberately still reported as unresolved: creating a new Company from a single social-media
   mention is a materially bigger, riskier decision (a permanent business record) than resolving
   an already-known one, and is explicitly out of scope for this build -- see this module's own
   report for why Option A (link-only) was chosen over Option B (create-on-verify).

STEP 3 FIELD-NAME CAVEAT (be honest about what's confirmed): `search_contact`'s real schema
(confirmed via `deepline tools describe search_contact --json`, 2026-08-23) documents
`contact_linkedin` as a valid input field, but every existing production caller always supplies
`domain` alongside a title filter -- none has ever queried by `contact_linkedin` alone, so the
exact response field name(s) for the matched person's CURRENT company (domain/name) in that
specific query shape have never been observed in a real response. `_try_profile_enrichment` below
defensively checks several plausible field names (the same "try a couple of known variants"
pattern `_make_contact` already uses for `linkedin_url`/`linkedin`) and, if a match is found under
an unrecognized key, still returns enough raw detail in its `reason` for a future session to
verify and tighten this once a real balance is available. This tier cannot be verified end-to-end
until Deepline's balance is topped up (currently negative) -- tested here only against mocked
`execute_tool` responses.

PERSISTENCE: reuses GtmSignal's OWN company_id column (already exists, already nullable, already
what every downstream reader -- InterpretedSignal, ProblemHypothesis, DemandHypothesis --
consumes unchanged). Resolution only ever fills it in once confident; nothing here invents a new
identity field. Four new, narrow, nullable columns on GtmSignal record WHY/HOW/WHEN a resolution
attempt happened (company_resolution_status/method/reason/resolved_at) so "never attempted" (all
four NULL), "resolved", "unresolved", and "ambiguous" are all distinguishable afterward --
categorical and explainable, never a numeric confidence score (per the task's own instruction)."""

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.budget_guard import BudgetExceededError, BudgetGuard
from app.db.models import Company
from app.deepline_client import DeeplineError, execute_tool, extract_rows
from app.gtm_os.intelligence.signal import GtmSignal
from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal

# Real cost per search_contact result RETURNED (confirmed live via decision_maker.py's own
# documented figure and the schema's own pricing note, 2026-08-23) -- zero returned contacts are
# free. Requesting 1 result here (we already have a specific person's profile URL, not a filter
# that could match many people) keeps this tier's worst case bounded to one billed result.
PROFILE_ENRICHMENT_PAGE_SIZE = 1

# Defensive field-name candidates for the matched person's current-company identity -- see the
# module docstring's "STEP 3 FIELD-NAME CAVEAT": none of these have been observed in a real
# response yet for a contact_linkedin-only query, only inferred from the schema's documented
# output shape used elsewhere (experience.employment_details.current.*, per
# crustdata_v3_person_search) and search_contact's own already-parsed top-level person fields.
_PROFILE_COMPANY_DOMAIN_KEYS = ("company_website_domain", "company_domain", "domain", "website")
_PROFILE_COMPANY_NAME_KEYS = ("company_name", "company", "current_company", "employer")


def _first_present(person: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = person.get(key)
        if value:
            return value
    return None

RESOLUTION_STATUSES = {"resolved", "unresolved", "ambiguous"}


def _normalize_company_name(name: str | None) -> str:
    return (name or "").strip().lower()


def _exact_name_matches(db: Session, tenant_id: int, company_name_raw: str) -> list[Company]:
    """Tenant-scoped, EXACT (case-insensitive) match only -- never a substring/ilike-with-
    wildcards search, per the task's explicit "do not use fuzzy matching without a verification
    rule" instruction. Existing `.ilike('%term%')` helpers elsewhere in this codebase (routes/
    api.py) are deliberately NOT reused here -- those are UI search-box helpers, not safe
    auto-resolution matchers."""
    from app.db.models import Batch

    normalized_target = _normalize_company_name(company_name_raw)
    if not normalized_target:
        return []
    return (
        db.query(Company)
        .join(Batch, Company.batch_id == Batch.id)
        .filter(Batch.tenant_id == tenant_id)
        .filter(func.lower(Company.name) == normalized_target)
        .all()
    )


def _try_paid_enrichment(db: Session, tenant_id: int, company_name_raw: str) -> dict:
    """Only called when the free exact-name match found zero candidates AND the caller
    explicitly opted into paid enrichment. Returns a result dict, never raises for a provider
    failure (fails safe -> unresolved, same discipline as every other provider-facing function
    in this codebase)."""
    from app.apify_client import _get_api_key as _get_apify_api_key
    from app.apify_client import search_google_ai_overview
    from app.phases.jd_first_discovery import _real_firmographics

    try:
        guard = BudgetGuard(budget_usd=1.0)  # small, bounded ceiling for this one resolution attempt
        guard.check()
    except BudgetExceededError as e:
        return {"status": "unresolved", "method": None, "reason": f"budget_guard_blocked: {e}"}
    except Exception as e:  # noqa: BLE001 -- Deepline CLI/balance-check itself failing must never crash resolution
        return {"status": "unresolved", "method": None, "reason": f"budget_guard_unavailable: {e}"}

    try:
        api_key = _get_apify_api_key(db, tenant_id)
        overview = search_google_ai_overview(api_key, f"{company_name_raw} official website domain")
    except Exception as e:  # noqa: BLE001 -- provider failure -> unresolved, never crash the sweep
        return {"status": "unresolved", "method": None, "reason": f"domain_guess_provider_error: {e}"}

    if not overview:
        return {"status": "unresolved", "method": None, "reason": "no_domain_guess_from_ai_overview"}

    import re

    # Matches the FULL hostname (all dot-separated labels), not just the last label + TLD --
    # found live in verification: a naive single-label pattern matched "example.com" out of
    # "enriched.example.com" and silently resolved to a completely unrelated existing company
    # that happened to share that shorter domain. Requires 2+ labels before the TLD suffix.
    domain_match = re.search(r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:com|io|co|net|org|ai))\b", overview.lower())
    if not domain_match:
        return {"status": "unresolved", "method": None, "reason": "no_domain_pattern_found_in_overview_text"}
    guessed_domain = domain_match.group(1)

    try:
        firmographics = _real_firmographics(guessed_domain)
    except Exception as e:  # noqa: BLE001 -- provider failure -> unresolved, never crash the sweep
        return {"status": "unresolved", "method": None, "reason": f"firmographics_provider_error: {e}"}

    if not firmographics:
        return {"status": "unresolved", "method": None, "reason": f"firmographics_no_confident_match_for_domain:{guessed_domain}"}

    verified_name = firmographics.get("name")
    if _normalize_company_name(verified_name) != _normalize_company_name(company_name_raw):
        return {
            "status": "unresolved",
            "method": None,
            "reason": f"firmographics_name_mismatch: guessed_domain={guessed_domain!r} verified_name={verified_name!r} vs signal_company_name_raw={company_name_raw!r}",
        }

    existing = (
        db.query(Company)
        .filter(func.lower(Company.domain) == guessed_domain.lower())
        .first()
    )
    if existing:
        return {"status": "resolved", "method": "enrichment_domain_match", "reason": None, "company_id": existing.id}

    return {
        "status": "unresolved",
        "method": None,
        "reason": f"enrichment_verified_real_company (domain={guessed_domain!r}, name={verified_name!r}) but it does not exist in our Company table -- creating a new Company from a single social-post mention is out of scope for this build (see module docstring, Option A chosen over Option B)",
    }


def _try_apify_profile_enrichment(db: Session, tenant_id: int, profile_url: str) -> dict:
    """Deepline-free company resolution, added 2026-08-24 -- a real fallback for when Deepline's
    balance is negative (confirmed live all session), reusing the SAME Google Search actor and
    organic-results-reading capability confirmed live and already proven for LinkedIn content
    discovery (investigation_execution.py's linkedin_post_search fix, same day). Tried FIRST,
    before the two Deepline-dependent tiers below, precisely because it doesn't share their
    single point of failure. Budget-guarded via the real Apify guard (app.apify_budget_guard),
    never Deepline's BudgetGuard -- a genuinely separate spend/failure path. Same Option A
    discipline as every other tier here: resolves to an EXISTING Company only, never fabricates
    one from a single social-post mention."""
    from app.apify_budget_guard import STATUS_ALLOWED as APIFY_BUDGET_ALLOWED
    from app.apify_budget_guard import check_apify_budget
    from app.apify_client import GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD, ApifyError
    from app.apify_client import _get_api_key as _get_apify_api_key
    from app.apify_client import search_google_organic_results

    try:
        api_key = _get_apify_api_key(db, tenant_id)
    except ApifyError as e:
        return {"status": "unresolved", "method": None, "reason": f"apify_credential_unavailable: {e}"}

    budget = check_apify_budget(db, tenant_id, GOOGLE_SEARCH_COST_PER_QUERY_NO_AI_OVERVIEW_USD)
    if budget["status"] != APIFY_BUDGET_ALLOWED:
        return {"status": "unresolved", "method": None, "reason": f"apify_budget_blocked: {budget['reason']}"}

    try:
        organic_results = search_google_organic_results(api_key, f"{profile_url} current company employer", max_pages=1)
    except ApifyError as e:
        return {"status": "unresolved", "method": None, "reason": f"google_search_provider_error: {e}"}

    if not organic_results:
        return {"status": "unresolved", "method": None, "reason": "no_organic_results_for_profile_query"}

    import re

    # Same full-hostname pattern _try_paid_enrichment uses -- see that function's own comment
    # for why a naive single-label match is unsafe (silently matches an unrelated shorter domain).
    # Deliberately scans ONLY title/description text, never the result `url` field itself --
    # confirmed live in testing: a search-result URL's own host (e.g. a generic short domain
    # from an unrelated result) can itself look like a valid domain and get falsely matched
    # instead of the real company mentioned in the actual text.
    domain_pattern = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:com|io|co|net|org|ai))\b")
    combined_text = " ".join(f"{r.get('title', '')} {r.get('description', '')}" for r in organic_results[:5]).lower()
    domain_match = domain_pattern.search(combined_text)
    if not domain_match:
        return {"status": "unresolved", "method": None, "reason": "no_domain_pattern_found_in_organic_results"}
    guessed_domain = domain_match.group(1)

    existing = db.query(Company).filter(func.lower(Company.domain) == guessed_domain).first()
    if existing:
        return {"status": "resolved", "method": "apify_profile_enrichment", "reason": None, "company_id": existing.id}

    return {
        "status": "unresolved",
        "method": None,
        "reason": (
            f"apify_profile_enrichment_guessed_domain={guessed_domain!r} but no existing Company matches it -- "
            "creating one from a single social-post mention is out of scope (see module docstring, Option A over Option B)"
        ),
    }


def _try_profile_enrichment(db: Session, tenant_id: int, profile_url: str) -> dict:
    """Only called when no exact company-name match was found AND the caller explicitly opted
    into paid enrichment AND the signal actually captured a real author profile URL (never
    invented here -- see resolve_company_for_signal). Same fail-safe discipline as
    _try_paid_enrichment: a provider failure never raises, it just falls through to
    "unresolved" so one signal's failure can never crash a sweep."""
    try:
        guard = BudgetGuard(budget_usd=1.0)  # same small, bounded ceiling as the existing paid tier
        guard.check()
    except BudgetExceededError as e:
        return {"status": "unresolved", "method": None, "reason": f"budget_guard_blocked: {e}"}
    except Exception as e:  # noqa: BLE001 -- Deepline CLI/balance-check itself failing must never crash resolution
        return {"status": "unresolved", "method": None, "reason": f"budget_guard_unavailable: {e}"}

    try:
        response = execute_tool("search_contact", {"contact_linkedin": profile_url, "page_size": PROFILE_ENRICHMENT_PAGE_SIZE})
    except DeeplineError as e:
        return {"status": "unresolved", "method": None, "reason": f"search_contact_provider_error: {e}"}

    raw = response.get("toolResponse", {}).get("raw", {})
    persons = raw.get("output", {}).get("persons") if isinstance(raw, dict) and isinstance(raw.get("output"), dict) else None
    if not isinstance(persons, list):
        persons = extract_rows(response, "persons")
    if not persons:
        return {"status": "unresolved", "method": None, "reason": f"search_contact_no_match_for_profile_url:{profile_url}"}

    person = persons[0]
    company_domain = _first_present(person, _PROFILE_COMPANY_DOMAIN_KEYS)
    company_name = _first_present(person, _PROFILE_COMPANY_NAME_KEYS)
    if not company_domain and not company_name:
        return {
            "status": "unresolved",
            "method": None,
            "reason": f"search_contact_matched_profile_but_no_recognized_company_field (checked {_PROFILE_COMPANY_DOMAIN_KEYS + _PROFILE_COMPANY_NAME_KEYS}; raw_person_keys={sorted(person.keys())})",
        }

    existing = None
    if company_domain:
        existing = db.query(Company).filter(func.lower(Company.domain) == company_domain.lower()).first()
    if not existing and company_name:
        from app.db.models import Batch

        existing = (
            db.query(Company)
            .join(Batch, Company.batch_id == Batch.id)
            .filter(Batch.tenant_id == tenant_id)
            .filter(func.lower(Company.name) == _normalize_company_name(company_name))
            .first()
        )

    if existing:
        return {"status": "resolved", "method": "profile_enrichment_search_contact", "reason": None, "company_id": existing.id}

    return {
        "status": "unresolved",
        "method": None,
        "reason": (
            f"profile_enrichment_identified_real_company (domain={company_domain!r}, name={company_name!r}) "
            "but it does not exist in our Company table -- creating one from a single social-post mention is "
            "out of scope (see module docstring, Option A over Option B)"
        ),
    }


def resolve_company_for_signal(
    db: Session,
    tenant_id: int,
    signal: GtmSignal,
    allow_paid_enrichment: bool = False,
) -> dict:
    """Runs the full resolution order for one GtmSignal, persists the outcome onto that same
    row (company_id when resolved, plus the four resolution_* bookkeeping columns always), and
    returns the same result dict for the caller's immediate use (so problem_detection.py doesn't
    need a second DB read of the row it just updated).

    Idempotent: if this signal already has company_resolution_status set (a previous attempt
    already ran, in any outcome), this is a no-op that just returns the already-persisted
    result -- never re-attempts (and never re-spends) on a signal already looked at."""
    if signal.company_resolution_status is not None:
        return {
            "status": signal.company_resolution_status,
            "method": signal.company_resolution_method,
            "reason": signal.company_resolution_reason,
            "company_id": signal.company_id,
        }

    if signal.company_id is not None:
        result = {"status": "resolved", "method": "explicit", "reason": None, "company_id": signal.company_id}
    else:
        result = None
        matches = _exact_name_matches(db, tenant_id, signal.company_name_raw or "")
        if len(matches) == 1:
            result = {"status": "resolved", "method": "exact_name_match", "reason": None, "company_id": matches[0].id}
        elif len(matches) > 1:
            result = {
                "status": "ambiguous",
                "method": None,
                "reason": f"{len(matches)} existing companies match name {signal.company_name_raw!r} exactly: ids={[m.id for m in matches]}",
                "company_id": None,
            }

        # Step 2.5 (Deepline-FREE profile-based enrichment, added 2026-08-24) -- tried FIRST among
        # the paid tiers, precisely because it does not share Deepline's single point of failure
        # (the real, confirmed-negative Deepline balance blocks steps 3/4 below every time).
        profile_attempt_reason = None
        if result is None and allow_paid_enrichment:
            profile_url = (signal.extracted_info or {}).get("author_profile_url")
            if profile_url:
                apify_result = _try_apify_profile_enrichment(db, tenant_id, profile_url)
                if apify_result["status"] == "resolved":
                    result = apify_result
                else:
                    profile_attempt_reason = f"apify_profile_enrichment: {apify_result.get('reason')}"
            else:
                profile_attempt_reason = "no_profile_url_captured_on_signal"

        # Step 3 (Deepline profile-based enrichment) -- tried whenever exact-name matching AND
        # the Deepline-free tier above didn't already resolve/disambiguate it.
        if result is None and allow_paid_enrichment:
            profile_url = (signal.extracted_info or {}).get("author_profile_url")
            if profile_url:
                profile_result = _try_profile_enrichment(db, tenant_id, profile_url)
                if profile_result["status"] == "resolved":
                    result = profile_result
                else:
                    profile_attempt_reason = f"{profile_attempt_reason}; deepline_profile_enrichment: {profile_result.get('reason')}"

        # Step 4 (existing name-guessing paid fallback) -- only makes sense when a company name
        # actually exists to guess a domain from.
        if result is None and allow_paid_enrichment and (signal.company_name_raw or "").strip():
            result = _try_paid_enrichment(db, tenant_id, signal.company_name_raw)
            result.setdefault("company_id", None)
            if result["status"] != "resolved" and profile_attempt_reason:
                result["reason"] = f"{result.get('reason')}; profile_enrichment_also_failed: {profile_attempt_reason}"

        if result is None:
            has_name = bool((signal.company_name_raw or "").strip())
            if not allow_paid_enrichment:
                reason = (
                    f"no_exact_name_match_for {signal.company_name_raw!r} and paid enrichment not enabled for this call"
                    if has_name
                    else "no_company_name_raw_available_on_signal and paid enrichment (including profile-based lookup) not enabled for this call"
                )
            else:
                reason = (
                    "no_company_name_raw_available_on_signal"
                    if not has_name
                    else f"no_exact_name_match_for {signal.company_name_raw!r}"
                )
                reason += f"; profile_enrichment_attempt: {profile_attempt_reason}"
            result = {"status": "unresolved", "method": None, "reason": reason, "company_id": None}

    signal.company_resolution_status = result["status"]
    signal.company_resolution_method = result.get("method")
    signal.company_resolution_reason = result.get("reason")
    signal.company_resolved_at = datetime.utcnow()
    if result["status"] == "resolved" and result.get("company_id"):
        signal.company_id = result["company_id"]
    db.add(signal)
    db.commit()

    return result


def ensure_company_resolved_if_needed(
    db: Session,
    tenant_id: int,
    interpreted_signal: InterpretedSignal,
    opening_tier: bool,
    allow_paid_enrichment: bool = False,
) -> None:
    """Called by problem_detection.evaluate_interpreted_signal() -- the one integration point,
    per the task's "minimum integration" instruction. Only attempts resolution when BOTH:
    (a) the InterpretedSignal has no company_id yet, and (b) its evidence tier already qualifies
    to OPEN a hypothesis (declared/implied_gap) -- contextual-tier evidence is never resolved,
    since it could never open anything regardless of company identity. Updates BOTH the
    underlying GtmSignal (the durable, reusable identity record) and this specific
    InterpretedSignal instance in-place, so the caller's own immediately-following logic sees the
    resolved company_id without a second read."""
    if interpreted_signal.company_id is not None or not opening_tier:
        return

    raw_signal = db.get(GtmSignal, interpreted_signal.source_signal_id)
    if raw_signal is None:
        return

    result = resolve_company_for_signal(db, tenant_id, raw_signal, allow_paid_enrichment=allow_paid_enrichment)
    if result["status"] == "resolved" and result.get("company_id"):
        interpreted_signal.company_id = result["company_id"]
        db.add(interpreted_signal)
        db.commit()
