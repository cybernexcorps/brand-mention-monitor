"""Tests for AI Studio agent response parsing."""

import pytest
from yandex_agent import _parse_agent_response, _extract_urls_fallback, _normalize_mention


class TestParseAgentResponse:
    """Test the three-tier parsing strategy for agent responses."""

    def test_parses_json_code_block(self):
        """Agent response with proper ```json block is parsed correctly."""
        output = '''Here are the results:

```json
{
  "mentions": [
    {
      "url": "https://sostav.ru/publication/ddvb-case-123",
      "title": "DDVB \u043f\u0440\u0435\u0434\u0441\u0442\u0430\u0432\u0438\u043b\u043e \u043d\u043e\u0432\u044b\u0439 \u043a\u0435\u0439\u0441",
      "domain": "sostav.ru",
      "snippet": "\u0411\u0440\u0435\u043d\u0434\u0438\u043d\u0433\u043e\u0432\u043e\u0435 \u0430\u0433\u0435\u043d\u0442\u0441\u0442\u0432\u043e DDVB \u043f\u043e\u043a\u0430\u0437\u0430\u043b\u043e \u043d\u043e\u0432\u044b\u0439 \u043f\u0440\u043e\u0435\u043a\u0442.",
      "summary": "\u041a\u0435\u0439\u0441 DDVB \u043d\u0430 sostav.ru."
    }
  ]
}
```'''
        result = _parse_agent_response(output)
        assert len(result) == 1
        assert result[0]["url"] == "https://sostav.ru/publication/ddvb-case-123"
        assert result[0]["domain"] == "sostav.ru"

    def test_parses_raw_json_in_text(self):
        """Agent response with bare JSON (no code fence) is parsed."""
        output = '{"mentions": [{"url": "https://retail.ru/news/ddvb", "title": "News", "domain": "retail.ru", "snippet": "text", "summary": "sum"}]}'
        result = _parse_agent_response(output)
        assert len(result) == 1
        assert result[0]["url"] == "https://retail.ru/news/ddvb"

    def test_empty_mentions_array(self):
        """Agent returns empty array when nothing found."""
        output = '```json\n{"mentions": []}\n```'
        result = _parse_agent_response(output)
        assert result == []

    def test_empty_output(self):
        """Empty output returns empty list."""
        assert _parse_agent_response("") == []
        assert _parse_agent_response(None) == []

    def test_fallback_url_extraction(self):
        """When JSON parsing fails, URLs are extracted from free text."""
        output = """I found these mentions:
        1. https://sostav.ru/article/ddvb-branding - DDVB case study
        2. https://retail.ru/news/12345 - packaging news
        No more results."""
        result = _parse_agent_response(output)
        assert len(result) == 2
        assert result[0]["url"] == "https://sostav.ru/article/ddvb-branding"
        assert result[0]["domain"] == "sostav.ru"
        assert result[1]["url"] == "https://retail.ru/news/12345"

    def test_malformed_json_falls_back(self):
        """Malformed JSON triggers URL fallback extraction."""
        output = '```json\n{"mentions": [BROKEN}\n```\nSee https://vc.ru/ddvb-article for details.'
        result = _parse_agent_response(output)
        assert len(result) == 1
        assert result[0]["domain"] == "vc.ru"

    def test_missing_fields_get_defaults(self):
        """Mention dicts with missing fields get empty string defaults."""
        output = '```json\n{"mentions": [{"url": "https://example.com/page"}]}\n```'
        result = _parse_agent_response(output)
        assert len(result) == 1
        assert result[0]["url"] == "https://example.com/page"
        assert result[0]["title"] == ""
        assert result[0]["snippet"] == ""
        assert result[0]["summary"] == ""
        assert result[0]["domain"] == "example.com"

    def test_www_stripped_from_domain(self):
        """www. prefix is stripped from agent-provided domains."""
        output = '```json\n{"mentions": [{"url": "https://www.sostav.ru/x", "domain": "www.sostav.ru", "title": "T", "snippet": "S", "summary": "M"}]}\n```'
        result = _parse_agent_response(output)
        assert result[0]["domain"] == "sostav.ru"


class TestNormalizeMention:
    """Test mention normalization."""

    def test_domain_from_url_when_missing(self):
        """If domain is missing, it's derived from the URL."""
        m = _normalize_mention({"url": "https://vc.ru/some/article"})
        assert m["domain"] == "vc.ru"

    def test_www_stripped(self):
        m = _normalize_mention({"url": "https://www.retail.ru/p", "domain": "www.retail.ru"})
        assert m["domain"] == "retail.ru"

    def test_all_defaults(self):
        m = _normalize_mention({})
        assert m["url"] == ""
        assert m["title"] == ""
        assert m["domain"] == ""
        assert m["snippet"] == ""
        assert m["summary"] == ""


class TestExtractUrlsFallback:
    """Test last-resort URL extraction."""

    def test_extracts_http_urls(self):
        text = "Check https://example.com/page and http://other.ru/article for details."
        result = _extract_urls_fallback(text)
        assert len(result) == 2

    def test_deduplicates(self):
        text = "https://example.com https://example.com https://example.com"
        result = _extract_urls_fallback(text)
        assert len(result) == 1

    def test_strips_trailing_punctuation(self):
        text = "See https://example.com/page."
        result = _extract_urls_fallback(text)
        assert result[0]["url"] == "https://example.com/page"

    def test_empty_text(self):
        assert _extract_urls_fallback("no urls here") == []
