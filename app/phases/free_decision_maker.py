"""Free/cheap decision-maker resolution, tried before Deepline's paid search_contact
(~$0.17/company). Three layers, each validated live, each imperfect -- a miss at any of them
falls through cleanly to the next, then finally to the existing Deepline flow in
decision_maker.py, so this can only ever save money, never cost a company its shot at a real
contact:

1. Jobo's free company-profile leadership list (name + title, no cost at all for the profile
   lookup itself) -- confirmed real for real companies, but only ~11% of Apify-discovered
   companies exist in Jobo's index at all (see progress-log/session notes 2026-08-07).
2. Apify's memo23/linkedin-people-search actor (~$0.004/profile + $0.005/run) to resolve that
   named person's real LinkedIn URL -- Jobo's own "linkedin_url" field is mislabeled and
   actually contains Crunchbase links, confirmed live, so it's not directly usable. This step
   is REQUIRED to get a pushable Contact; skipping it (and using the Crunchbase link, or a
   general web search for the person) doesn't work -- both were tested live and failed:
   Crunchbase blocks automated access outright (403 / 0 results across three different
   scraping approaches), and Google doesn't reliably index LinkedIn's own profile pages, only
   posts/activity.
3. Google's AI Overview (~$0.0085/query, `_find_via_google_search`), tried when Jobo has no
   data at all -- validated live 2026-08-11: correct, real answers for DAKCS and GovEase
   (both absent from Jobo's index). Real, confirmed risk: it can confidently answer for the
   WRONG company when the name is short/generic (a real test returned "Nebi" 's answer as the
   CEO of "Nebius", an unrelated, much larger company) -- so this layer's LLM-extraction step
   requires the model to flag low confidence, and its LinkedIn verification step
   (_resolve_linkedin_url_strict) requires BOTH a name match AND a current-company match,
   stricter than layer 1's name-only check.

Verification is mandatory, not optional, for every layer: found live that the people-search
actor's `company` filter can return "someone else who works there" ranked by prominence rather
than strictly the requested first/last name (a real miss: searching for Neal Sarraf at First
Resonance returned Timothy Gentry, Evan Sloss, and Karan Talati -- not Neal). Accepting an
unverified top result risks messaging the wrong person, which is worse than finding nobody."""

import httpx
from sqlalchemy.orm import Session

from app.apify_client import ApifyError, search_google_ai_overview, search_linkedin_people
from app.apify_client import _get_api_key as _get_apify_api_key
from app.db.models import Company
from app.jobo_client import JoboError, find_company_id_by_name, get_company_profile
from app.jobo_client import _get_api_key as _get_jobo_api_key
from app.llm_client import generate_json
from app.phases.decision_maker import (
    CEO_TITLE_KEYWORDS,
    CEO_TITLE_PRESIDENT_EXCLUSIONS,
    SALES_LEADER_TITLE_KEYWORDS,
)

# Raised from 3 -> 7 (2026-08-12): confirmed live that a real, correct decision-maker can sit
# outside the top 3 results -- KORONA POS's actual CEO (Till Freier) never appeared in a
# 3-result search, only random other employees ranked by the actor's own prominence heuristic,
# not name relevance. Cheap to widen (~$0.004/extra profile via Apify).
PEOPLE_SEARCH_MAX_RESULTS = 7

