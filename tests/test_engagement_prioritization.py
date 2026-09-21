"""Tests for engagement-harvest prioritization by real post engagement.

Real user feedback after majji's first live test: the post it harvested had numComments=1,
numLikes=0 -- almost nothing there to find. The engagement actor bills maxResults as a SHARED
cap across every post_url passed in, so spreading it evenly across low-engagement posts wastes
most of the budget on posts with nothing to harvest. numLikes/numComments/numShares are already
free (a byproduct of the post-search call already paid for) -- this just uses them.
"""
import pytest

from app.db.models import Batch, Company, Parameter
from app.gtm_os.intelligence.signal import GtmSignal

TENANT = 15  # majji
ELEPHANT_EDGE = 2


@pytest.fixture
def db(db_factory):
    return db_factory([Parameter, GtmSignal, Company, Batch])


def _signal(db, post_url, comments=0, likes=0, shares=0, ref="x"):
    s = GtmSignal(
        tenant_id=TENANT, source="linkedin_post", source_ref=ref, signal_type="post",
        dedup_key=f"k{ref}",
        raw_evidence={"postUrl": post_url, "numComments": comments, "numLikes": likes, "numShares": shares},
    )
    db.add(s)
    return s


def _search_config():
    return {
        "phrases": ["x"], "max_phrases_per_cycle": 8, "posts_per_phrase": 10,
        "date_posted_filter": "past-week", "min_hours_between_searches": 6,
        "phrase_last_searched_at": {}, "engagement_mining_enabled": True,
    }


def _setup_budget(db, monkeypatch):
    db.add(Parameter(tenant_id=ELEPHANT_EDGE, key="gtm_os_control_config", value={
        "state": "running", "apify": {"daily_budget_usd": 5.0, "monthly_budget_usd": 50.0},
    }))
    db.commit()
    import app.apify_budget_guard as guard
    monkeypatch.setattr(guard, "_get_apify_api_key", lambda db, tid: "key")
    monkeypatch.setattr(guard, "get_monthly_usage", lambda key: {
        "dailyServiceUsages": [], "totalUsageCreditsUsdAfterVolumeDiscount": 0.0,
    })


def test_higher_engagement_posts_are_sent_first(db, monkeypatch):
    import app.gtm_os.orchestration.sweep as sweep

    _setup_budget(db, monkeypatch)
    monkeypatch.setattr(
        "app.gtm_os.intelligence.linkedin_search_config.get_linkedin_search_config",
        lambda d, t: _search_config(),
    )

    def _fake_post_search(d, t):
        low = _signal(d, "https://x/low", comments=1, likes=0, ref="low")
        high = _signal(d, "https://x/high", comments=40, likes=200, ref="high")
        mid = _signal(d, "https://x/mid", comments=5, likes=10, ref="mid")
        d.commit()
        return [low, high, mid]

    monkeypatch.setattr(sweep, "sense_linkedin_post_search", _fake_post_search)

    seen = {}
    monkeypatch.setattr(
        "app.gtm_os.intelligence.sensing.sense_linkedin_post_engagement",
        lambda d, t, post_urls, **kw: seen.__setitem__("post_urls", post_urls) or [],
    )

    sweep._run_linkedin_post_search(db, TENANT, budget_tenant_id=ELEPHANT_EDGE)
    assert seen["post_urls"] == ["https://x/high", "https://x/mid", "https://x/low"]


