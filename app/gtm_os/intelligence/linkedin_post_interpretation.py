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

import re

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
#
# 2026-08-22 addition (real-production-sample audit): rhetorical FIRST-PERSON difficulty
# questions -- a real, observed missed shape (e.g. "What's harder right now? Getting the
# enquiry? Getting hold of them?") that "how are other teams"-style phrases don't cover at all.
# Deliberately narrow and specific ("what's harder"/"what's hardest about"/"do you struggle
# with") -- NOT a bare "struggling" keyword match, which would catch third-party advice-giving
# ("if you are a struggling coach...") the same audit confirmed must stay rejected. Each of these
# still requires the SAME trailing "?" the rest of this list already requires.
QUESTION_LEAD_PHRASES = [
    "how are other teams", "how is everyone", "how do you all", "how are you all",
    "any recommendations for", "any suggestions for", "has anyone found",
    "has anyone tried", "what are others using", "what is everyone using",
    "does anyone have", "looking for advice on", "looking for recommendations",
    "what's harder", "whats harder", "what's hardest about", "whats hardest about",
    "do you struggle with", "do you also struggle with",
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


def _sentences(text: str) -> list[str]:
    """Splits a post into sentence/clause-like chunks -- after a real sentence-ending mark
    (. ? !) followed by whitespace. Handles LinkedIn's own common one-idea-per-line-break style
    (each line already ends in its own punctuation) the same as a single-line multi-sentence
    paragraph. A trailing hashtag/mention block with no terminal punctuation becomes its own
    final, punctuation-less chunk -- it can never end in "?", so it's never mistaken for a
    question. Pure regex split -- no keyword list, no scoring, no LLM."""
    return [s.strip() for s in re.split(r"(?<=[.?!])\s+", text) if s.strip()]


def _matched_clause_is_question(text: str, phrase: str) -> bool:
    """Fixes the real bug found live in production signal 126: a genuine matching question
    ("What's harder right now?") was rejected because the OLD check required the entire post to
    end in "?", and something else (a closing remark, a hashtag block -- both extremely common on
    LinkedIn) followed it. Determines whether the actual sentence/clause containing the matched
    phrase itself is a real question, not whether the whole post happens to be. A post having real
    commentary or hashtags after a genuine question no longer invalidates it -- but a phrase
    appearing in a sentence that itself never ends in "?" (declarative, incidental, third-party
    framing) still correctly fails here, exactly as before."""
    for sentence in _sentences(text):
        if phrase in sentence.lower() and sentence.endswith("?"):
            return True
    return False


def _classify_text(text: str) -> dict | None:
    """The actual phrase-matching decision, factored out of interpret_linkedin_post_signal so the
    same, UNCHANGED rules can run either against one post's text (the original behavior) or
    against several posts from the same person concatenated together (2026-08-24 addition, see
    interpret_linkedin_post_signals_grouped below) -- aggregation changes what text the rules see,
    never what the rules themselves mean. Checked in priority order (strongest, most specific
    claim first): text could plausibly match more than one pattern set, and a declared problem
    statement is a stronger claim than an incidental growth mention in the same text."""
    text_lower = text.lower()
    affected_function = _infer_affected_function(text_lower)

    matched = _matches_any(text_lower, DECLARED_PROBLEM_PHRASES)
    if matched:
        return {
            "event_type": "problem_statement", "affected_function": affected_function, "matched_phrase": matched,
            "method": "deterministic:phrase_match:declared_problem",
            "business_change": lambda who: f"{who} appears to describe a difficulty related to {affected_function}.",
        }

    matched = _matches_any(text_lower, QUESTION_LEAD_PHRASES)
    if matched and _matched_clause_is_question(text, matched):
        return {
            "event_type": "solution_question", "affected_function": affected_function, "matched_phrase": matched,
            "method": "deterministic:phrase_match:solution_question",
            "business_change": lambda who: f"{who} appears to be asking how others address a {affected_function}-related challenge.",
        }

    matched = _matches_any(text_lower, ONGOING_SOLUTION_PHRASES)
    if matched:
        return {
            "event_type": "solution_evaluation_mention", "affected_function": affected_function, "matched_phrase": matched,
            "method": "deterministic:phrase_match:solution_evaluation",
            "business_change": lambda who: f"{who} appears to be actively evaluating solutions related to {affected_function}.",
        }

    matched = _matches_any(text_lower, RESOLVED_SOLUTION_PHRASES)
    if matched:
        return {
            "event_type": "solution_adoption_mention", "affected_function": affected_function, "matched_phrase": matched,
            "method": "deterministic:phrase_match:solution_adoption",
            "business_change": lambda who: f"{who} mentions having already adopted a solution related to {affected_function}.",
        }

    matched = _matches_any(text_lower, GROWTH_HIRING_PHRASES)
    if matched:
        return {
            "event_type": "growth_hiring_mention", "affected_function": affected_function, "matched_phrase": matched,
            "method": "deterministic:phrase_match:growth_hiring",
            "business_change": lambda who: f"{who} mentions team growth/hiring related to {affected_function}.",
        }

    return None  # no confident deterministic pattern matched -- not interpreted, not guessed at


def interpret_linkedin_post_signal(signal: GtmSignal) -> InterpretedSignal | None:
    if signal.source != "linkedin_post":
        raise ValueError(f"expected a linkedin_post signal, got {signal.source!r}")

    text = (signal.extracted_info or {}).get("text")
    if not text:
        return None
    classification = _classify_text(text)
    if classification is None:
        return None
    who = signal.person_name_raw or signal.company_name_raw or "This person/company"
    return _build(
        signal, event_type=classification["event_type"], affected_function=classification["affected_function"],
        business_change=classification["business_change"](who), matched_phrase=classification["matched_phrase"],
        text=text, method=classification["method"],
    )


def interpret_linkedin_post_signals_grouped(signals: list[GtmSignal]) -> list[InterpretedSignal]:
    """2026-08-24 addition: multiple posts from the SAME person, taken together, can carry a
    pattern no single one of them shows alone (real example: three separate posts about sales-hire
    turnover, none individually phrase-matching, together an unmistakable repeated-failure theme) --
    see this module's own docstring on why single-post phrase-matching alone can't already catch
    this (it would require real semantic reasoning, explicitly out of scope for this deterministic
    layer). This does NOT add that semantic reasoning. It does the one thing a deterministic layer
    honestly can: if ANY post in the group phrase-matches on its own OR the group's combined text
    phrase-matches, every post in the group becomes real evidence for that same event_type/tier --
    today only the one matching post becomes evidence and the other, clearly-related posts from the
    same person are silently discarded even though problem_detection.py's own multi-evidence
    corroboration mechanism already exists to make use of them. Callers group signals by a caller-
    determined identity key -- this function does not decide who counts as "the same person"."""
    results = []
    if not signals:
        return results
    if len(signals) == 1:
        result = interpret_linkedin_post_signal(signals[0])
        return [result] if result else []

    per_signal_text = {s.id: (s.extracted_info or {}).get("text") for s in signals}
    combined_text = "\n---\n".join(t for t in per_signal_text.values() if t)
    group_classification = _classify_text(combined_text) if combined_text else None

    for signal in signals:
        text = per_signal_text.get(signal.id)
        if not text:
            continue
        individual_classification = _classify_text(text)
        classification = individual_classification or group_classification
        if classification is None:
            continue
        who = signal.person_name_raw or signal.company_name_raw or "This person/company"
        evidence_text = text if individual_classification else combined_text
        method = classification["method"] if individual_classification else f"{classification['method']}:grouped_context"
        results.append(_build(
            signal, event_type=classification["event_type"], affected_function=classification["affected_function"],
            business_change=classification["business_change"](who), matched_phrase=classification["matched_phrase"],
            text=evidence_text, method=method,
        ))
    return results


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
