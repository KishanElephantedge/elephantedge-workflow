"""Deterministic intent classifier for LinkedIn post ENGAGEMENT (comments) -- decides which
commenters on a broadly-matched post are worth treating as real leads versus noise.

WHY THIS EXISTS. app/gtm_os/intelligence/engagement.py finds LinkedIn posts by broad phrase
search (reusing linkedin_search_config.py's existing mechanism, unmodified) and harvests every
commenter via a real, pre-enriched Apify actor (LinkedIn Post Engagement Scraper). Most comments
on a broadly-matched post are noise -- a single word ("Editor"), an emoji, a reply to someone
else's reply -- and nothing upstream of this module filters that out; a phrase search finds the
POST, not the comment. This is the step that reads the actual comment TEXT and decides which
ones say something a real prospect would say.

DELIBERATELY DETERMINISTIC, not an LLM call -- same discipline as linkedin_job_interpretation.py
and linkedin_post_interpretation.py, and for the same reason: this runs once per commenter
returned by the actor, potentially hundreds per sweep tick, and an unconditional LLM call at that
volume is exactly the pattern that produced ~525 calls/sweep and the multi-hour stalls this
project spent days fixing (see llm_budget.py). $0, no external dependency, cannot hang.

HONESTY ABOUT CALIBRATION. Unlike linkedin_job_interpretation.py's FIRST_HIRE patterns (built and
validated against 90 real historical labeled samples from this tenant's own production data),
this is a FIRST CUT -- there is no historical engagement-mining data yet to validate against,
because this source did not exist before now. The phrase list below is deliberately conservative
(narrow, multi-word, low-false-positive-risk patterns) rather than broad, on the same reasoning
linkedin_job_interpretation.py's own docstring states: a missed real lead is not lost (the raw
comment is still persisted as a signal either way -- see engagement.py, "never discard purchased
data"), it just does not get the priority a matched one does. This module MUST be re-validated
against real production comments once signals start flowing, the same way the job-signal
classifier was -- flagged here deliberately so that revalidation is not forgotten."""

import re

# Three real intent shapes a genuine prospect's comment can take, matched independently so the
# category is preserved for downstream reporting/tuning (not just a bare yes/no).
CATEGORY_DIRECT_INTEREST = "direct_interest"      # wants what's being offered, right now
CATEGORY_ATTENDANCE = "attendance_signal"         # was at / is going to a webinar or event
CATEGORY_PAIN = "pain_signal"                     # is describing the exact problem, unprompted

# Direct interest: asking for / claiming access to what's being offered. Deliberately excludes
# bare single words ("Editor", "Interested") on their own -- those ARE common on lead-magnet
# posts (see the real Eric Siu example this feature was built from) and genuinely mean interest
# there, but the same bare word appears on completely unrelated posts too with no signal value.
# Requiring a short surrounding phrase, not a bare keyword, is the same low-false-positive-risk
# choice linkedin_job_interpretation.py's own patterns make.
_DIRECT_INTEREST_PATTERNS = [
    re.compile(r"\b(?:would love|'d love) (?:this|it|access|to (?:try|get|have))\b", re.I),
    re.compile(r"\bsign me up\b", re.I),
    re.compile(r"\bcount me in\b", re.I),
    re.compile(r"\bwe need this\b", re.I),
    re.compile(r"\b(?:please )?send (?:it|this) (?:over|my way|to me)\b", re.I),
    re.compile(r"\bsend (?:it|this) (?:please|pls)\b", re.I),
    re.compile(r"\bhow (?:do|can) i get (?:this|it|access)\b", re.I),
    re.compile(r"\byes please\b", re.I),
    re.compile(r"\bdm(?:'d| me| please)?\b.{0,20}\b(?:this|it|access|interested)\b", re.I),
    re.compile(r"\binterested,? (?:please|pls|thanks|thx)\b", re.I),
    re.compile(r"\b(?:please )?share the link\b", re.I),
]

# Attendance/webinar-adjacent -- the "someone will be hosting webinar or events... people who
# attend will also be our right target" signal, applied to what an attendee ACTUALLY writes
# afterward (never attendee-list access, which this codebase has no real path to -- see this
# module's sibling docstring in engagement.py for why that idea was correctly redirected here).
_ATTENDANCE_PATTERNS = [
    re.compile(r"\bjust (?:registered|signed up)\b", re.I),
    re.compile(r"\b(?:will be|see you) (?:there|attending)\b", re.I),
    re.compile(r"\battended (?:the |this |your )?(?:webinar|session|talk|workshop)\b", re.I),
    re.compile(r"\bgreat (?:session|webinar|talk|workshop)\b", re.I),
    re.compile(r"\b(?:learned|learnt) (?:a lot|so much)\b", re.I),
    re.compile(r"\bthanks for (?:hosting|having me|the invite)\b", re.I),
    re.compile(r"\breally (?:enjoyed|insightful)\b.{0,20}\b(?:webinar|session|talk)\b", re.I),
]

