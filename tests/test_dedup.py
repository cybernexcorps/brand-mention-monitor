"""Tests for URL deduplication, blocked domain filtering, and exclude filtering."""

import sys
import os

# Allow imports from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from config import BLOCKED_DOMAINS


# ---------------------------------------------------------------------------
# Helpers (mirrors main.py logic for unit-testing in isolation)
# ---------------------------------------------------------------------------

def _normalize_url(url: str) -> str:
    url = url.rstrip("/").split("?")[0].split("#")[0]
    url = url.replace("://www.", "://")
    return url


def deduplicate(results: list[dict], existing_urls: set[str] | None = None) -> list[dict]:
    normalized_existing = {_normalize_url(u) for u in (existing_urls or set())}
    seen: set[str] = set()
    unique: list[dict] = []
    for r in results:
        norm = _normalize_url(r["url"])
        if norm not in seen and norm not in normalized_existing:
            seen.add(norm)
            unique.append(r)
    return unique


def filter_blocked(results: list[dict], exclude_domains: list[str] | None = None) -> list[dict]:
    exclude_set = BLOCKED_DOMAINS | set(exclude_domains or [])
    return [
        r for r in results
        if r["domain"].replace("www.", "") not in exclude_set
    ]


# ---------------------------------------------------------------------------
# Tests — URL deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:

    def test_trailing_slash(self):
        results = [
            {"url": "https://sostav.ru/article/123/", "domain": "sostav.ru", "title": "A"},
            {"url": "https://sostav.ru/article/123", "domain": "sostav.ru", "title": "B"},
        ]
        unique = deduplicate(results)
        assert len(unique) == 1
        assert unique[0]["title"] == "A"  # first one wins

    def test_query_params_stripped(self):
        results = [
            {"url": "https://retail.ru/news/abc?utm_source=yandex", "domain": "retail.ru", "title": "A"},
            {"url": "https://retail.ru/news/abc", "domain": "retail.ru", "title": "B"},
        ]
        unique = deduplicate(results)
        assert len(unique) == 1

    def test_www_prefix_normalized(self):
        results = [
            {"url": "https://www.sostav.ru/article/123", "domain": "sostav.ru", "title": "A"},
            {"url": "https://sostav.ru/article/123", "domain": "sostav.ru", "title": "B"},
        ]
        unique = deduplicate(results)
        assert len(unique) == 1

    def test_existing_urls_excluded(self):
        results = [
            {"url": "https://sostav.ru/article/123", "domain": "sostav.ru", "title": "New"},
            {"url": "https://retail.ru/news/456", "domain": "retail.ru", "title": "Also New"},
        ]
        existing = {"https://sostav.ru/article/123"}
        unique = deduplicate(results, existing)
        assert len(unique) == 1
        assert unique[0]["title"] == "Also New"

    def test_hash_fragment_stripped(self):
        results = [
            {"url": "https://example.com/page#section1", "domain": "example.com", "title": "A"},
            {"url": "https://example.com/page", "domain": "example.com", "title": "B"},
        ]
        unique = deduplicate(results)
        assert len(unique) == 1

    def test_different_urls_kept(self):
        results = [
            {"url": "https://sostav.ru/article/111", "domain": "sostav.ru", "title": "A"},
            {"url": "https://sostav.ru/article/222", "domain": "sostav.ru", "title": "B"},
        ]
        unique = deduplicate(results)
        assert len(unique) == 2


# ---------------------------------------------------------------------------
# Tests — Blocked domain filtering
# ---------------------------------------------------------------------------

class TestBlockedDomainFiltering:

    def test_own_domains_blocked(self):
        results = [
            {"url": "https://ddvb.ru/about", "domain": "ddvb.ru", "title": "Own site"},
            {"url": "https://sostav.ru/news", "domain": "sostav.ru", "title": "External"},
        ]
        filtered = filter_blocked(results)
        assert len(filtered) == 1
        assert filtered[0]["domain"] == "sostav.ru"

    def test_social_media_blocked(self):
        results = [
            {"url": "https://t.me/ddvb_channel", "domain": "t.me", "title": "Telegram"},
            {"url": "https://vk.com/ddvb", "domain": "vk.com", "title": "VK"},
            {"url": "https://retail.ru/news", "domain": "retail.ru", "title": "Good"},
        ]
        filtered = filter_blocked(results)
        assert len(filtered) == 1
        assert filtered[0]["domain"] == "retail.ru"

    def test_whois_seo_tools_blocked(self):
        results = [
            {"url": "https://cy-pr.com/ddvb.ru", "domain": "cy-pr.com", "title": "SEO"},
            {"url": "https://2whois.ru/ddvb.ru", "domain": "2whois.ru", "title": "WHOIS"},
            {"url": "https://pr-cy.ru/ddvb.ru", "domain": "pr-cy.ru", "title": "PR-CY"},
        ]
        filtered = filter_blocked(results)
        assert len(filtered) == 0

    def test_www_prefix_handled(self):
        results = [
            {"url": "https://www.ddvb.ru/about", "domain": "www.ddvb.ru", "title": "Own"},
        ]
        filtered = filter_blocked(results)
        assert len(filtered) == 0

    def test_search_engines_blocked(self):
        results = [
            {"url": "https://yandex.ru/search?q=ddvb", "domain": "yandex.ru", "title": "SERP"},
            {"url": "https://google.ru/search?q=ddvb", "domain": "google.ru", "title": "SERP"},
        ]
        filtered = filter_blocked(results)
        assert len(filtered) == 0


# ---------------------------------------------------------------------------
# Tests — Exclude domain filtering
# ---------------------------------------------------------------------------

class TestExcludeDomainFiltering:

    def test_exclude_domains_removed(self):
        results = [
            {"url": "https://sostav.ru/article/1", "domain": "sostav.ru", "title": "A"},
            {"url": "https://retail.ru/news/2", "domain": "retail.ru", "title": "B"},
        ]
        filtered = filter_blocked(results, exclude_domains=["sostav.ru"])
        assert len(filtered) == 1
        assert filtered[0]["domain"] == "retail.ru"

    def test_exclude_plus_blocked_combined(self):
        results = [
            {"url": "https://ddvb.ru/about", "domain": "ddvb.ru", "title": "Own"},
            {"url": "https://sostav.ru/article", "domain": "sostav.ru", "title": "Excluded"},
            {"url": "https://retail.ru/news", "domain": "retail.ru", "title": "Keep"},
        ]
        filtered = filter_blocked(results, exclude_domains=["sostav.ru"])
        assert len(filtered) == 1
        assert filtered[0]["domain"] == "retail.ru"

    def test_empty_exclude_list(self):
        results = [
            {"url": "https://retail.ru/news", "domain": "retail.ru", "title": "A"},
        ]
        filtered = filter_blocked(results, exclude_domains=[])
        assert len(filtered) == 1
