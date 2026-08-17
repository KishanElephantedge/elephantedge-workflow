"""Deterministic, observation-level interpretation for linkedin_post GtmSignals -- Step 6.

Extends what Step 4 deliberately left out. linkedin_job/theirstack_job interpretation
(interpretation.py) works off a structured field (a job title) with an unambiguous keyword-role
mapping; a LinkedIn post is free text, where the actual failure mode is different: matching a
generic word ("sales", "pipeline", "growth", "AI", "outbound") tells you almost nothing about
whether the post expresses a problem, a question, an announcement, or nothing relevant at all.

So this module does NOT do keyword-presence matching. It matches narrow, explicit PHRASES that
carry the actual claim (e.g. "struggling to", "how are other teams solving", "just hired") --
the same "reuse existing keyword/category logic where appropriate, but do not reuse anything that
makes a buying-signal judgment" instruction Step 6 was given, applied to phrases instead of single
words specifically because single words overclaim here in a way they didn't for job titles.

If a post's text doesn't match one of these narrow, high-precision patterns, NO InterpretedSignal
is created at all -- an unmatched post is not "no problem," it's "not confidently classifiable by
a deterministic rule," and forcing a guess here would be exactly the overclaiming Step 6 asked to
avoid. This is the same "don't force a low-value interpretation" precedent already used for
company_website in Step 4.

**Named, explicit LLM-assisted candidate, per Step 6's "STOP rather than force an unreliable
rule" instruction**: detecting a genuine problem/question/solution-evaluation statement that
ISN'T phrased with one of the fixed trigger phrases below (sarcasm, negation, humble-brag framing,
implicit problem statements) requires real semantic understanding that phrase-matching cannot
reliably provide. That is out of scope here and not silently approximated -- it's a real gap,
left for an explicitly-scoped future LLM-assisted pass, not guessed at with weaker rules.

No LLM calls in this module."""

from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal
from app.gtm_os.intelligence.signal import GtmSignal

# Explicit, narrow phrases that carry a genuine self-reported difficulty -- not single keywords.
# "we're struggling to generate qualified pipeline" matches; "our sales pipeline grew this
# quarter" does not, even though both contain "pipeline".
DECLARED_PROBLEM_PHRASES = [
    "struggling to", "struggling with", "having trouble", "having a hard time",
    "hard time getting", "hard time with", "can't seem to", "cant seem to",
    "still trying to figure out", "our biggest challenge", "biggest challenge is",
    "pain point", "isn't working", "is not working", "not working as well as",
    "keeps falling through the cracks", "can't get it to work",
]

# Requires the phrase AND a trailing "?" -- a narrow subset of questions, specifically ones
# asking how others solve/handle something, not every question mark on LinkedIn.
QUESTION_LEAD_PHRASES = [
    "how are other teams", "how is everyone", "how do you all", "how are you all",
    "any recommendations for", "any suggestions for", "has anyone found",
    "has anyone tried", "what are others using", "what is everyone using",
    "does anyone have", "looking for advice on", "looking for recommendations",
]

# Split into ONGOING vs RESOLVED per Step 7's approved adjustment -- the tense carries real
# meaning, not just phrasing variety:
#
# ONGOING ("evaluating", "comparing", "testing", "piloting") describes an active, currently
# unresolved orientation toward solving something -- strong enough to imply the underlying
# problem/need itself, not just corroborate one already open (Step 7's own worked example:
# "we're evaluating tools for outbound attribution" should be able to establish BOTH the problem
# and the demand from one signal, with no separate earlier problem statement required).
#
# RESOLVED ("just signed up for", "just adopted") describes something already decided/done --
# per the Step 5 design doc's own Example C, past-tense adoption language can mean the need was
# already resolved (possibly by a competitor), so it stays weaker/contextual only, exactly as
# originally designed in Step 6.
ONGOING_SOLUTION_PHRASES = [
    "evaluating vendors for", "evaluating tools for", "evaluating solutions for",
    "evaluating options for", "comparing tools for", "comparing vendors for",
    "comparing solutions for", "we're testing", "we are testing", "piloting",
]

RESOLVED_SOLUTION_PHRASES = [
    "just signed up for", "just started using", "just adopted", "we started using",
]

# Growth/hiring language in free text -- same contextual tier as the structured job-posting
# interpreter, for the same reason: growth alone doesn't establish a gap (Step 5's tightened rule).
GROWTH_HIRING_PHRASES = [
    "just hired", "we've hired", "we hired", "growing our team", "expanding our team",
    "excited to welcome", "adding more", "scaling our team", "grew our team",
]

