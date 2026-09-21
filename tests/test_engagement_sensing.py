"""Tests for the engagement-mining sensing worker and its chaining into post-search.

Two properties matter most:
1. Every engager returned by the actor is persisted, qualified or not (never discard purchased
   data -- the actor already billed for it regardless of what this function decides to keep).
2. The engagement harvest reuses the SAME posts the post-search step already found and paid
   for, rather than searching again -- a second search would double-pay Apify for scraping the
   same real-world posts.
"""
import pytest

from app.db.models import Batch, Company, Credential
from app.gtm_os.intelligence.sensing import sense_linkedin_post_engagement
from app.gtm_os.intelligence.signal import GtmSignal

TENANT = 15  # majji


@pytest.fixture
def db(db_factory):
    # Credential: _get_apify_api_key reads it even though the actor call itself is mocked.
    session = db_factory([GtmSignal, Company, Batch, Credential])
    session.add(Credential(tenant_id=TENANT, name="apify_api_key", value="test-key"))
    session.commit()
    return session


ENGAGER_ITEM = {
    "fullName": "Rohan Karunakaran",
    "profileUrl": "https://www.linkedin.com/in/rohan-karunakaran",
    "jobTitle": "Founder/CEO",
    "seniorityLevel": "Owner",
    "department": "Executive",
    "currentCompany": "Frontier Studio",
    "locationName": "San Francisco, CA",
    "commentText": "We're struggling with exactly this, would love access",
    "commentPostedAgoText": "2h",
    "timesEngaged": 1,
    "engagedPostUrls": ["https://www.linkedin.com/posts/ericosiu_video-editor"],
    "postUrl": "https://www.linkedin.com/posts/ericosiu_video-editor",
    "postAuthorName": "Eric Siu",
    "isCompanyPage": False,
    "profileEnriched": True,
    "leadScore": 78,
}


def test_persists_every_engager_returned_regardless_of_intent(db, monkeypatch):
    """THE GOVERNING PRINCIPLE. The actor already billed for both of these; neither may be
    discarded, only flagged differently."""
    import app.gtm_os.intelligence.sensing as sensing

    noise_item = dict(ENGAGER_ITEM, fullName="Random Commenter",
                      profileUrl="https://www.linkedin.com/in/random", commentText="Nice post!")
    monkeypatch.setattr("app.apify_client.search_linkedin_post_engagers", lambda key, urls, max_results: [ENGAGER_ITEM, noise_item])

    signals = sense_linkedin_post_engagement(db, TENANT, ["https://www.linkedin.com/posts/ericosiu_video-editor"])
    assert len(signals) == 2

    qualified = [s for s in signals if s.extracted_info["intent_qualified"]]
    unqualified = [s for s in signals if not s.extracted_info["intent_qualified"]]
    assert len(qualified) == 1 and qualified[0].person_name_raw == "Rohan Karunakaran"
    assert len(unqualified) == 1 and unqualified[0].person_name_raw == "Random Commenter"


def test_signal_shape_and_fields(db, monkeypatch):
    import app.gtm_os.intelligence.sensing as sensing
    monkeypatch.setattr("app.apify_client.search_linkedin_post_engagers", lambda key, urls, max_results: [ENGAGER_ITEM])

    [signal] = sense_linkedin_post_engagement(db, TENANT, ["https://www.linkedin.com/posts/x"])

    assert signal.tenant_id == TENANT
    assert signal.source == "linkedin_engagement"
    assert signal.signal_type == "post_comment"
    assert signal.source_ref == ENGAGER_ITEM["profileUrl"]
    assert signal.person_name_raw == "Rohan Karunakaran"
    assert signal.company_name_raw == "Frontier Studio"
    assert signal.extracted_info["job_title"] == "Founder/CEO"
    assert signal.extracted_info["lead_score"] == 78
    assert signal.extracted_info["intent_qualified"] is True
    assert "pain_signal" in signal.extracted_info["intent_categories"]
    assert signal.dedup_key
    assert signal.raw_evidence == ENGAGER_ITEM


def test_repeat_engagement_does_not_duplicate_the_signal(db, monkeypatch):
    """The same person engaging with a post already sensed must not create a second row --
    same already_sensed discipline every other adapter in sensing.py already follows."""
    import app.gtm_os.intelligence.sensing as sensing
    monkeypatch.setattr("app.apify_client.search_linkedin_post_engagers", lambda key, urls, max_results: [ENGAGER_ITEM])

    sense_linkedin_post_engagement(db, TENANT, ["https://www.linkedin.com/posts/x"])
    signals_again = sense_linkedin_post_engagement(db, TENANT, ["https://www.linkedin.com/posts/x"])

    assert signals_again == []
    assert db.query(GtmSignal).filter(GtmSignal.source == "linkedin_engagement").count() == 1