GOOGLE_LEADER_EXTRACTION_PROMPT = """The following is a Google AI Overview answering a \
search for the founder/CEO of "{company_name}" ({domain}).

AI Overview text:
{text}

Extract up to 2 people, ONLY if the text is clearly about the company at domain "{domain}" \
-- not a different, similarly-named company. This is a real, confirmed failure mode: a search \
for a small company called "Nebi" once returned the CEO of "Nebius", a completely unrelated, \
much larger company, because the AI Overview assumed that's what was meant. If the text \
hedges, guesses, or explicitly says it might be a different company, treat that as NOT \
confident.

List whoever is CURRENTLY, ACTIVELY running the company FIRST (current CEO/President/head \
executive), even if their title is described loosely rather than with the exact word "CEO" --\
 then the ORIGINAL FOUNDER second, only if they're a different person. This ordering matters: \
a company's original founder from decades ago is often no longer active day-to-day (retired, \
moved on) even though the text still credits them as founder, while whoever is described as \
"currently leads/serves as President/head executive" is the real, present-day contact.

Return JSON exactly in this shape:
{{"candidates": [{{"first_name": "...", "last_name": "...", "title": "..."}}, ...], "confident_same_company": true}}

If the text doesn't clearly identify anyone for this exact company, return:
{{"candidates": [], "confident_same_company": false}}"""


