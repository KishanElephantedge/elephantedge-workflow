"""Observation-level interpretation for linkedin_post GtmSignals -- Step 6.

Extends what Step 4 deliberately left out. linkedin_job/theirstack_job interpretation
(interpretation.py) works off a structured field (a job title) with an unambiguous keyword-role
mapping; a LinkedIn post is free text, where the actual failure mode is different: matching a
generic word ("sales", "pipeline", "growth", "AI", "outbound") tells you almost nothing about
whether the post expresses a problem, a question, an announcement, or nothing relevant at all.

TWO-TIER matching, in this order (2026-08-24, explicit instruction -- semantic detection was
always the intended design, not an afterthought bolted onto phrase-matching):

1. DETERMINISTIC PHRASE MATCH (free, instant, tried first): narrow, explicit PHRASES that carry
   the actual claim (e.g. "struggling to", "how are other teams solving", "just hired"). Kept as
   the first pass because it's a real, already-verified-precise pattern for the wording it does
   catch -- no reason to spend an LLM call re-deriving what a fixed string match already answers
   correctly.
2. SEMANTIC LLM FALLBACK (only when step 1 finds nothing): the same underlying claim -- a real
   problem, a real question, active evaluation, adoption, growth/hiring -- expressed in ANY
   wording, not just the fixed phrase list. This is what step 1 alone could never do (its own
   prior docstring named this exact gap: "detecting a genuine problem/question/solution-
   evaluation statement that ISN'T phrased with one of the fixed trigger phrases... requires real
   semantic understanding that phrase-matching cannot reliably provide"). The LLM is given the
   SAME finite category definitions phrase-matching already encodes -- it classifies into the
   identical event_type vocabulary (problem_statement / solution_question /
   solution_evaluation_mention / solution_adoption_mention / growth_hiring_mention / none), never
   a new one -- so every evidence-tier/eligibility rule downstream (problem_detection.py,
   demand_detection.py) is completely untouched by this; only what text produces a match changed.
   The LLM must return an exact quoted sentence/clause from the source text as its evidence --
   verified programmatically against the real text before being trusted (see
   _classify_text_semantic) -- a classification whose quote doesn't actually appear in the text is
   discarded, never trusted blind.

If NEITHER tier confidently matches, NO InterpretedSignal is created at all -- an unmatched post
is not "no problem," it's "not confidently classifiable," and forcing a guess here would be
exactly the overclaiming this module has always avoided (same "don't force a low-value
interpretation" precedent as company_website in Step 4)."""

import re

from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal
from app.gtm_os.intelligence.signal import GtmSignal
from app.llm_client import generate_json

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


# Exact same category definitions the deterministic phrases above encode -- given to the LLM
# fallback verbatim so semantic classification can never quietly redefine what these event types
# mean. Order matches priority (strongest/most specific claim first).
_EVENT_TYPE_DEFINITIONS = """- problem_statement: the person self-reports a genuine difficulty/challenge they are currently
  experiencing (not a difficulty they're describing someone else having, not generic industry
  commentary, not advice given TO others).
- solution_question: the person is asking, as a real question (not rhetorical, not third-party
  advice-giving), how others handle/solve/address a specific challenge.
- solution_evaluation_mention: the person describes actively evaluating, comparing, testing, or
  piloting a solution right now -- an unresolved, in-progress search for a fix.
- solution_adoption_mention: the person mentions having already adopted/started using a solution
  -- already decided/done, not in-progress.
- growth_hiring_mention: the person mentions team growth or hiring, with no explicit problem/
  question/evaluation attached.
- none: nothing above applies confidently -- generic commentary, advice given to others,
  storytelling, humor, industry takes with no first-person claim, etc."""


