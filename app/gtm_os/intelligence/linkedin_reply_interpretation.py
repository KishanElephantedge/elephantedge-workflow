"""Deterministic, observation-level interpretation for linkedin_reply GtmSignals -- Step 11B.

Extends the phrase-matching philosophy already established for LinkedIn posts
(linkedin_post_interpretation.py) to real prospect replies from SalesRobot conversations
(sensed in Step 11A). Same discipline: narrow, explicit PHRASES, not keyword-presence matching;
no LLM; return None rather than force a guess when nothing confidently matches.

**Reuses existing event_type names wherever the underlying meaning is identical to what the
post interpreter already produces** ("problem_statement", "solution_question",
"solution_evaluation_mention") -- these automatically inherit the SAME Problem/Demand tier
treatment already registered in problem_detection.py/demand_detection.py, with zero new tier-map
entries needed. Two genuinely NEW event types this source introduces ("pricing_request",
"demo_request") have no post-interpreter equivalent -- these are registered as new entries in
demand_detection.DEMAND_EVENT_TYPE_TIERS (both "buying_intent_adjacent", same tier as
solution_evaluation_mention -- "stronger Demand evidence" per the Step 11B spec, not a new
BuyingIntent object). Two more new event types ("not_interested", "timing_deferral") are
DELIBERATELY left unregistered in either tier map -- they are not evidence FOR a problem or
demand at all; a future sweep/orchestrator (not built here) is the right place to decide whether
to link them as `role="contradicting"` against an already-open hypothesis.

**Evidence vs. context (Step 11B's own required distinction)**: only the prospect's own reply
text is ever treated as evidence (goes into `evidence_excerpt`, drives classification). The
preceding message (mostly our own outbound send) is read ONLY to judge whether a short,
otherwise-ambiguous reply is confirming something meaningful -- it is never itself evidence, and
is never separately turned into an InterpretedSignal. Traceability to that context is preserved
structurally, not via a new field: the source GtmSignal.extracted_info (Step 11A) already stores
previous_message_text/direction/sent_time, reachable via source_signal_id.

**Named, explicit LLM-assisted candidates (Step 11B's own instruction, not implemented)**:
sarcasm, negation ("not really a problem"), indirect/implicit problem statements phrased without
one of the fixed trigger phrases below, complex multi-sentence replies mixing several intents,
ambiguous short responses with no problem-probing context to lean on, and nuanced objections
("we tried something like this before and it didn't work") -- none of these are reliably
resolvable by phrase-matching, and none are silently approximated here.

No LLM calls in this module."""

from app.gtm_os.intelligence.interpreted_signal import InterpretedSignal
from app.gtm_os.intelligence.linkedin_post_interpretation import _infer_affected_function
from app.gtm_os.intelligence.signal import GtmSignal

# --- Self-sufficient reply-text patterns (classifiable without needing the preceding message) ---

# Broader than the post interpreter's own list -- replies are more casual/conversational than
# public posts ("one of our biggest problems" vs. a post's "our biggest challenge"). Same
# discipline (explicit phrases, not bag-of-words), just tuned for reply phrasing.
DECLARED_PROBLEM_PHRASES = [
    "struggling to", "struggling with", "having trouble", "having a hard time",
    "hard time getting", "hard time with", "can't seem to", "cant seem to",
    "still trying to figure out", "biggest challenge", "biggest problem",
    "major issue", "big issue", "real challenge", "pain point", "isn't working",
    "is not working", "not working as well as", "keeps falling through the cracks",
    "can't get it to work",
]

QUESTION_LEAD_PHRASES = [
    "how are other teams", "how is everyone", "how do you all", "how are you all",
    "any recommendations for", "any suggestions for", "has anyone found",
    "has anyone tried", "what are others using", "what is everyone using",
    "does anyone have", "looking for advice on", "looking for recommendations",
    "how are others solving", "how is everyone solving", "how are you solving",
]