def _split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split(" ", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


def _matches_title(title: str, keywords: list[str], require_bare_president: bool = False) -> bool:
    title_lower = (title or "").lower()
    if any(kw in title_lower for kw in keywords):
        return True
    if require_bare_president and "president" in title_lower and not any(excl in title_lower for excl in CEO_TITLE_PRESIDENT_EXCLUSIONS):
        return True
    return False


def _names_match(requested_first: str, requested_last: str, candidate_name: str) -> bool:
    """Strict-ish check: the candidate's own returned name must actually contain both the
    requested first and last name -- not just "same company". This is the safeguard against
    the real miss found live (see module docstring)."""
    candidate_lower = (candidate_name or "").lower()
    return requested_first.strip().lower() in candidate_lower and requested_last.strip().lower() in candidate_lower


def _slug_matches(requested_first: str, requested_last: str, profile_url: str) -> bool:
    """Fallback for _names_match: confirmed live 2026-08-11 that this actor's `name` field is
    sometimes a headline/tagline instead of the person's actual name on "partial": true
    profiles (e.g. "Driving down cycle times and building more homes." instead of "Henry
    Dziuba") -- which made _names_match wrongly reject two real, correct matches (Roy Raugh at
    Valens Software, Henry Dziuba at TradeTrax) even though their real name was sitting right
    there in the profile URL slug (linkedin.com/in/roy-raugh-54891b36a). Checked as an OR with
    _names_match, never instead of it -- the URL slug is a real, LinkedIn-assigned identifier
    tied to the actual person, not free text, so it's a reliable independent signal."""
    if not profile_url:
        return False
    slug = profile_url.rstrip("/").split("/")[-1].lower().replace("-", " ")
    return requested_first.strip().lower() in slug and requested_last.strip().lower() in slug


def _jobo_leadership_candidates(db: Session, tenant_id: int, company: Company) -> list[dict]:
    """Returns [] on any failure (no credential, no match, Jobo error) -- this is a
    best-effort free pre-check, never allowed to block or fail the real decision-maker
    search that follows it."""
    try:
        api_key = _get_jobo_api_key(db, tenant_id)
    except JoboError:
        return []
    try:
        with httpx.Client() as client:
            company_id = find_company_id_by_name(client, api_key, company.name)
            if not company_id:
                return []
            profile = get_company_profile(client, company_id)
            return (profile or {}).get("leadership") or []
    except (httpx.HTTPError, JoboError):
        return []


def _resolve_linkedin_url(db: Session, tenant_id: int, first_name: str, last_name: str, company_name: str) -> str | None:
    try:
        api_key = _get_apify_api_key(db, tenant_id)
        candidates = search_linkedin_people(api_key, first_name, last_name, company_name, max_results=PEOPLE_SEARCH_MAX_RESULTS)
    except ApifyError:
        return None
    for candidate in candidates:
        if _names_match(first_name, last_name, candidate.get("name") or "") or _slug_matches(first_name, last_name, candidate.get("profileUrl") or ""):
            return candidate.get("profileUrl")
    return None


def _resolve_linkedin_url_strict(db: Session, tenant_id: int, first_name: str, last_name: str, company_name: str) -> str | None:
    """Same as _resolve_linkedin_url but ALSO requires the company to be verifiable from the
    candidate's own profile text -- used only for Google-AI-Overview-derived candidates,
    where the name itself has already passed through an LLM extraction step and could
    plausibly be a real person at the WRONG company (the Nebi/Nebius failure mode), not just
    an unrelated same-named person. Name-match alone (as the Jobo layer uses) isn't enough
    protection here since the AI Overview's own confusion already produced a real,
    correctly-spelled name.

    Checks `summary`/`about`, NOT `currentCompany` -- confirmed live 2026-08-11 that this
    actor's `currentCompany` field is null on essentially every real result (checked 6 real
    candidates, all null), which made the original currentCompany-based check reject every
    match unconditionally, including genuinely correct ones. `summary`/`about` reliably
    contains real company text instead (e.g. "Senior Account Executive at DAKCS Software
    Systems"), confirmed present on the same real results."""
    try:
        api_key = _get_apify_api_key(db, tenant_id)
        candidates = search_linkedin_people(api_key, first_name, last_name, company_name, max_results=PEOPLE_SEARCH_MAX_RESULTS)
    except ApifyError:
        return None
    company_lower = company_name.strip().lower()
    for candidate in candidates:
        if not (_names_match(first_name, last_name, candidate.get("name") or "") or _slug_matches(first_name, last_name, candidate.get("profileUrl") or "")):
            continue
        profile_text = f"{candidate.get('summary') or ''} {candidate.get('about') or ''}".lower()
        if not profile_text.strip():
            continue  # unverifiable -- reject rather than risk a wrong-company match
        if company_lower in profile_text:
            return candidate.get("profileUrl")
    return None


def _find_via_google_search(db: Session, tenant_id: int, company: Company) -> dict | None:
    """Third free/cheap layer, tried after Jobo misses -- Google's AI Overview (via Apify,
    ~$0.0085/query) often has real leadership data (confirmed live 2026-08-11: correct,
    verified answers for DAKCS and GovEase, both companies Jobo had no data for at all). The
    query is domain-qualified and the result goes through strict LinkedIn verification
    (name AND current company) before being trusted -- see _resolve_linkedin_url_strict's
    docstring for why name-match alone isn't enough for this particular source."""
    if not company.domain:
        return None
    query = f"{company.name} {company.domain} founder CEO"
    try:
        api_key = _get_apify_api_key(db, tenant_id)
        content = search_google_ai_overview(api_key, query)
    except ApifyError:
        return None
    if not content:
        # Confirmed live (2026-08-12): Google's AI Overview is non-deterministic -- the exact
        # same domain-qualified query returned nothing once, then a real, verifiable answer
        # (Pitstop's Emmett Kilduff) shortly after. One retry catches this transient-empty
        # class of miss for the cost of one extra query (~$0.0085).
        try:
            content = search_google_ai_overview(api_key, query)
        except ApifyError:
            return None
    if not content:
        return None

    try:
        extracted = generate_json(
            GOOGLE_LEADER_EXTRACTION_PROMPT.format(company_name=company.name, domain=company.domain, text=content),
            db, tenant_id, max_tokens=400,
        )
    except Exception:
        return None

    if not extracted.get("confident_same_company"):
        return None

    # Try each candidate in the LLM's priority order (current active leader first, historical
    # founder second) -- confirmed live 2026-08-11 this matters: a real case (DAKCS) named
    # both Kent Green (1980s founder, LinkedIn verification correctly failed -- long retired)
    # and Andy Shumway (current President, actually verifiable) in the same AI Overview. Only
    # trying the first candidate silently lost a real, findable contact.
    for candidate in extracted.get("candidates") or []:
        first_name, last_name = candidate.get("first_name"), candidate.get("last_name")
        if not first_name or not last_name:
            continue
        title = candidate.get("title") or ""

        linkedin_url = _resolve_linkedin_url_strict(db, tenant_id, first_name, last_name, company.name)
        if not linkedin_url:
            continue

        thread_role = "sales_leader" if _matches_title(title, SALES_LEADER_TITLE_KEYWORDS) else "founder_ceo"
        return {
            "first_name": first_name,
            "last_name": last_name,
            "title": title,
            "linkedin_url": linkedin_url,
            "thread_role": thread_role,
            "reasoning": f"Google AI Overview match (title={title!r}), LinkedIn resolved+verified (name+current-company match)",
        }
    return None


def resolve_fallback_email(db: Session, tenant_id: int, company: Company, first_name: str) -> tuple[str, str] | None:
    """Called for ANY contact (free-path or paid-Deepline-path) that still has no email after
    the primary resolution. Two free, zero-verification layers, in order:

    1. Jobo's company-level `email_address` (a generic info@/support@ address, not personal --
       confirmed live 2026-08-10 this is the ONLY email field anywhere in Jobo's data model;
       their leadership objects have no personal email at all, confirmed three independent
       ways -- raw API docs, marketing site, and the CompanyLeaderDto field reference).
    2. A single pattern guess, firstname@domain -- NOT verified. Self-built SMTP RCPT TO
       verification was built and worked perfectly locally (see app/email_verify.py) but
       Render blocks all outbound port 25 ("[Errno 101] Network is unreachable", confirmed
       live 2026-08-10), so it cannot run in production. Deliberately guesses only the single
       most common pattern, not a multi-pattern chain -- more guesses without catch-all
       detection just means more chances of silently landing in a wrong-but-existing inbox.
       Caller must treat this as a real guess (see Contact.email_source), not a confirmed fact.

    Returns (email, source) or None if nothing found."""
    try:
        api_key = _get_jobo_api_key(db, tenant_id)
        with httpx.Client() as client:
            company_id = find_company_id_by_name(client, api_key, company.name)
            if company_id:
                profile = get_company_profile(client, company_id)
                generic_email = (profile or {}).get("email_address")
                if generic_email:
                    return generic_email, "jobo_company"
    except (httpx.HTTPError, JoboError):
        pass

    if first_name and company.domain:
        guess = f"{first_name.strip().lower()}@{company.domain.strip().lower()}"
        return guess, "pattern_guess"

    return None


def find_free_decision_maker(db: Session, tenant_id: int, company: Company) -> dict | None:
    """Tries three free/cheap layers in order, each falling through cleanly to the next on a
    miss -- caller (decision_maker.find_decision_maker) only falls through to the paid
    Deepline path if ALL of these miss:
    1. Jobo's free company-profile leadership list (name + title) -> Apify people-search
       (~$0.02, name-verified) to resolve a real LinkedIn URL.
    2. Google's AI Overview (~$0.0085/query, see _find_via_google_search) when Jobo has no
       data for this company at all -- validated live 2026-08-11 as a real, meaningful lift
       (found DAKCS and GovEase's real founders, both companies absent from Jobo's index)."""
    leadership = _jobo_leadership_candidates(db, tenant_id, company)
    for keywords, require_bare_president, thread_role in [
        (CEO_TITLE_KEYWORDS, True, "founder_ceo"),
        (SALES_LEADER_TITLE_KEYWORDS, False, "sales_leader"),
    ]:
        for person in leadership:
            title = person.get("title") or ""
            if not _matches_title(title, keywords, require_bare_president):
                continue
            first_name, last_name = _split_name(person.get("name") or "")
            if not first_name or not last_name:
                continue
            linkedin_url = _resolve_linkedin_url(db, tenant_id, first_name, last_name, company.name)
            if linkedin_url:
                return {
                    "first_name": first_name,
                    "last_name": last_name,
                    "title": title,
                    "linkedin_url": linkedin_url,
                    "thread_role": thread_role,
                    "reasoning": f"Jobo leadership match (title={title!r}), LinkedIn resolved+verified via Apify people-search",
                }

    return _find_via_google_search(db, tenant_id, company)
