"""Jobo discovery pipeline -- the second, fully independent discovery source alongside
Deepline's (app/phases/discovery.py + hiring_signal.py + decision_maker.py). Adapted from
the proven standalone script (scripts/jobo_comparison_test.py) into a real phase module that
persists Company/Score/Contact rows to the database instead of just printing, and drops that
script's local-text-file exclusion list (fragile on a deployed server with an ephemeral
filesystem) in favor of the same DB-domain-exclusion pattern Deepline's discovery.py uses.

Jobo's job-search API returns company + decision-maker leadership data in the SAME response
cycle (search hit -> free company profile lookup, which includes a leadership list), so this
single phase covers what Deepline needs three separate phases + gates for (discovery, hiring
signal, decision maker) -- there is no separate "run decision maker later" step for Jobo
batches, by design.
"""
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.db.models import Batch, Company, Contact, Score
from app.jobo_client import JoboError, JoboCreditGuard, get_company_profile, search_jobs, _get_api_key
from app.phases.decision_maker import CEO_TITLE_KEYWORDS, CEO_TITLE_PRESIDENT_EXCLUSIONS, SALES_LEADER_TITLE_KEYWORDS
from app.phases.hiring_signal import ROLE_KEYWORDS, _detect_product_fit_signals
from app.phases.tech_stack import AI_SDR_TOOL_KEYWORDS, OUTBOUND_TOOL_KEYWORDS

TARGET_EMPLOYEE_SIZE = "11-50"
TARGET_REVENUE_BUCKETS = {None, "under-1m", "1m-10m"}
EXCLUDED_COMPANY_TYPES = {"non_profit", "nonprofit", "government-agency", "educational-institution", "self-employed"}
MAX_TOTAL_FUNDING_USD = 10_000_000
MIN_QUALIFYING_SCORE = 55
PAGE_SIZE = 20

ALL_ROLE_TITLE_KEYWORDS = sorted({kw for kws in ROLE_KEYWORDS.values() for kw in kws})
ALL_ROLE_TITLE_KEYWORDS.extend(["representative", "specialist", "employee", "associate", "inside sales", "growth", "lead generation"])

# Widened 2026-08-07 to test whether the near-zero 11-50-employee hit rate is a query-breadth
# problem or a real data-density gap in Jobo's ATS-sourced index (see run_jobo_discovery
# investigation) -- added generic sales-leadership titles small companies are more likely to
# post for than dedicated "SDR"/"BDR" reqs, which tend to be a later, bigger-company hire.
SEARCH_QUERIES = [
    '"sales development representative"',
    '"account executive"',
    '"business development representative"',
    '"inside sales representative"',
    '"head of sales"',
    '"vp of sales"',
    '"vp sales"',
    '"sales director"',
    '"director of sales"',
    '"sales manager"',
    '"sales lead"',
    '"business development manager"',
    '"chief revenue officer"',
]

# Leadership-list-scoped GTM check -- Jobo's company profile only exposes an explicit
# leadership list (not a full per-department employee count the way Crustdata's
# person-search does), so "5+ GTM-titled leaders" is the right-shaped threshold here, not
# the absolute headcount thresholds used in hiring_signal.assess_team_composition (which
# needs a real total-employee-count denominator Jobo doesn't give us).
GTM_LEADERSHIP_KEYWORDS = [
    "sales", "marketing", "bdr", "sdr", "business development", "account executive", "ae",
    "growth", "partnership", "success", "support", "customer service", "customer support",
    "gtm", "ops", "cro", "revenue officer",
]
FULL_TEAM_LEADERSHIP_COUNT = 5


def _fits_icp(profile: dict) -> tuple[bool, str | None]:
    if profile.get("country_code") != "US":
        return False, "not_us"
    if profile.get("company_size") != TARGET_EMPLOYEE_SIZE:
        return False, f"employee_size={profile.get('company_size')!r}"
    if profile.get("revenue") not in TARGET_REVENUE_BUCKETS:
        return False, f"revenue={profile.get('revenue')!r}"
    if profile.get("is_acquired"):
        return False, "acquired"
    if (profile.get("company_type") or "").lower() in EXCLUDED_COMPANY_TYPES:
        return False, f"excluded_type={profile.get('company_type')!r}"
    try:
        total_funding = float(profile.get("total_funding") or 0)
    except (ValueError, TypeError):
        total_funding = 0
    if total_funding > MAX_TOTAL_FUNDING_USD:
        return False, f"funding_exceeded={total_funding}"
    return True, None


def _has_qualifying_signal(job: dict) -> tuple[bool, list[str]]:
    title = (job.get("title") or job.get("normalized_job_title") or "").lower()
    role_matched = any(kw in title for kw in ALL_ROLE_TITLE_KEYWORDS)
    product_fit_categories = _detect_product_fit_signals(job.get("description") or "")
    return (role_matched or bool(product_fit_categories)), product_fit_categories


