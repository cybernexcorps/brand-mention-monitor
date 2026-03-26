"""Tests for XML response parsing and HTML cleaning."""

import base64
import sys
import os

# Allow imports from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from yandex_ai import _clean_html, _parse_search_xml


# ---------------------------------------------------------------------------
# Test data — mock Yandex Search API XML response
# ---------------------------------------------------------------------------

_MOCK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<yandexsearch version="1.0">
<response>
  <results>
    <grouping>
      <group>
        <doc>
          <url>https://sostav.ru/publication/ddvb-rebranding-2024.html</url>
          <domain>sostav.ru</domain>
          <title><hlword>DDVB</hlword> провела ребрендинг для клиента</title>
          <passages>
            <passage>Агентство <hlword>DDVB</hlword> завершило проект ребрендинга.</passage>
            <passage>Новый фирменный стиль был представлен на выставке.</passage>
          </passages>
        </doc>
      </group>
      <group>
        <doc>
          <url>https://retail.ru/news/ddvb-award-2024/</url>
          <domain>www.retail.ru</domain>
          <title>Премия года: <hlword>DDVB</hlword> в списке лауреатов</title>
          <passages>
            <passage>Брендинговое агентство <hlword>DDVB</hlword> получило награду.</passage>
          </passages>
        </doc>
      </group>
      <group>
        <doc>
          <url>https://example.com/no-title-page</url>
          <domain>example.com</domain>
          <passages>
            <passage>Some passage without a title tag above.</passage>
          </passages>
        </doc>
      </group>
    </grouping>
  </results>
</response>
</yandexsearch>"""


def _make_operation_result(xml_str: str) -> dict:
    """Build a mock operation result dict matching Yandex API shape."""
    raw_data = base64.b64encode(xml_str.encode("utf-8")).decode("ascii")
    return {
        "done": True,
        "response": {
            "rawData": raw_data,
        },
    }


# ---------------------------------------------------------------------------
# Tests — XML parsing
# ---------------------------------------------------------------------------

class TestParseSearchXml:

    def test_parses_all_groups(self):
        op = _make_operation_result(_MOCK_XML)
        results = _parse_search_xml(op, max_results=10)
        assert len(results) == 3

    def test_extracts_url(self):
        op = _make_operation_result(_MOCK_XML)
        results = _parse_search_xml(op, max_results=10)
        assert results[0]["url"] == "https://sostav.ru/publication/ddvb-rebranding-2024.html"

    def test_extracts_domain_strips_www(self):
        op = _make_operation_result(_MOCK_XML)
        results = _parse_search_xml(op, max_results=10)
        # Second result has www.retail.ru in XML
        assert results[1]["domain"] == "retail.ru"

    def test_cleans_title_html(self):
        op = _make_operation_result(_MOCK_XML)
        results = _parse_search_xml(op, max_results=10)
        title = results[0]["title"]
        assert "<hlword>" not in title
        assert "DDVB" in title

    def test_joins_passages_into_snippet(self):
        op = _make_operation_result(_MOCK_XML)
        results = _parse_search_xml(op, max_results=10)
        snippet = results[0]["snippet"]
        assert "ребрендинга" in snippet
        assert "фирменный стиль" in snippet

    def test_max_results_limit(self):
        op = _make_operation_result(_MOCK_XML)
        results = _parse_search_xml(op, max_results=2)
        assert len(results) == 2

    def test_empty_raw_data(self):
        op = {"done": True, "response": {"rawData": ""}}
        results = _parse_search_xml(op, max_results=10)
        assert results == []

    def test_missing_response(self):
        op = {"done": True, "response": {}}
        results = _parse_search_xml(op, max_results=10)
        assert results == []

    def test_missing_title_fallback(self):
        op = _make_operation_result(_MOCK_XML)
        results = _parse_search_xml(op, max_results=10)
        # Third group has no <title> tag
        assert results[2]["url"] == "https://example.com/no-title-page"
        assert results[2]["title"] == ""  # empty string fallback


# ---------------------------------------------------------------------------
# Tests — _clean_html
# ---------------------------------------------------------------------------

class TestCleanHtml:

    def test_removes_tags(self):
        assert _clean_html("<b>bold</b>") == "bold"

    def test_removes_hlword(self):
        assert _clean_html("<hlword>DDVB</hlword> agency") == "DDVB agency"

    def test_strips_whitespace(self):
        assert _clean_html("  hello  ") == "hello"

    def test_nested_tags(self):
        assert _clean_html("<a href='x'><b>text</b></a>") == "text"

    def test_empty_string(self):
        assert _clean_html("") == ""

    def test_no_tags(self):
        assert _clean_html("plain text") == "plain text"

    def test_self_closing_tags(self):
        assert _clean_html("before<br/>after") == "beforeafter"
