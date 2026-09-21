"""Tests for the deterministic engagement-intent classifier.

No historical labeled data exists yet for this source (it's new), so these tests pin the
documented intent rather than a calibrated recall/precision number -- see the module's own
honesty note about needing revalidation once real signals flow.
"""
from app.gtm_os.intelligence.engagement_intent import (
    CATEGORY_ATTENDANCE, CATEGORY_DIRECT_INTEREST, CATEGORY_PAIN, classify_engagement_intent,
)


def test_direct_interest_is_qualified():
    for text in [
        "Would love access to this!",
        "Sign me up",
        "We need this so badly",
        "How do I get this?",
        "Please send this over, thanks",
        "yes please!!",
    ]:
        result = classify_engagement_intent(text)
        assert result["qualified"] is True, text
        assert CATEGORY_DIRECT_INTEREST in result["categories"]


def test_attendance_signal_is_qualified():
    for text in [
        "Just registered, see you there!",
        "Great session today, learned a lot",
        "Thanks for hosting, really insightful webinar",
        "Attended the workshop last week, still thinking about it",
    ]:
        result = classify_engagement_intent(text)
        assert result["qualified"] is True, text
        assert CATEGORY_ATTENDANCE in result["categories"]


def test_pain_signal_is_qualified():
    for text in [
        "We're struggling with exactly this",
        "This is exactly what we need",
        "We're currently looking for a solution like this",
        "Same problem here, we're trying to fix it",
    ]:
        result = classify_engagement_intent(text)
        assert result["qualified"] is True, text
        assert CATEGORY_PAIN in result["categories"]


def test_a_bare_lead_magnet_word_is_not_qualified_on_its_own():
    """KNOWN, documented limitation. From the real Eric Siu example this feature was built from:
    most real leads just wrote 'Editor' (the tool's name) -- a bare single word cannot be
    distinguished from noise by a generic, cross-post classifier without knowing what the post
    actually offered. Persisted as a raw signal regardless (see engagement.py's own 'never
    discard purchased data' discipline) -- just not flagged as intent-qualified here."""
    for text in ["Editor", "EDITOR", "editor", "Interested"]:
        result = classify_engagement_intent(text)
        assert result["qualified"] is False, text


def test_generic_positive_reactions_are_not_qualified():
    for text in ["Great post!", "Love this", "Nice", "🔥🔥🔥", "Thanks for sharing", "Amazing"]:
        result = classify_engagement_intent(text)
        assert result["qualified"] is False, text


def test_empty_or_missing_text_is_not_qualified_and_never_raises():
    assert classify_engagement_intent(None)["qualified"] is False
    assert classify_engagement_intent("")["qualified"] is False
    assert classify_engagement_intent("   ")["qualified"] is False


def test_a_comment_can_match_more_than_one_category():
    result = classify_engagement_intent("We're struggling with this -- would love access, please send it over")
    assert result["qualified"] is True
    assert CATEGORY_PAIN in result["categories"]
    assert CATEGORY_DIRECT_INTEREST in result["categories"]
    assert len(result["matched_phrases"]) >= 2


def test_matched_phrases_are_the_actual_regex_patterns_that_fired():
    result = classify_engagement_intent("sign me up")
    assert result["matched_phrases"], "a qualified result must report what matched"


def test_off_topic_comments_from_a_real_thread_are_not_qualified():
    """Real, verbatim off-topic comments from the same production thread this feature is built
    from (contatcs.txt) -- confirms the classifier does not over-match generic chatter."""
    for text in [
        "How are the tokens managed?",
        "why not just give it to people instead of doing this lead magnet silliness?",
        "The reversible edit plan is the part I find most interesting.",
    ]:
        assert classify_engagement_intent(text)["qualified"] is False, text