# Pain/need language: the person is describing their own situation unprompted, not just
# reacting to the post -- the strongest signal, same first-person-declaration shape
# linkedin_search_config.py's own QUERY_CATALOG already looks for at the POST level, applied
# here to a COMMENT instead.
_PAIN_PATTERNS = [
    re.compile(r"\bwe(?:'re| are) struggling with\b", re.I),
    re.compile(r"\bexactly what we need\b", re.I),
    re.compile(r"\bwe need help with\b", re.I),
    re.compile(r"\blooking to hire\b", re.I),
    re.compile(r"\bwe(?:'re| are) trying to (?:fix|solve)\b", re.I),
    re.compile(r"\bthis (?:would|'d) (?:really )?help (?:us|our team)\b", re.I),
    re.compile(r"\bwe(?:'re| are) (?:currently )?looking for\b", re.I),
    re.compile(r"\bsame (?:issue|problem|struggle) here\b", re.I),
]

_ALL = [
    (CATEGORY_DIRECT_INTEREST, _DIRECT_INTEREST_PATTERNS),
    (CATEGORY_ATTENDANCE, _ATTENDANCE_PATTERNS),
    (CATEGORY_PAIN, _PAIN_PATTERNS),
]

# INTERNAL-HIRING PRE-FILTER, added 2026-09-21 -- real miss found live on majji's first test:
# the phrase search matched "We're hiring our first SDR for Group Sales at Backcountry" (an
# ordinary recruiting post for an EMPLOYEE) because one of the configured phrases
# ("hiring a head of sales") is inherently ambiguous -- it reads the same whether a company wants
# to hire someone onto their OWN payroll or is signaling they'd take FRACTIONAL/external help.
# Harvesting that post's comments cost real Apify money on 10 job APPLICANTS, none of whom could
# ever be a real lead -- classify_engagement_intent correctly rejected all 10, but only after
# paying to find them. This filter runs on the POST's own text, BEFORE the paid engagement-
# harvest call, so a post that reads as internal recruiting is skipped before spending on it.
#
# Deliberately narrow (same low-false-positive-risk discipline as every other pattern list in
# this module): an EXTERNAL_HELP override phrase (fractional/consultant/agency/advisor/outside
# help) always wins, since those are exactly the words majji's ICP notes use for what he sells,
# and a post can legitimately use "hiring" language while asking for that. A post that matches
# neither list is left alone -- this only filters clear internal-recruiting language, never
# guesses "irrelevant" from silence. FIRST CUT, not validated against volume yet -- revisit once
# more real engagement-mining posts have been seen, same caveat as the rest of this module.
_INTERNAL_HIRING_PATTERNS = [
    re.compile(r"\bwe'?re hiring (?:our|a|an|for)\b", re.I),
    re.compile(r"\bwe are hiring (?:our|a|an|for)\b", re.I),
    re.compile(r"\bjoin (?:our|my) team\b", re.I),
    re.compile(r"\bopen (?:role|position|roles|positions)\b", re.I),
    re.compile(r"\bapply (?:now|here|today|within)\b", re.I),
    re.compile(r"\bsend (?:me )?your resume\b", re.I),
    re.compile(r"\bdm me your resume\b", re.I),
    re.compile(r"\bwe'?re looking to hire\b", re.I),
    re.compile(r"\bwe are looking to hire\b", re.I),
    re.compile(r"\bjob (?:opening|opportunity|posting)\b", re.I),
    re.compile(r"\bnew(?:est)? (?:member|addition) to (?:our|the) team\b", re.I),
]

_EXTERNAL_HELP_OVERRIDE_PATTERNS = [
    re.compile(r"\bfractional\b", re.I),
    re.compile(r"\bconsult(?:ant|ing)\b", re.I),
    re.compile(r"\bagency\b", re.I),
    re.compile(r"\bexternal help\b", re.I),
    re.compile(r"\boutside help\b", re.I),
    re.compile(r"\badvisor\b", re.I),
]


def is_internal_hiring_post(post_text: str | None) -> bool:
    """True if this post reads as ordinary internal recruiting (hiring an EMPLOYEE onto the
    poster's own team) rather than a need/offering signal -- see the module comment above for
    the real example this was built from. Never raises; empty/missing text is simply not a
    match (never guesses relevance from silence)."""
    text = (post_text or "").strip()
    if not text:
        return False
    if any(p.search(text) for p in _EXTERNAL_HELP_OVERRIDE_PATTERNS):
        return False
    return any(p.search(text) for p in _INTERNAL_HIRING_PATTERNS)


def classify_engagement_intent(comment_text: str | None) -> dict:
    """Returns {"qualified": bool, "categories": [...], "matched_phrases": [...]}.

    `qualified` is True the moment ANY pattern matches, in ANY category -- categories are kept
    for reporting/tuning, never to require more than one kind of match. A comment can (and often
    will) match more than one category; all are recorded.

    Never raises, never requires the text to be non-empty -- a missing/blank comment (a like-only
    engagement, if this actor ever returns those) is simply unqualified, not an error."""
    text = (comment_text or "").strip()
    if not text:
        return {"qualified": False, "categories": [], "matched_phrases": []}

    categories: list[str] = []
    matched: list[str] = []
    for category, patterns in _ALL:
        hits = [p.pattern for p in patterns if p.search(text)]
        if hits:
            categories.append(category)
            matched.extend(hits)

    return {"qualified": bool(categories), "categories": categories, "matched_phrases": matched}