def test_empty_post_urls_returns_empty_without_calling_the_actor(db, monkeypatch):
    import app.gtm_os.intelligence.sensing as sensing
    called = {"n": 0}
    monkeypatch.setattr("app.apify_client.search_linkedin_post_engagers", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [])

    assert sense_linkedin_post_engagement(db, TENANT, []) == []
    assert called["n"] == 0


def test_an_item_with_no_profile_url_is_skipped_not_crashed(db, monkeypatch):
    import app.gtm_os.intelligence.sensing as sensing
    bad_item = dict(ENGAGER_ITEM, profileUrl=None)
    monkeypatch.setattr("app.apify_client.search_linkedin_post_engagers", lambda key, urls, max_results: [bad_item])

    assert sense_linkedin_post_engagement(db, TENANT, ["https://x"]) == []


# --- chaining behavior (the wrapper in sweep.py) ------------------------------------------------

def test_wrapper_chains_off_the_same_posts_without_a_second_search(db, monkeypatch):
    """THE DOUBLE-SPEND GUARD. The wrapper must extract post URLs from the post-search results
    and pass them straight through -- never call search_linkedin_posts (the paid post-scraping
    actor) a second time."""
    import app.gtm_os.orchestration.sweep as sweep
    from app.db.models import Parameter

    db2 = db  # already has GtmSignal/Company/Batch; add Parameter for the search config
    from sqlalchemy import inspect
    Parameter.__table__.create(bind=db2.get_bind(), checkfirst=True)

    monkeypatch.setattr(
        "app.gtm_os.intelligence.linkedin_search_config.get_linkedin_search_config",
        lambda d, t: {
            "phrases": ["looking for a fractional sales leader"], "max_phrases_per_cycle": 8,
            "posts_per_phrase": 10, "date_posted_filter": "past-week",
            "min_hours_between_searches": 6, "phrase_last_searched_at": {},
            "engagement_mining_enabled": True,
        },
    )
    monkeypatch.setattr("app.apify_budget_guard.check_apify_budget", lambda *a, **k: {"status": "allowed", "reason": "ok"})

    post_search_calls = {"n": 0}

    def _fake_post_search(d, t):
        post_search_calls["n"] += 1
        s = GtmSignal(
            tenant_id=TENANT, source="linkedin_post", source_ref="urn:li:post:123",
            signal_type="post", dedup_key="k1", raw_evidence={"postUrl": "https://www.linkedin.com/posts/x"},
        )
        d.add(s)
        d.commit()
        return [s]

    monkeypatch.setattr(sweep, "sense_linkedin_post_search", _fake_post_search)

    engagement_calls = {"post_urls": None}

    def _fake_engagement(d, t, post_urls, **kw):
        engagement_calls["post_urls"] = post_urls
        return []

    monkeypatch.setattr("app.gtm_os.intelligence.sensing.sense_linkedin_post_engagement", _fake_engagement)

    sweep._run_linkedin_post_search(db2, TENANT)

    assert post_search_calls["n"] == 1, "post-search must run exactly once"
    assert engagement_calls["post_urls"] == ["https://www.linkedin.com/posts/x"], (
        "engagement mining must reuse the post URL the post-search step already found"
    )


def test_wrapper_skips_engagement_when_disabled(db, monkeypatch):
    import app.gtm_os.orchestration.sweep as sweep
    from app.db.models import Parameter
    Parameter.__table__.create(bind=db.get_bind(), checkfirst=True)

    monkeypatch.setattr(
        "app.gtm_os.intelligence.linkedin_search_config.get_linkedin_search_config",
        lambda d, t: {
            "phrases": ["x"], "max_phrases_per_cycle": 8, "posts_per_phrase": 10,
            "date_posted_filter": "past-week", "min_hours_between_searches": 6,
            "phrase_last_searched_at": {},
            # engagement_mining_enabled deliberately absent -- must default to off.
        },
    )
    monkeypatch.setattr("app.apify_budget_guard.check_apify_budget", lambda *a, **k: {"status": "allowed", "reason": "ok"})
    monkeypatch.setattr(sweep, "sense_linkedin_post_search", lambda d, t: [])

    called = {"n": 0}
    monkeypatch.setattr(
        "app.gtm_os.intelligence.sensing.sense_linkedin_post_engagement",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [],
    )

    sweep._run_linkedin_post_search(db, TENANT)
    assert called["n"] == 0, "engagement mining must never run for a tenant that has not opted in"