ONGOING_EVALUATION_PHRASES = [
    "evaluating a few tools", "evaluating some tools", "evaluating a couple of tools",
    "evaluating tools for", "evaluating vendors for", "evaluating solutions for",
    "evaluating options for", "comparing a few tools", "comparing tools for",
    "comparing vendors for", "comparing solutions for", "currently evaluating",
    "we're testing", "we are testing", "piloting",
]

# Genuinely new categories -- no equivalent in the post interpreter (a public post rarely
# contains a direct pricing/demo ask the way a reply to real outreach does).
PRICING_REQUEST_PHRASES = [
    "send me pricing", "send pricing", "share pricing", "what's the pricing",
    "what is the pricing", "can you send over pricing", "send over the pricing",
    "know the pricing", "get pricing",
]

DEMO_REQUEST_PHRASES = [
    "schedule a demo", "book a demo", "set up a demo", "see a demo",
    "can we do a demo", "arrange a demo", "get a demo",
]

# Negative / non-evidence -- explicit, not treated as Problem or Demand (see module docstring
# for why these stay unregistered in both tier maps).
NOT_INTERESTED_PHRASES = [
    "not interested", "no thanks", "not for us", "we're all set", "we are all set",
    "pass on this",
]

TIMING_DEFERRAL_PHRASES = [
    "not now", "maybe later", "maybe next quarter", "not right now",
    "reach out later", "circle back later", "check back later", "some other time",
]

# --- Context-only patterns (never evidence on their own; used to judge whether a SHORT reply is
# confirming something meaningful in the preceding OUTBOUND message) ---

PROBLEM_PROBING_QUESTION_PHRASES = [
    "struggling with", "facing challenges", "facing any challenges",
    "having trouble with", "any challenges with", "pain point", "biggest challenge",
    "hard time with", "difficulty with", "facing issues",
]

SHORT_AFFIRMATIVE_PHRASES = [
    "yes", "yes,", "yeah", "yep", "definitely", "absolutely", "exactly",
    "that's right", "thats right", "correct", "for sure",
]
SHORT_AFFIRMATIVE_MAX_LEN = 40  # keeps a long, substantive reply that happens to START with "yes" from being treated as a bare affirmative


def _matches_any(text_lower: str, phrases: list[str]) -> str | None:
    for phrase in phrases:
        if phrase in text_lower:
            return phrase
    return None


def _is_short_affirmative(text: str) -> bool:
    if len(text) > SHORT_AFFIRMATIVE_MAX_LEN:
        return False
    stripped = text.strip().lower()
    return any(stripped.startswith(p) for p in SHORT_AFFIRMATIVE_PHRASES)


def _previous_message_is_problem_probing(previous_text: str | None, previous_direction: str | None) -> bool:
    """Only OUR OWN outbound question can make a bare affirmative meaningful -- if the prospect
    themselves sent the previous message (two inbound in a row), there's no "our question" to
    confirm, so this always returns False in that case."""
    if previous_direction != "outbound" or not previous_text:
        return False
    lowered = previous_text.lower().strip()
    return lowered.endswith("?") and any(phrase in lowered for phrase in PROBLEM_PROBING_QUESTION_PHRASES)


