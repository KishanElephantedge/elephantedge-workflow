"""LLM-based decision-maker selection -- 2026-09-09, replaces fixed keyword-tier logic for the
free Jobo leadership layer.

WHY THIS EXISTS. free_decision_maker.py used to pick which real leadership candidates to keep
via a fixed tier order (CEO/Founder family first, then a sales-leader keyword list, then a
broader-leadership keyword list), reordered only by one employee-count threshold
(CEO_FIRST_MAX_EMPLOYEES). Real, explicit feedback (2026-09-09): a fixed threshold is still a
human-authored rule standing in for judgment -- it will be wrong for the next edge case exactly
the way "always pick the CEO" was wrong. The system should reason about each real company
individually: who here actually looks like they own the revenue number or are a plausible real
buyer, given their real title and this company's real profile -- not consult a lookup table.

Jobo's free leadership call already returns a company's ENTIRE real leadership list in one free
request (no per-tier cost, unlike the paid search_contact layer in decision_maker.py, which bills
per title-filtered call and can't be restructured this way without a real cost increase -- that
layer still uses _size_ordered_tiers for now). This module is the natural, no-extra-cost place to
apply real reasoning: given the company's profile and its full real candidate list, pick the
genuinely best-fit people, in ranked order, with a reason -- and say "insufficient_signal" for
any candidate that doesn't actually look like a fit, rather than forcing a pick to fill a quota."""

import json

from sqlalchemy.orm import Session

from app.db.models import Company
from app.llm_client import generate_json

SELECTION_PROMPT = """A company posted a job signaling real buying intent for Elephant Edge's
GTM/sales-automation offering. Here is the company's real profile and its REAL, full leadership
list (name + title) -- pick which of these real people are genuinely worth reaching out to as a
decision-maker, and in what priority order. Never invent a person not in this list.

COMPANY:
- Name: {company_name}
- Industry: {industry}
- Employee count: {employee_count}
- Real job posting that triggered this (hiring signal): {active_job_title}

REAL LEADERSHIP CANDIDATES (name, title) -- pick only from this list:
{candidates_json}

Reason about each candidate individually against this company's real size/structure -- do not
apply a fixed rule like "always pick the CEO" or "always pick the VP Sales". A CEO is a strong
buyer signal at a small company but is rarely the real buyer at a large one; a sales/revenue
leader is a strong signal when their title implies real ownership of the number; a candidate
whose title is unrelated to revenue, sales, or company leadership (e.g. engineering, HR, board
member with no operating role) is NOT a fit regardless of seniority.

Return up to {max_contacts} candidates, best fit first. If fewer than {max_contacts} real
candidates actually fit, return fewer -- never pad with a weak fit just to fill the number.

Return strict JSON: {{"selections": [{{"name": "<exact name from the list above>",
"thread_role": "founder_ceo"|"sales_leader"|"other_leadership",
"reasoning": "<1-2 sentences citing this person's real title and why they fit THIS company>"}}]}}"""


def select_best_decision_makers(
    db: Session, tenant_id: int, company: Company, candidates: list[dict], max_contacts: int,
) -> list[dict]:
    """candidates: real leadership entries with at least "name" and "title" (Jobo's own shape).
    Returns up to max_contacts of those SAME candidates, ranked, each with a chosen thread_role
    and reasoning -- never a candidate not present in the input list. Returns [] on any LLM
    failure or when the model finds no genuine fit; callers already treat an empty result as
    "this layer found nothing," the same as before."""
    if not candidates or max_contacts <= 0:
        return []

    named_candidates = [
        {"name": c.get("name"), "title": c.get("title") or "(no title on file)"}
        for c in candidates if c.get("name")
    ]
    if not named_candidates:
        return []

    try:
        result = generate_json(
            SELECTION_PROMPT.format(
                company_name=company.name,
                industry=company.industry or "unknown",
                employee_count=company.employee_count if company.employee_count is not None else "unknown",
                active_job_title=company.active_job_title or "unknown",
                candidates_json=json.dumps(named_candidates, indent=2),
                max_contacts=max_contacts,
            ),
            db, tenant_id, max_tokens=600,
        )
    except Exception:
        return []

    valid_names = {c["name"] for c in named_candidates}
    selections = []
    for sel in (result.get("selections") or [])[:max_contacts]:
        name = sel.get("name")
        if name not in valid_names:
            continue  # never trust an invented/off-list name
        thread_role = sel.get("thread_role")
        if thread_role not in ("founder_ceo", "sales_leader", "other_leadership"):
            thread_role = "other_leadership"
        selections.append({
            "name": name,
            "thread_role": thread_role,
            "reasoning": sel.get("reasoning") or "",
        })
    return selections
