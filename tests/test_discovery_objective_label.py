"""Tests for _discovery_objective (app/routes/api.py) -- the "how/where did we find this
company" label surfaced to the UI, added 2026-09-22 after a partner (majji) asked to see which
objective (firmographic ICP discovery vs LinkedIn engagement mining) found each account."""
from app.routes.api import _discovery_objective


class _Company:
    def __init__(self, source=None, hiring_signal_reasoning=None, active_job_title=None):
        self.source = source
        self.hiring_signal_reasoning = hiring_signal_reasoning
        self.active_job_title = active_job_title


def test_apify_job_search_source_gets_a_plain_label():
    c = _Company(source="apify:fantastic-jobs_advanced-linkedin-job-search-api")
    result = _discovery_objective(c)
    assert result["objective_label"] == "Firmographic ICP discovery (hiring signal)"


def test_posting_url_is_extracted_from_the_reasoning_string():
    c = _Company(
        source="apify:fantastic-jobs_advanced-linkedin-job-search-api",
        hiring_signal_reasoning="Head of Sales role, strength=high [posting: https://www.linkedin.com/jobs/view/123] [headcount: 40] [industry: Software]",
        active_job_title="Head of Sales",
    )
    result = _discovery_objective(c)
    assert result["posting_url"] == "https://www.linkedin.com/jobs/view/123"
    assert result["job_title"] == "Head of Sales"


def test_an_unrecognized_source_falls_back_to_the_raw_value_not_none():
    c = _Company(source="some_future_source")
    assert _discovery_objective(c)["objective_label"] == "some_future_source"


def test_no_source_at_all_returns_none_not_a_crash():
    c = _Company()
    result = _discovery_objective(c)
    assert result["objective_label"] is None
    assert result["posting_url"] is None


def test_no_posting_marker_in_reasoning_does_not_crash():
    c = _Company(source="apify:fantastic-jobs_advanced-linkedin-job-search-api", hiring_signal_reasoning="no marker here")
    assert _discovery_objective(c)["posting_url"] is None