def interpret_linkedin_reply_signal(signal: GtmSignal) -> InterpretedSignal | None:
    """Checked in priority order: strongest/most specific self-sufficient patterns first
    (pricing/demo requests, then declared problem language, then questions/evaluation
    language, then explicit negative/deferral language), and only as a last resort the
    context-dependent short-affirmative check -- deliberately the weakest, lowest-priority path,
    since a bare "yes" is the most linguistically ambiguous thing a reply can contain."""
    if signal.source != "linkedin_reply":
        raise ValueError(f"expected a linkedin_reply signal, got {signal.source!r}")

    extracted = signal.extracted_info or {}
    text = extracted.get("text")
    if not text:
        # A reply we know happened but whose TEXT is unavailable (2026-08-31). SalesRobot's
        # campaign endpoint reports lastActivity == "REPLIED" reliably, while its synced inbox
        # cannot be matched back to most of those prospects at all -- see sensing.py's own note
        # on the two non-overlapping LinkedIn identifier encodings.
        #
        # Returning None here is what silently discarded every such reply, and with it the entire
        # outcome/learning loop: real people replied, and sales_outcomes stayed at zero. The fact
        # of a reply is a genuine, verified outcome on its own -- it just cannot be classified as
        # positive/negative without words. So it is recorded as a plain reply and left to map to
        # the generic "reply" outcome category, never guessed at.
        if extracted.get("connection_evidence"):
            return InterpretedSignal(
                tenant_id=signal.tenant_id, source_signal_id=signal.id,
                event_type="connection_accepted",
                affected_function="sales",
                business_change=f"{signal.person_name_raw or 'The prospect'} accepted the connection request (no reply yet).",
                evidence_excerpt=str(extracted.get("connection_evidence")),
                extraction_method="deterministic:campaign_connection_flag",
                extraction_confidence="low",
                company_id=signal.company_id, company_name_raw=signal.company_name_raw,
                contact_id=signal.contact_id, person_name_raw=signal.person_name_raw,
                observed_at=signal.observed_at,
            )
        if not extracted.get("reply_evidence"):
            return None
        # SalesRobot's own tag is a REAL classification of the reply, not our inference -- so a
        # textless reply can still be categorised honestly instead of collapsing to generic.
        tags = {str(t).strip().lower() for t in (extracted.get("tag_list") or [])}
        if "meeting request" in tags:
            reply_event_type = "meeting_requested"
        elif "interested" in tags:
            reply_event_type = "interested_reply"
        elif "not interested" in tags:
            reply_event_type = "not_interested"
        else:
            reply_event_type = "reply_received"
        return InterpretedSignal(
            tenant_id=signal.tenant_id,
            source_signal_id=signal.id,
            event_type=reply_event_type,
            affected_function="sales",
            business_change=(
                f"{signal.person_name_raw or 'The prospect'} replied to outreach"
                + (f" -- tagged {', '.join(sorted(tags))} in the campaign" if tags else " (message text not retrievable)")
                + "."
            ),
            evidence_excerpt=str(extracted.get("reply_evidence")),
            extraction_method="deterministic:campaign_reply_flag",
            # "low", not medium: we know THAT they replied, nothing about what they said.
            extraction_confidence="low",
            company_id=signal.company_id,
            company_name_raw=signal.company_name_raw,
            contact_id=signal.contact_id,
            person_name_raw=signal.person_name_raw,
            observed_at=signal.observed_at,
        )
    text_lower = text.lower()
    previous_text = extracted.get("previous_message_text")
    previous_direction = extracted.get("previous_message_direction")
    # The reply itself often doesn't name a function explicitly ("yes, that's one of our
    # biggest problems") -- falling back to the preceding message's own topic ("...outbound
    # pipeline?") is still purely descriptive (which function this is ABOUT), not a
    # problem/demand judgment, so it's safe to use context here even though event_type
    # classification itself never reads the previous message except via the one narrow
    # affirmative-confirmation rule below.
    affected_function = _infer_affected_function(text_lower)
    if affected_function == "unknown" and previous_text:
        affected_function = _infer_affected_function(previous_text.lower())
    who = signal.person_name_raw or signal.company_name_raw or "This person/company"

    matched = _matches_any(text_lower, PRICING_REQUEST_PHRASES)
    if matched:
        return _build(
            signal, event_type="pricing_request", affected_function=affected_function,
            business_change=f"{who} explicitly asked for pricing information related to {affected_function}.",
            evidence_excerpt=text, method="deterministic:phrase_match:pricing_request", confidence="medium",
        )

    matched = _matches_any(text_lower, DEMO_REQUEST_PHRASES)
    if matched:
        return _build(
            signal, event_type="demo_request", affected_function=affected_function,
            business_change=f"{who} explicitly asked to schedule a demo related to {affected_function}.",
            evidence_excerpt=text, method="deterministic:phrase_match:demo_request", confidence="medium",
        )

    matched = _matches_any(text_lower, DECLARED_PROBLEM_PHRASES)
    if matched:
        return _build(
            signal, event_type="problem_statement", affected_function=affected_function,
            business_change=f"{who} appears to describe a difficulty related to {affected_function}.",
            evidence_excerpt=text, method="deterministic:phrase_match:declared_problem", confidence="medium",
        )

    matched = _matches_any(text_lower, QUESTION_LEAD_PHRASES)
    if matched and text.strip().endswith("?"):
        return _build(
            signal, event_type="solution_question", affected_function=affected_function,
            business_change=f"{who} appears to be asking how others address a {affected_function}-related challenge.",
            evidence_excerpt=text, method="deterministic:phrase_match:solution_question", confidence="medium",
        )

    matched = _matches_any(text_lower, ONGOING_EVALUATION_PHRASES)
    if matched:
        return _build(
            signal, event_type="solution_evaluation_mention", affected_function=affected_function,
            business_change=f"{who} appears to be actively evaluating solutions related to {affected_function}.",
            evidence_excerpt=text, method="deterministic:phrase_match:solution_evaluation", confidence="medium",
        )

    matched = _matches_any(text_lower, NOT_INTERESTED_PHRASES)
    if matched:
        return _build(
            signal, event_type="not_interested", affected_function=affected_function,
            business_change=f"{who} explicitly indicated they are not interested (context: {affected_function} outreach).",
            evidence_excerpt=text, method="deterministic:phrase_match:not_interested", confidence="medium",
        )

    matched = _matches_any(text_lower, TIMING_DEFERRAL_PHRASES)
    if matched:
        return _build(
            signal, event_type="timing_deferral", affected_function=affected_function,
            business_change=f"{who} indicated the timing isn't right now, without declining outright (context: {affected_function} outreach).",
            evidence_excerpt=text, method="deterministic:phrase_match:timing_deferral", confidence="medium",
        )

    # Context-dependent fallback -- the ONLY rule here that reads the preceding message to decide
    # meaning. A bare affirmative on its own is never evidence; it only becomes a problem
    # confirmation if OUR prior message was itself a genuine problem-probing question.
    if _is_short_affirmative(text) and _previous_message_is_problem_probing(previous_text, previous_direction):
        preview = (previous_text or "")[:80]
        return _build(
            signal, event_type="problem_statement", affected_function=affected_function,
            business_change=f"{who} appears to confirm a {affected_function}-related difficulty raised in our own prior message ({preview!r}).",
            evidence_excerpt=text, method="deterministic:context_confirmed_affirmative",
            # Lower than a self-sufficient phrase match -- a bare "yes" is linguistically weaker
            # evidence than an explicit statement, even when the context makes it meaningful.
            confidence="low",
        )

    return None  # no confident deterministic pattern matched -- not interpreted, not guessed at


def _build(signal: GtmSignal, *, event_type: str, affected_function: str, business_change: str, evidence_excerpt: str, method: str, confidence: str) -> InterpretedSignal:
    return InterpretedSignal(
        tenant_id=signal.tenant_id,
        source_signal_id=signal.id,
        event_type=event_type,
        affected_function=affected_function,
        business_change=business_change,
        evidence_excerpt=evidence_excerpt,
        extraction_method=method,
        # "medium"/"low", never "high" -- text-based phrase matching (even the most explicit
        # phrase) is never as unambiguous as a structured-field match like a job title
        # (interpretation.py's own distinction, carried forward here).
        extraction_confidence=confidence,
        company_id=signal.company_id,
        company_name_raw=signal.company_name_raw,
        contact_id=signal.contact_id,
        person_name_raw=signal.person_name_raw,
        observed_at=signal.observed_at,
    )
