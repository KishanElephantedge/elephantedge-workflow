"""Tests for app/gtm_os/content/topic_linking.py's search-provenance matching.

REAL FIX, found live 2026-10-15 (Nora Volger, Competitor Activity showing zero topics despite 40
real competitor_content signals already fetched): sense_web_search_trends/sense_competitor_content/
sense_google_news_rss all search Google FOR one specific configured topic (the topic's own name
IS the query) and already record that in extracted_info.matched_keyword. Relying only on
match_topic()'s literal substring-in-title/summary scan threw that real provenance away -- a
competitor's page can rank for "site:example.com Commercial Narrative Development" without the
phrase appearing anywhere in its own title/summary.
"""
import pytest

from app.db.models import Parameter
from app.gtm_os.content.topic import ContentTopic, ContentTopicEvidence
from app.gtm_os.content.topic_linking import run_content_topic_linking_sweep
from app.gtm_os.intelligence.signal import GtmSignal

TENANT = 16  # nora


@pytest.fixture
def db(db_factory):
    return db_factory([Parameter, ContentTopic, ContentTopicEvidence, GtmSignal])


def _set_topics(db, names):
    db.add(Parameter(tenant_id=TENANT, key="content_intelligence_topics", value=[
        {"name": n, "aliases": [], "enabled": True, "description": None, "category": None} for n in names
    ]))
    db.commit()


def test_a_competitor_signal_links_via_matched_keyword_even_with_no_literal_text_overlap(db):
    """The exact real-world case: a competitor's page title/summary never says "Commercial
    Narrative Development" anywhere, but it was found BY searching Google for exactly that
    topic -- real provenance, not fabricated relevance."""
    _set_topics(db, ["Commercial Narrative Development"])

    db.add(GtmSignal(
        tenant_id=TENANT, source="competitor_content", source_ref="https://rival.com/about",
        signal_type="search_result", dedup_key="test-1",
        extracted_info={
            "title": "About Us - Rival Consulting",
            "summary": "We help companies grow.",
            "matched_keyword": "Commercial Narrative Development",
            "competitor_name": "Rival Consulting", "competitor_domain": "rival.com",
        },
    ))
    db.commit()

    result = run_content_topic_linking_sweep(db, TENANT)

    assert result["matches_found"] == 1
    assert result["evidence_created"] == 1
    evidence = db.query(ContentTopicEvidence).one()
    assert evidence.match_method == "search_provenance"
    assert evidence.matched_term == "Commercial Narrative Development"


def test_literal_text_match_still_works_and_is_labeled_differently(db):
    """The original matching path (real phrase actually present in the article) must keep
    working unchanged, and stays distinguishable in the audit trail from provenance matches."""
    _set_topics(db, ["Commercial Narrative Development"])

    db.add(GtmSignal(
        tenant_id=TENANT, source="web_search_trend", source_ref="https://example.com/post",
        signal_type="search_result", dedup_key="test-2",
        extracted_info={
            "title": "Why Commercial Narrative Development matters",
            "summary": "A deep dive.",
            "matched_keyword": "Commercial Narrative Development",
        },
    ))
    db.commit()

    run_content_topic_linking_sweep(db, TENANT)

    evidence = db.query(ContentTopicEvidence).one()
    assert evidence.match_method == "deterministic_alias_match"


def test_a_signal_with_no_matched_keyword_and_no_text_overlap_stays_unmatched(db):
    """Never fabricates a link -- a signal that's neither literally relevant nor was searched
    for this topic must not be counted as evidence for it."""
    _set_topics(db, ["Commercial Narrative Development"])

    db.add(GtmSignal(
        tenant_id=TENANT, source="hackernews_story", source_ref="https://news.ycombinator.com/item?id=1",
        signal_type="story", dedup_key="test-3",
        extracted_info={"title": "Show HN: a new JS framework", "summary": "Fast and small."},
    ))
    db.commit()

    result = run_content_topic_linking_sweep(db, TENANT)

    assert result["matches_found"] == 0
    assert result["unmatched_signals"] == 1
    assert db.query(ContentTopicEvidence).count() == 0