def _classify_text_semantic(text: str, db, tenant_id: int) -> dict | None:
    """Fallback for text the deterministic phrases above don't catch -- the same underlying claim
    expressed in different words. Reuses the existing Gemini-first/Claude-fallback generate_json()
    helper (app/llm_client.py, already used by investigation_generation.py for query formulation)
    -- no new LLM plumbing invented. The LLM is given the EXACT SAME finite category set the
    deterministic phrases already encode (see _EVENT_TYPE_DEFINITIONS) so it can never introduce a
    new event_type/tier meaning; it only decides which of the existing categories, if any, this
    text's actual (possibly differently-worded) claim belongs to.

    Evidence integrity guard: the LLM must return an exact quoted sentence/clause from the source
    text. If that quote doesn't actually appear in the real text (hallucinated/paraphrased), the
    whole classification is discarded -- never trusted blind. This is the same discipline
    investigation_generation.py's _sanitize_formulations already applies to LLM output elsewhere
    in this codebase: verify against real data before trusting, don't take the model's word alone."""
    prompt = f"""You are classifying a single LinkedIn post for a B2B sales-intelligence system.

Category definitions (choose exactly one, "none" if nothing confidently applies):
{_EVENT_TYPE_DEFINITIONS}

Post text:
\"\"\"{text}\"\"\"

Return JSON exactly:
{{"event_type": "problem_statement" | "solution_question" | "solution_evaluation_mention" | "solution_adoption_mention" | "growth_hiring_mention" | "none", "quote": "<the exact sentence/clause from the post text above that supports this classification, verbatim, or empty string if event_type is none>", "affected_function": "sales" | "marketing" | "unknown"}}"""
    try:
        response = generate_json(prompt, db, tenant_id, max_tokens=300)
    except Exception:
        return None  # LLM unavailable -- fail closed to "not classifiable", never a guess

    event_type = response.get("event_type")
    valid_event_types = {
        "problem_statement", "solution_question", "solution_evaluation_mention",
        "solution_adoption_mention", "growth_hiring_mention",
    }
    if event_type not in valid_event_types:
        return None

    quote = (response.get("quote") or "").strip()
    if not quote or quote.lower() not in text.lower():
        return None  # unverifiable/hallucinated evidence -- discarded, not trusted

    affected_function = response.get("affected_function")
    if affected_function not in ("sales", "marketing"):
        affected_function = _infer_affected_function(text.lower())

    business_change_by_type = {
        "problem_statement": lambda who: f"{who} appears to describe a difficulty related to {affected_function}.",
        "solution_question": lambda who: f"{who} appears to be asking how others address a {affected_function}-related challenge.",
        "solution_evaluation_mention": lambda who: f"{who} appears to be actively evaluating solutions related to {affected_function}.",
        "solution_adoption_mention": lambda who: f"{who} mentions having already adopted a solution related to {affected_function}.",
        "growth_hiring_mention": lambda who: f"{who} mentions team growth/hiring related to {affected_function}.",
    }
    return {
        "event_type": event_type, "affected_function": affected_function, "matched_phrase": quote,
        "method": f"llm_semantic:{event_type}",
        "business_change": business_change_by_type[event_type],
    }


def _classify_text(text: str, db=None, tenant_id: int | None = None) -> dict | None:
    """The classification decision, factored out of interpret_linkedin_post_signal so the same
    logic can run either against one post's text (the original behavior) or against several posts
    from the same person concatenated together (2026-08-24 addition, see
    interpret_linkedin_post_signals_grouped below) -- aggregation changes what text is classified,
    never what the categories mean. Tries the deterministic phrase match first (free, instant);
    only falls back to the semantic LLM classifier (see _classify_text_semantic) when db/tenant_id
    are supplied AND no phrase matched -- callers that omit db/tenant_id get phrase-matching only,
    same as this module's original behavior. Checked in priority order (strongest, most specific
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

    if db is not None and tenant_id is not None:
        semantic = _classify_text_semantic(text, db, tenant_id)
        if semantic is not None:
            return semantic

    return None  # no confident pattern matched (deterministic or semantic) -- not guessed at


def interpret_linkedin_post_signal(signal: GtmSignal, db=None, tenant_id: int | None = None) -> InterpretedSignal | None:
    if signal.source != "linkedin_post":
        raise ValueError(f"expected a linkedin_post signal, got {signal.source!r}")

    text = (signal.extracted_info or {}).get("text")
    if not text:
        return None
    classification = _classify_text(text, db=db, tenant_id=tenant_id or signal.tenant_id)
    if classification is None:
        return None
    who = signal.person_name_raw or signal.company_name_raw or "This person/company"
    return _build(
        signal, event_type=classification["event_type"], affected_function=classification["affected_function"],
        business_change=classification["business_change"](who), matched_phrase=classification["matched_phrase"],
        text=text, method=classification["method"],
    )


def interpret_linkedin_post_signals_grouped(signals: list[GtmSignal], db=None, tenant_id: int | None = None) -> list[InterpretedSignal]:
    """2026-08-24 addition: multiple posts from the SAME person, taken together, can carry a
    pattern no single one of them shows alone (real example: three separate posts about sales-hire
    turnover, none individually matching, together an unmistakable repeated-failure theme) -- see
    this module's own docstring on the two-tier (phrase then semantic) matching each individual
    and combined text is run through. If ANY post in the group matches on its own OR the group's
    combined text matches, every post in the group becomes real evidence for that same event_type/
    tier -- today only the one matching post becomes evidence and the other, clearly-related posts
    from the same person are silently discarded even though problem_detection.py's own multi-
    evidence corroboration mechanism already exists to make use of them. Callers group signals by
    a caller-determined identity key -- this function does not decide who counts as "the same
    person"."""
    results = []
    if not signals:
        return results
    tenant_id = tenant_id or signals[0].tenant_id
    if len(signals) == 1:
        result = interpret_linkedin_post_signal(signals[0], db=db, tenant_id=tenant_id)
        return [result] if result else []

    per_signal_text = {s.id: (s.extracted_info or {}).get("text") for s in signals}
    combined_text = "\n---\n".join(t for t in per_signal_text.values() if t)
    group_classification = _classify_text(combined_text, db=db, tenant_id=tenant_id) if combined_text else None

    for signal in signals:
        text = per_signal_text.get(signal.id)
        if not text:
            continue
        individual_classification = _classify_text(text, db=db, tenant_id=tenant_id)
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
