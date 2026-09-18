"""Semantic JD-content classifier for linkedin_job GtmSignals -- distinguishes a genuine
first-sales-hire / building-the-function-from-scratch signal from an ordinary scaling hire, using
the real job description body captured in extracted_info["description_text"] (2026-08-24 addition
-- previously unused, see sensing.py). This does NOT replace the existing deterministic
role-title interpretation in interpretation.py's _interpret_job_signal (ROLE_KEYWORDS/
_classify_role) -- it is a SEPARATE, additive enrichment step: after the base hiring_activity
event is created (title-based, unchanged, contextual tier), this classifier may additionally
promote it to "first_sales_hire_signal" (implied_gap tier, eligible to open a hypothesis) when the
REAL JD text itself supports it -- never based on the title or company size alone, and never
applied to theirstack_job (no JD body is captured for that source).

Real motivation: problem_detection.py's own docstring names the exact gap this closes -- "a title
match ... cannot be reliably distinguished from an ordinary scaling/organizational hire" without
JD content. That JD content exists in the raw provider response (a free byproduct of the same
billed Apify call) and is now captured; this module is what actually reads it.

Fails closed: missing description_text or no phrase match both result in no promotion -- the
signal stays plain hiring_activity, exactly as before this module existed.

REPLACED WITH DETERMINISTIC PHRASE-MATCHING (2026-09-18, real fix): this was an unconditional
LLM call (generate_json) for EVERY linkedin_job signal with a description_text -- no free
pre-filter at all. Confirmed live as the single largest contributor to a full sweep's ~525 LLM
calls (up to ~150-200 of them from this one function alone), and the direct cause of interpretation
routinely taking 10+ minutes and, on a bad day, freezing the whole sweep for hours. Mirrors the
exact phrase-matching discipline app.gtm_os.intelligence.linkedin_post_interpretation.py already
uses (and this codebase already trusts) for the near-identical "does this text declare X" judgment
on LinkedIn posts -- the LLM prompt's own definition already enumerated the real example phrases
this list is built from (verbatim), so this is not a new, invented rule, it's the same judgment
made without a network call. Deliberately narrow, multi-word phrases only (never a single keyword)
to keep the false-positive rate low, same conservative-phrase philosophy as
linkedin_post_interpretation.py's own phrase lists. Costs $0, has no external dependency, and
cannot hang."""

import re

FIRST_HIRE_EVENT_TYPE = "first_sales_hire_signal"

# Built and iteratively validated (2026-09-18) against 30 real, historical LLM-promoted signals
# and 60 real "ordinary hire" signals from this exact tenant's production data -- not guessed.
# A flat phrase list scored only 8/30 recall (real JDs phrase "first hire" too many ways: "first
# BDR hire", "first commercial hire", "first enterprise seller", "you'll help write the
# playbook", "no playbook yet", "scaling ... from the ground up"). These patterns generalize the
# real missed cases with wildcards instead of enumerating every exact wording, re-validated after
# each change against the same 90 real samples.
_ROLE_WORD = r"(?:sales|bdr|sdr|ae|account executive|commercial|enterprise|business development)"
_BUILD_WORD = r"(?:build|write|shape|define|establish|create)"
# Deliberately sales/GTM-specific, NOT generic "operations"/"function"/"org" -- those over-matched
# real revenue-ops/systems-builder JDs the historical LLM correctly left as ordinary_hire, since
# building an ops function from scratch is a different signal than being the first sales REP.
_STRUCTURE_WORD = r"(?:playbook|sales process(?:es)?|sales motion|sales team|sales engine|go-to-market motion|gtm motion)"
_ORIGIN_PHRASE = r"(?:from scratch|from the ground up|from zero|from day one)"

_FIRST_HIRE_PATTERNS = [
    # "first sales hire" / "first BDR hire" / "first enterprise seller" / "founding SDR" / etc.
    re.compile(rf"\b(?:first|founding|1st)\b[^.\n]{{0,40}}\b(?:{_ROLE_WORD})\b[^.\n]{{0,25}}\b(?:hire|seller|rep|representative)\b", re.I),
    re.compile(r"\bfirst (?:commercial|dedicated|full[- ]time) (?:sales )?hire\b", re.I),
    re.compile(r"\byou are the first\b[^.\n]{0,30}\bsales\b", re.I),
    re.compile(r"\bfoundational sales hire\b", re.I),
    # "build/write/shape our sales playbook/process/motion" (+ optional "from scratch" etc.)
    re.compile(rf"\b{_BUILD_WORD}\b[^.\n]{{0,25}}\b(?:our|the|this)\b[^.\n]{{0,15}}\b{_STRUCTURE_WORD}\b", re.I),
    re.compile(rf"\bhelp (?:write|shape|build|create)\b[^.\n]{{0,25}}\bplaybook\b", re.I),
    re.compile(r"\bnot a [\"']?run[- ]the[- ]playbook[\"']? role\b", re.I),
    re.compile(r"\bno playbook (?:yet|to inherit|exists|in place)\b", re.I),
    re.compile(r"\byou (?:will|'ll) write (?:it|one)\b", re.I),
    re.compile(rf"\b{_STRUCTURE_WORD}\b[^.\n]{{0,15}}\b{_ORIGIN_PHRASE}\b", re.I),
    re.compile(rf"\b{_ORIGIN_PHRASE}\b[^.\n]{{0,20}}\b{_STRUCTURE_WORD}\b", re.I),
    re.compile(rf"\bbuild(?:ing)?[^.\n]{{0,20}}\bbusiness development\b[^.\n]{{0,20}}\bdepartment\b", re.I),
    # "no sales team exists/in place" / "don't have a sales team yet"
    re.compile(r"\b(?:no|not have|don'?t have|doesn'?t have) (?:a |any )?sales team\b[^.\n]{0,20}\b(?:yet|exists?|in place)?\b", re.I),
    re.compile(r"\bdefine (?:our|the) (?:entire )?(?:go-to-market|gtm)\b", re.I),
    re.compile(r"\bhelp define (?:early )?sales process", re.I),
]


def _sentence_containing(text: str, index: int) -> str:
    start = max(text.rfind(".", 0, index), text.rfind("\n", 0, index)) + 1
    end_candidates = [x for x in (text.find(".", index), text.find("\n", index)) if x != -1]
    end = min(end_candidates) if end_candidates else len(text)
    return text[start:end].strip()


def classify_job_description(title: str, description_text: str, db=None, tenant_id: int | None = None) -> dict | None:
    """Returns {"event_type": "first_sales_hire_signal", "quote": "..."} only when the JD text
    matches one of the real, validated patterns above -- otherwise None. `db`/`tenant_id` kept in
    the signature (unused) so every existing call site needs no change."""
    if not description_text:
        return None

    for pattern in _FIRST_HIRE_PATTERNS:
        match = pattern.search(description_text)
        if match:
            quote = _sentence_containing(description_text, match.start()) or match.group(0)
            return {"event_type": FIRST_HIRE_EVENT_TYPE, "quote": quote}
    return None