def _full_leadership_team(leadership: list[dict]) -> tuple[bool, int, list[str]]:
    gtm_people = [f"{p.get('name')} ({p.get('title')})" for p in leadership if any(kw in (p.get("title") or "").lower() for kw in GTM_LEADERSHIP_KEYWORDS)]
    return len(gtm_people) >= FULL_TEAM_LEADERSHIP_COUNT, len(gtm_people), gtm_people


def _best_matching_person(leadership: list[dict], keywords: list[str], require_bare_president: bool = False) -> dict | None:
    for person in leadership:
        title = (person.get("title") or "").lower()
        if any(kw in title for kw in keywords):
            return person
        if require_bare_president and "president" in title and not any(excl in title for excl in CEO_TITLE_PRESIDENT_EXCLUSIONS):
            return person
    return None


def _score(profile: dict, has_primary_contact: bool, signal_strength: str, product_fit_categories: list[str], active_job_title: str) -> dict:
    need = min({"strong": 25, "medium": 15, "weak": 5}.get(signal_strength, 0), 30)

    ability_to_pay = 5
    if profile.get("funding_rounds"):
        ability_to_pay += 10
    try:
        if float(profile.get("total_funding") or 0) > 0:
            ability_to_pay += 5
    except (ValueError, TypeError):
        pass
    ability_to_pay = min(ability_to_pay, 20)

    tech_combined = " ".join(t.lower() for t in (profile.get("technology_list") or []))
    has_outbound_tooling = any(kw in tech_combined for kw in OUTBOUND_TOOL_KEYWORDS)
    has_ai_sdr_tool = any(kw in tech_combined for kw in AI_SDR_TOOL_KEYWORDS)
    outbound_maturity = min((15 if has_outbound_tooling else 0) + (5 if has_ai_sdr_tool else 0), 20)

    product_fit = 0
    if any(k in (profile.get("primary_industry") or "").lower() for k in ["software", "technology", "internet", "it", "services"]):
        product_fit += 10
    if profile.get("country_code") == "US":
        product_fit += 5
    if has_primary_contact:
        product_fit += 5
    product_fit = min(product_fit, 20)

    buying_intent = 0
    if profile.get("funding_rounds"):
        buying_intent += 4
    if any(p in active_job_title.lower() for p in ["first", "founding"]):
        buying_intent += 4
    if any(kw in active_job_title.lower() for kw in ROLE_KEYWORDS["head_of_sales"]):
        buying_intent += 2
    buying_intent += min(len(product_fit_categories) * 3, 9)
    buying_intent = min(buying_intent, 10)

    total = need + ability_to_pay + outbound_maturity + product_fit + buying_intent
    tier = "excluded"
    if total >= 90:
        tier = "hot"
    elif total >= 80:
        tier = "warm"
    elif total >= 70:
        tier = "cool"

    return {
        "need": need, "ability_to_pay": ability_to_pay, "outbound_maturity": outbound_maturity,
        "product_fit": product_fit, "buying_intent": buying_intent, "total_score": total, "tier": tier,
        "has_outbound_tooling": has_outbound_tooling, "has_ai_sdr_tool": has_ai_sdr_tool,
    }


def _existing_domains(tenant_id: int, db: Session) -> set[str]:
    rows = db.query(Company.domain).join(Batch).filter(Batch.tenant_id == tenant_id).filter(Company.domain.isnot(None)).all()
    return {r[0].lower() for r in rows if r[0]}


