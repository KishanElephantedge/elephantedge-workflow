"""Link-shortener hosts must never become a company's domain.

NOT HYPOTHETICAL. Production 2026-09-19:
  * Lumion existed TWICE inside tenant 2 -- once as lumion.ai, once as hi.switchy.io -- because
    the shortener host did not match the real domain, so the already-seen-domain dedup could not
    tell they were the same company and discovery paid to "find" it again. That one bad domain
    also stranded 9 signals as ambiguous.
  * Asseta was stored as hubs.li and PUSHED TO A CAMPAIGN on that domain, which makes any email
    inference or enrichment against it worthless.

A shortener host is not an identity: every company using HubSpot resolves to hubs.li, so storing
it makes unrelated companies look identical and makes the real one look new.
"""
import pytest

from app.phases.apify_discovery import _normalize_domain, is_link_shortener


@pytest.mark.parametrize("host", [
    "hubs.li", "switchy.io", "bit.ly", "lnkd.in", "t.co", "linktr.ee", "tinyurl.com",
])
def test_known_shorteners_are_recognised(host):
    assert is_link_shortener(host) is True


def test_subdomains_of_shorteners_are_recognised():
    """The real production value was hi.switchy.io, not switchy.io."""
    assert is_link_shortener("hi.switchy.io") is True
    assert is_link_shortener("go.hubs.li") is True


def test_real_company_domains_are_not_flagged():
    for domain in ("lumion.ai", "acmerobotics.com", "terra.security", "vasion.com", "yuzu.health"):
        assert is_link_shortener(domain) is False


def test_a_domain_merely_containing_a_shortener_name_is_not_flagged():
    """Substring matching here would reject real companies -- the check is host-or-subdomain."""
    assert is_link_shortener("bitly-consulting.com") is False
    assert is_link_shortener("mybit.ly.com") is False


def test_normalize_rejects_a_shortener_url_entirely():
    """Returning "" makes this behave exactly like a posting with no website at all, which the
    keep loop already handles -- rather than storing a domain that corrupts dedup."""
    assert _normalize_domain("https://hubs.li/Q02abc123") == ""
    assert _normalize_domain("http://hi.switchy.io/lumion") == ""


def test_normalize_still_handles_real_urls():
    assert _normalize_domain("https://www.lumion.ai/careers") == "lumion.ai"
    assert _normalize_domain("HTTP://ACMEROBOTICS.COM") == "acmerobotics.com"
    assert _normalize_domain("") == ""


def test_the_lumion_case_would_no_longer_duplicate():
    """The concrete regression: both of Lumion's production rows came from these two values.
    With the shortener rejected, only the real domain can ever create a company, so dedup sees
    one identity instead of two."""
    real = _normalize_domain("https://lumion.ai")
    shortened = _normalize_domain("https://hi.switchy.io/lumion")

    assert real == "lumion.ai"
    assert shortened == "", "the shortener must not become a second identity for the same company"
