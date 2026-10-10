"""One decision-maker resolution path, used by every play, instead of one per play.

WHY THIS EXISTS (2026-10-07, phase 9 -- "migrate other stages onto the same executor"). Before
this, `icp_filters.py` and `hiring.py` each had their own, separately-written batched HarvestAPI
decision-maker resolver. They drifted:

  - `icp_filters.py`'s was fixed on 2026-09-28/10-04 after a confirmed live bug: batching
    MULTIPLE company LinkedIn URLs together in one `currentCompanies` request silently returns
    ZERO people, even for real, correctly-sized companies with active LinkedIn pages -- while the
    identical request using company NAMES instead returns real matches.
  - `hiring.py`'s `_harvest_decision_makers` was never updated. It still batches by URL. It
    almost certainly has the same 0%-yield problem in production right now, silently, because
    nothing failed loudly -- it just returns no decision makers, the same way icp_filters.py's
    search used to.
  - `hiring.py`'s version also never adopted the OTHER fix made alongside the first one: a
    budget/provider error on a LATER page crashed the whole batch, discarding whatever EARLIER
    pages in the same chunk had already been paid for and found. icp_filters.py stops paging on
    that error and keeps what it already has; hiring.py still does not.

Two real, independently-confirmed bugs, fixed once, both call sites. This is the concrete version
of "one failover path, not four bespoke ones": the next bug found here gets fixed once, for every
play that resolves decision makers, not rediscovered separately in each one later.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.db.models import Company, Contact

logger = logging.getLogger(__name__)

# Name search is a broader match than URL search -- "Klir" also matches "Klir Online", "KLIR Sky,
# Ltd." and similar unrelated companies worldwide in real, live results. Smaller batch, more pages,
# keeps a real match from being buried in that noise within what actually gets checked.
NAME_BATCH_SIZE = 15
NAME_SEARCH_PAGES = 3

# Crustdata's `in`/`not_in` operator silently ignores arrays longer than 5 (its own schema says
# so) -- this is NOT the same batch size as HarvestAPI's NAME_BATCH_SIZE above.
CRUSTDATA_BATCH_SIZE = 5


def _crustdata_person_search_batch(
    db: Session, tenant_id: int, companies: list[Company], titles: list[str], *,
    default_thread_role: str, reasoning_label: str,
    offering_name_for: dict[int, str | None],
) -> dict[int, Contact | None]:
    """TIER 1, added 2026-10-10: crustdata_v3_person_search at $0.002/returned result (verified
    live via the free providers/schema pricing block, 2026-10-09) vs HarvestAPI's flat
    $0.07/page below -- and, found the same day onboarding Jeff Ballard, it surfaces real people
    HarvestAPI's LinkedIn-bucket search missed entirely (Atlan: HarvestAPI returned nobody;
    Crustdata's own index had a Director of Revenue Operations, a Revenue Enablement manager,
    and a VP of GTM Strategy & Revenue Operations). Tried first for every company; whatever it
    can't resolve falls through to the existing HarvestAPI tier, unchanged.

    Title matching uses Crustdata's "(.)" all-words-match operator per title (not an exact
    string), OR'd across every distinct title -- the titles a partner states are themselves
    broad, legitimate buyer roles (Jeff Ballard's ICP wants ANY CMO, not a narrower one), so
    looseness here is intentional; the same LLM judgment HarvestAPI's tier already uses picks
    the real buyer from whoever genuinely comes back, same as that tier.
    """
    from app.deepline_client import DeeplineError, DeeplineSpendBlocked, execute_tool
    from app.gtm_os.plays.hiring import _norm
    from app.phases.decision_maker_reasoning import select_best_decision_makers
    from app.phases.free_decision_maker import _split_name

    out: dict[int, Contact | None] = {}
    seen_titles: set[str] = set()
    title_conditions = []
    for title in titles:
        cleaned = re.sub(r"[/,&]", " ", title)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        key = cleaned.lower()
        if cleaned and key not in seen_titles:
            seen_titles.add(key)
            title_conditions.append({"field": "experience.employment_details.current.title",
                                     "type": "(.)", "value": cleaned})
    if not title_conditions:
        return out

    for start in range(0, len(companies), CRUSTDATA_BATCH_SIZE):
        chunk = [c for c in companies[start:start + CRUSTDATA_BATCH_SIZE] if c.name]
        if not chunk:
            continue
        by_name = {_norm(c.name): c for c in chunk}
        payload = {
            "filters": {
                "op": "and",
                "conditions": [
                    {"field": "experience.employment_details.current.company_name",
                     "type": "in", "value": [c.name for c in chunk]},
                    {"op": "or", "conditions": title_conditions},
                ],
            },
            "limit": 25,
            "fields": ["basic_profile.name", "basic_profile.current_title",
                      "experience.employment_details.current.company_name",
                      "social_handles.professional_network_identifier.profile_url"],
        }
        try:
            response = execute_tool("crustdata_v3_person_search", payload)
        except (DeeplineSpendBlocked, DeeplineError) as e:
            # A tier outage here must never block the fallback -- return what's found so far and
            # let the caller send the rest through HarvestAPI, same discipline as that tier's
            # own budget_stopped handling.
            logger.warning("crustdata person_search tier stopped: %s: %s", type(e).__name__, e)
            break

        profiles = ((response.get("toolResponse") or {}).get("raw") or {}).get("profiles") or []
        people: dict[int, list] = {}
        for p in profiles:
            basic = p.get("basic_profile") or {}
            current = (((p.get("experience") or {}).get("employment_details") or {}).get("current") or [{}])
            company_name = (current[0] if current else {}).get("company_name") or ""
            linkedin_url = (((p.get("social_handles") or {}).get("professional_network_identifier") or {})
                           .get("profile_url"))
            target = by_name.get(_norm(company_name))
            if target is not None and basic.get("name") and linkedin_url:
                people.setdefault(target.id, []).append(
                    {"name": basic["name"], "title": basic.get("current_title"), "linkedin_url": linkedin_url})

        for company in chunk:
            candidates = people.get(company.id) or []
            if not candidates:
                continue
            named = {c["name"]: c for c in candidates}
            picks = select_best_decision_makers(
                db, tenant_id, company, [{"name": n, "title": c["title"]} for n, c in named.items()], 1,
                offering_name=offering_name_for.get(company.id))
            pick = named.get(picks[0]["name"]) if picks else None
            if pick is None:
                continue
            first_name, last_name = _split_name(pick["name"])
            contact = Contact(
                company_id=company.id, first_name=first_name, last_name=last_name,
                title=pick["title"], linkedin_url=pick["linkedin_url"],
                thread_role=picks[0].get("thread_role") or default_thread_role,
                matched_title_reasoning=f"{reasoning_label}; crustdata person_search; "
                                       f"agent: {picks[0].get('reasoning') or ''}"[:1000])
            db.add(contact)
            db.commit()
            out[company.id] = contact

    return out


def resolve_decision_makers_batch(
    db: Session, tenant_id: int, companies: list[Company], titles: list[str], *,
    default_thread_role: str, reasoning_label: str,
    offering_name_for: dict[int, str | None] | None = None,
    batch_size: int = NAME_BATCH_SIZE, search_pages: int = NAME_SEARCH_PAGES,
) -> dict[int, Contact | None]:
    """TIER 1 (crustdata_v3_person_search, cheap, tried for everyone) then TIER 2 (HarvestAPI,
    `batch_size` companies per page at $0.07/page of 25, matched by company NAME -- see module
    docstring for why), with an LLM picking the real buyer from whoever genuinely came back for
    that company in either tier.

    Returns {company.id: Contact or None}. Never raises on a budget refusal or provider error --
    stops paging/chunking at that point and returns whatever was already found and paid for in
    earlier pages/chunks, rather than discarding it.

    `offering_name_for`: optional {company.id: offering_name}, so a caller that already knows a
    qualified offering (hiring.py, from the lead's own qualifier output) can pass it through to
    the same reasoning step icp_filters.py uses with none -- this function does not need to know
    where that mapping came from.

    `default_thread_role`: used only when the agent's own pick carries no thread_role. The
    agent's own choice is always preferred when it supplies one -- a previous version of this
    logic in icp_filters.py ignored the agent's pick and hardcoded a single label regardless,
    which this restores to the richer, already-correct behavior hiring.py had.
    """
    from app import harvestapi
    from app.deepline_client import DeeplineError, DeeplineSpendBlocked
    from app.gtm_os.plays.hiring import _norm
    from app.phases.decision_maker_reasoning import select_best_decision_makers

    offering_name_for = offering_name_for or {}
    out: dict[int, Contact | None] = {}

    try:
        out.update(_crustdata_person_search_batch(
            db, tenant_id, companies, titles, default_thread_role=default_thread_role,
            reasoning_label=reasoning_label, offering_name_for=offering_name_for))
    except Exception as e:  # noqa: BLE001 -- a new tier's bug must never break the existing one
        logger.warning("crustdata person_search tier failed, falling back to HarvestAPI only: %s: %s",
                       type(e).__name__, e)

    companies = [c for c in companies if c.id not in out]
    if not companies:
        return out

    budget_stopped = False

    for start in range(0, len(companies), batch_size):
        if budget_stopped:
            break
        chunk = [c for c in companies[start:start + batch_size] if c.name]
        if not chunk:
            continue
        by_universal = {harvestapi.universal_name(c.linkedin_url): c for c in chunk if c.linkedin_url}
        by_name = {_norm(c.name): c for c in chunk}
        people: dict[int, list] = {}

        for page in range(1, search_pages + 1):
            try:
                found = harvestapi.search_leads(page=page, currentCompanies=",".join(c.name for c in chunk),
                                                currentJobTitles=",".join(titles))
            except (DeeplineSpendBlocked, DeeplineError):
                # A later page failing must not discard earlier pages in THIS chunk, or earlier
                # chunks already processed below -- the exact "buy it, then throw it away"
                # pattern this module exists to stop repeating per play.
                budget_stopped = True
                break
            for person in found:
                target = (by_name.get(_norm(person.get("company_name")))
                         or by_universal.get(harvestapi.universal_name(person.get("company_linkedin_url"))))
                if target is not None and person.get("linkedin_url"):
                    people.setdefault(target.id, []).append(person)
            if len(found) < 25 or all(c.id in people for c in chunk):
                break

        for company in chunk:
            candidates = people.get(company.id) or []
            if not candidates:
                out[company.id] = None
                continue
            named = {f"{p['first_name'] or ''} {p['last_name'] or ''}".strip(): p for p in candidates}
            picks = select_best_decision_makers(
                db, tenant_id, company, [{"name": n, "title": p["title"]} for n, p in named.items()], 1,
                offering_name=offering_name_for.get(company.id))
            pick = named.get(picks[0]["name"]) if picks else None
            if pick is None:
                out[company.id] = None
                continue
            contact = Contact(
                company_id=company.id, first_name=pick["first_name"], last_name=pick["last_name"],
                title=pick["title"], linkedin_url=pick["linkedin_url"],
                thread_role=picks[0].get("thread_role") or default_thread_role,
                matched_title_reasoning=f"{reasoning_label}; agent: {picks[0].get('reasoning') or ''}"[:1000])
            db.add(contact)
            db.commit()
            out[company.id] = contact

    return out