def test_harvest_is_capped_to_the_top_N_posts_not_every_post_found(db, monkeypatch):
    """THE POINT. Concentrate budget, don't spread it across every low-value post found."""
    import app.gtm_os.orchestration.sweep as sweep

    _setup_budget(db, monkeypatch)
    monkeypatch.setattr(
        "app.gtm_os.intelligence.linkedin_search_config.get_linkedin_search_config",
        lambda d, t: _search_config(),
    )

    def _fake_post_search(d, t):
        made = [_signal(d, f"https://x/{i}", comments=i, ref=str(i)) for i in range(10)]
        d.commit()
        return made

    monkeypatch.setattr(sweep, "sense_linkedin_post_search", _fake_post_search)

    seen = {}
    monkeypatch.setattr(
        "app.gtm_os.intelligence.sensing.sense_linkedin_post_engagement",
        lambda d, t, post_urls, **kw: seen.__setitem__("post_urls", post_urls) or [],
    )

    sweep._run_linkedin_post_search(db, TENANT, budget_tenant_id=ELEPHANT_EDGE)
    assert len(seen["post_urls"]) == sweep.MAX_POSTS_FOR_ENGAGEMENT_HARVEST
    # The 5 HIGHEST-numbered (highest comment count) posts, not the first 5 found.
    assert seen["post_urls"] == ["https://x/9", "https://x/8", "https://x/7", "https://x/6", "https://x/5"]


def test_a_post_with_no_engagement_data_at_all_still_participates_safely(db, monkeypatch):
    """Missing numComments/numLikes must not crash the sort -- treated as zero, not excluded."""
    import app.gtm_os.orchestration.sweep as sweep

    _setup_budget(db, monkeypatch)
    monkeypatch.setattr(
        "app.gtm_os.intelligence.linkedin_search_config.get_linkedin_search_config",
        lambda d, t: _search_config(),
    )

    def _fake_post_search(d, t):
        s = GtmSignal(
            tenant_id=TENANT, source="linkedin_post", source_ref="bare", signal_type="post",
            dedup_key="kbare", raw_evidence={"postUrl": "https://x/bare"},  # no engagement fields at all
        )
        d.add(s)
        d.commit()
        return [s]

    monkeypatch.setattr(sweep, "sense_linkedin_post_search", _fake_post_search)
    seen = {}
    monkeypatch.setattr(
        "app.gtm_os.intelligence.sensing.sense_linkedin_post_engagement",
        lambda d, t, post_urls, **kw: seen.__setitem__("post_urls", post_urls) or [],
    )

    sweep._run_linkedin_post_search(db, TENANT, budget_tenant_id=ELEPHANT_EDGE)
    assert seen["post_urls"] == ["https://x/bare"]


def test_internal_hiring_posts_are_excluded_from_harvest_entirely(db, monkeypatch):
    """THE REAL FIX. A post that reads as ordinary internal recruiting must never be sent to the
    paid engagement-harvest call at all, even if it has high engagement -- real money was
    wasted on exactly this post in majji's first live test (10 job applicants, 0 qualified)."""
    import app.gtm_os.orchestration.sweep as sweep

    _setup_budget(db, monkeypatch)
    monkeypatch.setattr(
        "app.gtm_os.intelligence.linkedin_search_config.get_linkedin_search_config",
        lambda d, t: _search_config(),
    )

    def _fake_post_search(d, t):
        hiring_post = GtmSignal(
            tenant_id=TENANT, source="linkedin_post", source_ref="hiring", signal_type="post",
            dedup_key="k-hiring",
            raw_evidence={"postUrl": "https://x/hiring", "numComments": 158, "numLikes": 110},
            extracted_info={"text": "We're hiring our first SDR for Group Sales at Backcountry."},
        )
        real_signal = _signal(d, "https://x/real", comments=5, likes=10, ref="real")
        d.add(hiring_post)
        d.commit()
        return [hiring_post, real_signal]

    monkeypatch.setattr(sweep, "sense_linkedin_post_search", _fake_post_search)

    seen = {}
    monkeypatch.setattr(
        "app.gtm_os.intelligence.sensing.sense_linkedin_post_engagement",
        lambda d, t, post_urls, **kw: seen.__setitem__("post_urls", post_urls) or [],
    )

    sweep._run_linkedin_post_search(db, TENANT, budget_tenant_id=ELEPHANT_EDGE)
    assert seen["post_urls"] == ["https://x/real"], "the hiring post must be filtered out despite its higher engagement"