# Purely descriptive function tagging -- which part of the business this appears to be about.
# Not a buying-signal judgment; only used to fill affected_function once an event_type above has
# already been determined by phrase-matching.
FUNCTION_KEYWORDS = {
    "sales": [
        "sales", "pipeline", "sdr", "sdrs", "bdr", "outbound", "prospecting", "quota",
        "account executive", "cold email", "cold outreach", "closing deals",
    ],
    "marketing": [
        "marketing", "campaign", "campaigns", "demand gen", "content marketing",
        "brand awareness", "seo", "paid ads", "lead generation",
    ],
}


def _infer_affected_function(text_lower: str) -> str:
    for function, keywords in FUNCTION_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return function
    return "unknown"


def _matches_any(text_lower: str, phrases: list[str]) -> str | None:
    for phrase in phrases:
        if phrase in text_lower:
            return phrase
    return None


def interpret_linkedin_post_signal(signal: GtmSignal) -> InterpretedSignal | None:
    """Checked in priority order (strongest, most specific claim first): a post could plausibly
    match more than one pattern set, and a declared problem statement is a stronger claim than an
    incidental growth mention in the same post."""
    if signal.source != "linkedin_post":
        raise ValueError(f"expected a linkedin_post signal, got {signal.source!r}")

    text = (signal.extracted_info or {}).get("text")
    if not text:
        return None
    text_lower = text.lower()
    affected_function = _infer_affected_function(text_lower)
    who = signal.person_name_raw or signal.company_name_raw or "This person/company"

    matched = _matches_any(text_lower, DECLARED_PROBLEM_PHRASES)
    if matched:
        return _build(
            signal, event_type="problem_statement", affected_function=affected_function,
            business_change=f"{who} appears to describe a difficulty related to {affected_function}.",
            matched_phrase=matched, text=text, method="deterministic:phrase_match:declared_problem",
        )

    matched = _matches_any(text_lower, QUESTION_LEAD_PHRASES)
    if matched and text.strip().endswith("?"):
        return _build(
            signal, event_type="solution_question", affected_function=affected_function,
            business_change=f"{who} appears to be asking how others address a {affected_function}-related challenge.",
            matched_phrase=matched, text=text, method="deterministic:phrase_match:solution_question",
        )

    matched = _matches_any(text_lower, ONGOING_SOLUTION_PHRASES)
    if matched:
        return _build(
            signal, event_type="solution_evaluation_mention", affected_function=affected_function,
            business_change=f"{who} appears to be actively evaluating solutions related to {affected_function}.",
            matched_phrase=matched, text=text, method="deterministic:phrase_match:solution_evaluation",
        )

    matched = _matches_any(text_lower, RESOLVED_SOLUTION_PHRASES)
    if matched:
        return _build(
            signal, event_type="solution_adoption_mention", affected_function=affected_function,
            business_change=f"{who} mentions having already adopted a solution related to {affected_function}.",
            matched_phrase=matched, text=text, method="deterministic:phrase_match:solution_adoption",
        )

    matched = _matches_any(text_lower, GROWTH_HIRING_PHRASES)
    if matched:
        return _build(
            signal, event_type="growth_hiring_mention", affected_function=affected_function,
            business_change=f"{who} mentions team growth/hiring related to {affected_function}.",
            matched_phrase=matched, text=text, method="deterministic:phrase_match:growth_hiring",
        )

    return None  # no confident deterministic pattern matched -- not interpreted, not guessed at


def _build(signal: GtmSignal, *, event_type: str, affected_function: str, business_change: str, matched_phrase: str, text: str, method: str) -> InterpretedSignal:
    return InterpretedSignal(
        tenant_id=signal.tenant_id,
        source_signal_id=signal.id,
        event_type=event_type,
        affected_function=affected_function,
        business_change=business_change,
        evidence_excerpt=text,
        extraction_method=method,
        # "medium", not "high": a narrow phrase match is reliable at catching the pattern, but
        # sentence-level nuance (negation, sarcasm, quoting someone else) isn't verified the way
        # an unambiguous job-title keyword match is -- see interpretation.py's role-title matches.
        extraction_confidence="medium",
        company_id=signal.company_id,
        company_name_raw=signal.company_name_raw,
        contact_id=signal.contact_id,
        person_name_raw=signal.person_name_raw,
        observed_at=signal.observed_at,
    )
