"""LLM offering tiebreak -- 2026-09-07.

icp_offering_matching.py is explicitly deterministic-only ("no LLM, no embeddings, per scope
boundary") and, by its own design, returns best_fit=None whenever more than one offering is a
candidate_match for a company's matched ICP(s) -- "no configured precedence rule exists to pick
one, so none is returned as a definitive best fit." That's honest, not a bug: icp_1 alone feeds
three offerings (Consulting, Workshop, Digital Playbook) with no deterministic field to tell them
apart (target_problem_types/qualification_signals/exclusions are all empty for these three in
offering_config.py). Left unresolved, this produces the exact symptom already found once before
in production (batch 92, Sept 3): every tied company silently fell through to whichever offering
happened to be listed first, so all 9 real opportunities that day were tagged "Consulting"
regardless of actual fit.

This module is the explicit, separate, additive step that breaks that tie using real signal --
kept OUT of icp_offering_matching.py so that module's own deterministic guarantee stays true for
whatever else relies on it. Only called when match_offerings_for_company() already found more
than one genuine candidate_match; never invoked to invent a match icp_offering_matching.py itself
would call unconfigured or excluded.

WHAT SIGNAL IT ACTUALLY HAS TO GO ON. Company.industry, employee_count, location, and the real
job title that triggered the ICP match (active_job_title) -- plus each candidate offering's own
configured description/positioning_messaging/target_company_characteristics from
offering_config.py. Workshop's own config says its audience is "India SMB founders" -- a real,
usable signal (a US-located company is real evidence against Workshop, not proof either way for
Consulting vs Digital Playbook). The LLM is instructed to use ONLY what it is given, name which
signal(s) drove its answer, and say "insufficient signal" rather than guess when nothing in the
prompt actually discriminates -- same "missing is fine, guessing is not" discipline the rest of
this codebase's ICP work already follows (icp_matching.py's own revenue/sales-team proxies always
label themselves as derived, never presented as fact)."""

from sqlalchemy.orm import Session

from app.db.models import Company
from app.gtm_os.icp.icp_offering_matching import match_offerings_for_company
from app.gtm_os.opportunity.offering_config import get_offering_config
from app.llm_client import generate_json

TIEBREAK_PROMPT = """A company matched more than one of our offerings equally well on ICP fit
alone. Pick the SINGLE best-fit offering using only the real information below -- never invent a
fact not given here.

COMPANY:
- Name: {name}
- Industry: {industry}
- Employee count: {employee_count}
- Location: {location}
- Job posting that triggered this match: {active_job_title}

CANDIDATE OFFERINGS (pick exactly one of these names):
{offerings_block}

Rules:
- Base your choice on real signal above matching a candidate's own description/audience/
  positioning -- e.g. a stated target audience the company's location does or doesn't fit.
- If nothing here actually discriminates between the candidates, say so honestly rather than
  guessing -- pick the offering explicitly described as the general/default case if one exists,
  and set confidence to "insufficient_signal".
- Never pick a name that is not in the candidate list above.
- The "offering" field must be EXACTLY one of the candidate names as written above (e.g.
  "Consulting"), with nothing else appended -- not "Consulting: Sales Consulting.", not a
  sentence, just the bare name.

Return JSON exactly:
{{"offering": "<the bare candidate name, exactly as listed, nothing appended>",
  "reasoning": "<1-2 sentences citing the specific signal(s) used>",
  "confidence": "signal_based" | "insufficient_signal"}}"""


def break_offering_tie(db: Session, tenant_id: int, company_id: int) -> dict:
    """Runs match_offerings_for_company() unmodified; if it already resolved to a single
    best_fit (0 or 1 candidates), returns that untouched -- this function only ever fires on a
    genuine tie, never overrides a real deterministic answer. Returns the same shape
    match_offerings_for_company() does, plus `tiebreak` (None if untouched, else the LLM's
    reasoning) so a caller can always see whether a result was decided deterministically or
    resolved by this step."""
    result = match_offerings_for_company(db, tenant_id, company_id)
    if result.get("best_fit") is not None or not result.get("icp_matches"):
        _persist_resolved_offering(db, company_id, result.get("best_fit"))
        return {**result, "tiebreak": None}

    candidate_names = sorted({
        o["offering"]
        for icp_match in result["icp_matches"]
        for o in icp_match["offerings"]
        if o["status"] == "candidate_match"
    })
    if len(candidate_names) < 2:
        return {**result, "tiebreak": None}  # 0 or 1 real candidates -- nothing to break

    company = db.get(Company, company_id)
    if company is None:
        return {**result, "tiebreak": None}

    offering_config_by_name = {o["name"]: o for o in get_offering_config(db, tenant_id)}
    offerings_block = "\n".join(
        f"- {name}: {offering_config_by_name[name].get('description') or '(no description on file)'}"
        f" | audience: {offering_config_by_name[name].get('target_company_characteristics') or 'not specified'}"
        f" | positioning: {offering_config_by_name[name].get('positioning_messaging') or 'not specified'}"
        for name in candidate_names if name in offering_config_by_name
    )
    prompt = TIEBREAK_PROMPT.format(
        name=company.name, industry=company.industry or "unknown",
        employee_count=company.employee_count or "unknown", location=company.location or "unknown",
        active_job_title=company.active_job_title or "unknown",
        offerings_block=offerings_block,
    )
    try:
        decision = generate_json(prompt, db, tenant_id, max_tokens=400)
    except Exception as e:  # noqa: BLE001 -- a tiebreak failure must not block the rest of a batch
        return {**result, "tiebreak": {"status": "failed", "error": str(e)}}

    chosen = decision.get("offering")
    if chosen not in candidate_names:
        # Tolerate the model echoing "Name: description" despite the prompt's instruction not
        # to -- take the part before the first colon and re-check, rather than discarding a
        # response whose actual pick is unambiguous. Still hard-fails if that doesn't resolve to
        # a real candidate; never lets a genuinely off-list/hallucinated name through.
        prefix = (chosen or "").split(":", 1)[0].strip()
        if prefix in candidate_names:
            chosen = prefix
        else:
            return {**result, "tiebreak": {"status": "invalid_choice", "raw": decision, "candidates": candidate_names}}

    _persist_resolved_offering(db, company_id, {"offering": chosen})
    return {
        **result,
        "best_fit": {"offering": chosen},
        "best_fit_reason": f"LLM tiebreak among {candidate_names}: {decision.get('reasoning')}",
        "tiebreak": {"status": "resolved", "confidence": decision.get("confidence"), "reasoning": decision.get("reasoning"), "candidates": candidate_names},
    }


def _persist_resolved_offering(db: Session, company_id: int, best_fit: dict | None) -> None:
    """Writes the resolved offering onto Company.resolved_offering_name so
    run_campaign_execution() can route this company's contact by its OWN offering match instead of
    a single Batch-level field -- see that column's own comment in models.py for the real bug this
    closes. A None best_fit (no candidate offering at all) is left alone rather than overwritten
    with null: a real prior resolution must never be erased by a later call that happens to find
    nothing, e.g. after a config change temporarily removes every candidate."""
    if not best_fit or not best_fit.get("offering"):
        return
    company = db.get(Company, company_id)
    if company is None:
        return
    company.resolved_offering_name = best_fit["offering"]
    db.commit()