def run_jobo_discovery(batch_id: int, db: Session, tenant_id: int, target: int = 5, budget_usd: float = 1.5, start_page: int = 1, max_pages: int = 50) -> dict:
    """Phase entrypoint for a Jobo-sourced batch. Stops on whichever comes first: `target`
    qualifying companies found, `budget_usd` spent (JoboCreditGuard, checked after every
    single company), or no more search pages left."""
    api_key = _get_api_key(db, tenant_id)
    excluded_domains = _existing_domains(tenant_id, db)
    guard = JoboCreditGuard(budget_usd)

    qualifying_count = 0
    seen_company_ids: set[str] = set()
    rejection_counts: dict[str, int] = {}
    raw_checked = 0
    page = start_page
    budget_stopped_early = False
    api_error: str | None = None

    with httpx.Client() as client:
        while qualifying_count < target and page < start_page + max_pages:
            try:
                # locations=["United States"] -- real server-side filter, found live
                # (2026-08-07) to have never been passed at all, so every prior run paid to
                # fetch and then locally reject non-US postings (86 of 349 in one real test)
                # instead of excluding them for free at the query level. include_facets
                # requests industries composition at no extra cost, to check real canonical
                # industry values before ever filtering by them (a wrong/typo'd industry name
                # silently matches nothing per Jobo's own docs, no error).
                data, balance = search_jobs(client, api_key, SEARCH_QUERIES, page, PAGE_SIZE,
                                             locations=["United States"], include_facets=["industries"])
                guard.record(balance)
                guard.check()
            except JoboError:
                budget_stopped_early = True
                break
            except httpx.HTTPStatusError as e:
                # Found live: this used to be a silent `except httpx.HTTPError: break`, so a
                # real API failure (invalid/expired key, server error, etc.) was
                # indistinguishable from "genuinely zero postings this page" -- both reported
                # as a clean "completed, 0 companies found" with no error visible anywhere.
                # Capturing the real response body/status here so a broken credential doesn't
                # silently masquerade as an empty result.
                api_error = f"Jobo search failed ({e.response.status_code}): {e.response.text[:500]}"
                break
            except httpx.HTTPError as e:
                api_error = f"Jobo search failed: {e}"
                break

            jobs = data.get("jobs", [])
            if not jobs:
                break

            for job in jobs:
                company_ref = job.get("company") or {}
                company_id = company_ref.get("id")
                if not company_id or company_id in seen_company_ids:
                    continue
                seen_company_ids.add(company_id)

                website = company_ref.get("website") or ""
                domain = website.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].lower().strip()
                if domain and domain in excluded_domains:
                    rejection_counts["already_seen"] = rejection_counts.get("already_seen", 0) + 1
                    continue

                raw_checked += 1
                has_signal, product_fit_categories = _has_qualifying_signal(job)
                if not has_signal:
                    rejection_counts["no_hiring_signal"] = rejection_counts.get("no_hiring_signal", 0) + 1
                    continue

                profile = get_company_profile(client, company_id)
                if profile is None:
                    rejection_counts["profile_not_found"] = rejection_counts.get("profile_not_found", 0) + 1
                    continue
                fits, reason = _fits_icp(profile)
                if not fits:
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    continue

                leadership = profile.get("leadership") or []
                is_full_team, gtm_count, gtm_people = _full_leadership_team(leadership)
                if is_full_team:
                    rejection_counts["full_gtm_team"] = rejection_counts.get("full_gtm_team", 0) + 1
                    continue

                active_job_title = job.get("title") or ""
                signal_strength = "weak"
                if any(kw in active_job_title.lower() for kw in ROLE_KEYWORDS["head_of_sales"]):
                    signal_strength = "strong"
                elif any(kw in active_job_title.lower() for kw in ROLE_KEYWORDS["sdr"]):
                    signal_strength = "strong" if any(p in active_job_title.lower() for p in ["first", "founding"]) else "medium"
                elif any(kw in active_job_title.lower() for kw in ROLE_KEYWORDS["ae"]):
                    signal_strength = "medium"

                person = _best_matching_person(leadership, CEO_TITLE_KEYWORDS, require_bare_president=True)
                thread_role = "founder_ceo"
                has_primary_contact = person is not None
                if not person:
                    person = _best_matching_person(leadership, SALES_LEADER_TITLE_KEYWORDS)
                    thread_role = "sales_leader"
                if not person:
                    rejection_counts["no_decision_maker"] = rejection_counts.get("no_decision_maker", 0) + 1
                    continue

                score_dict = _score(profile, has_primary_contact, signal_strength, product_fit_categories, active_job_title)
                if score_dict["total_score"] < MIN_QUALIFYING_SCORE:
                    rejection_counts["low_score"] = rejection_counts.get("low_score", 0) + 1
                    continue

                company = Company(
                    batch_id=batch_id,
                    name=profile.get("name", "Unknown"),
                    domain=domain or None,
                    source="jobo",
                    active_job_title=active_job_title,
                    hiring_signal_strength=signal_strength,
                    hiring_signal_reasoning=f"Jobo job search match. GTM leadership present: {gtm_people}",
                    product_fit_jd_categories=product_fit_categories or None,
                    has_outbound_tooling=score_dict["has_outbound_tooling"],
                    has_ai_sdr_tool=score_dict["has_ai_sdr_tool"],
                    team_fit_tier="primary" if gtm_count == 0 else "secondary",
                    team_fit_reasoning=f"{gtm_count} GTM-titled leaders present" if gtm_count else "No existing GTM leadership found",
                )
                db.add(company)
                db.commit()
                db.refresh(company)

                db.add(Score(
                    company_id=company.id,
                    signal_strength=score_dict["need"],
                    total_score=score_dict["total_score"],
                    tier=score_dict["tier"],
                    breakdown=score_dict,
                ))
                db.add(Contact(
                    company_id=company.id,
                    first_name=(person.get("name") or "").split(" ")[0] or None,
                    last_name=" ".join((person.get("name") or "").split(" ")[1:]) or None,
                    title=person.get("title"),
                    linkedin_url=person.get("linkedin_url") or None,
                    thread_role=thread_role,
                    matched_title_reasoning="Jobo leadership match",
                ))
                company.decision_maker_searched_at = datetime.utcnow()
                db.commit()

                excluded_domains.add(domain)
                qualifying_count += 1
                if qualifying_count >= target:
                    break

                try:
                    guard.check()
                except JoboError:
                    budget_stopped_early = True
                    break

            if budget_stopped_early:
                break
            total_pages = data.get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

    return {
        "companies_checked": raw_checked,
        "companies_qualified": qualifying_count,
        "rejection_breakdown": rejection_counts,
        "budget_stopped_early": budget_stopped_early,
        "credits_spent_usd": guard.spent_usd(),
        "last_page": page,
        "api_error": api_error,
    }
