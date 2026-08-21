"""V2-owned deterministic contact ranking -- Phase 3+4 Part C.

Ranks a company's already-discovered Contact rows (from app.phases.decision_maker's
find_decision_makers(), the proven capability both V1 and V2 call -- this module never
searches for contacts itself, never queries a provider, never writes to the database) for a
given Opportunity. Pure, explainable, multi-key ordering -- NEVER a single numeric/AI lead
score, an arbitrary 0-100 value, a guessed seniority number, or an invented probability. Every
ranked contact carries a plain-language `reasoning` string built entirely from real fields.

Ranking keys, in priority order (ties broken by the next key):

1. Function alignment -- does this contact's role tier match the Opportunity's own
   affected_function? Only "sales" currently has a real, provider-confirmed aligned role
   (sales_leader) -- see app/gtm_os/intelligence/linkedin_post_interpretation.py's
   FUNCTION_KEYWORDS, the actual real vocabulary affected_function is drawn from
   ("sales"|"marketing"|"unknown"). No alignment is claimed for "marketing" -- none of the
   three real role tiers this codebase's contact search can return (founder_ceo/sales_leader/
   other_leadership) represents marketing leadership, and inventing one would mean claiming a
   persona the underlying provider doesn't actually support.
2. Role tier -- general decision-making authority, in the same fixed order
   app/phases/decision_maker.py's own three real tiers are searched in: founder_ceo, then
   sales_leader, then other_leadership (the CTO/VP last-resort broader tier).
3. Result quality -- Contact.email_source, an already-real field describing how confidently
   this contact's email was resolved: "deepline" (paid, provider-returned) > "jobo_company"
   (free, generic company-level address) > "pattern_guess" (free, unverified guess) > None (no
   email at all). Not a new field -- reused exactly as decision_maker.py/free_decision_maker.py
   already populate it.
4. Recency -- Contact.created_at, most recently discovered first.
5. Deterministic tie-break -- Contact.id ascending. Never random.
"""
from app.db.models import Contact

# Real, provider-confirmed affected_function -> role_tier alignment. See module docstring for
# why "marketing" has no entry.
FUNCTION_ALIGNED_ROLES: dict[str, set[str]] = {
    "sales": {"sales_leader"},
}

# Mirrors the exact tier search order in app/phases/decision_maker.py::find_decision_makers.
ROLE_TIER_ORDER: dict[str, int] = {"founder_ceo": 0, "sales_leader": 1, "other_leadership": 2}

EMAIL_SOURCE_QUALITY_ORDER: dict[str | None, int] = {"deepline": 0, "jobo_company": 1, "pattern_guess": 2, None: 3}


def _sort_key(contact: Contact, affected_function: str | None):
    aligned_roles = FUNCTION_ALIGNED_ROLES.get(affected_function or "", set())
    function_alignment_rank = 0 if contact.thread_role in aligned_roles else 1
    role_tier_rank = ROLE_TIER_ORDER.get(contact.thread_role, 99)
    quality_rank = EMAIL_SOURCE_QUALITY_ORDER.get(contact.email_source, 3)
    recency_key = -(contact.created_at.timestamp()) if contact.created_at else 0
    return (function_alignment_rank, role_tier_rank, quality_rank, recency_key, contact.id)


def _reasoning(contact: Contact, affected_function: str | None, rank: int) -> str:
    parts = [f"role_tier={contact.thread_role or 'unknown'}"]
    aligned_roles = FUNCTION_ALIGNED_ROLES.get(affected_function or "", set())
    if contact.thread_role in aligned_roles:
        parts.append(f"aligned with this opportunity's affected_function={affected_function!r}")
    parts.append(f"email_source={contact.email_source or 'none'}")
    return f"rank {rank}: " + ", ".join(parts)


def rank_contacts(contacts: list[Contact], affected_function: str | None = None) -> list[dict]:
    """Pure function -- no writes, no re-querying, no provider calls. Returns each contact in
    ranked order with an explainable reasoning string and a positional label (primary /
    secondary / fallback) -- the label is POSITIONAL (1st / 2nd / 3rd+), never a score."""
    ordered = sorted(contacts, key=lambda c: _sort_key(c, affected_function))
    results = []
    for i, contact in enumerate(ordered):
        label = "primary" if i == 0 else "secondary" if i == 1 else "fallback"
        results.append({
            "contact": contact,
            "rank": i + 1,
            "rank_label": label,
            "role_tier": contact.thread_role,
            "reasoning": _reasoning(contact, affected_function, i + 1),
        })
    return results
