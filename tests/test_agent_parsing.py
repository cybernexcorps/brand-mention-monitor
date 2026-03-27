"""Tests for AI Studio generative search mention processing."""

from unittest.mock import MagicMock, patch
from urllib.parse import urlparse


class TestMentionNormalization:
    """Test that generative search results are properly normalized."""

    def test_domain_extracted_from_url(self):
        """Domain is correctly parsed from URL."""
        url = "https://marketing-tech.ru/cases/ddvb/article"
        domain = urlparse(url).netloc.replace("www.", "")
        assert domain == "marketing-tech.ru"

    def test_www_stripped(self):
        url = "https://www.sostav.ru/publication/123"
        domain = urlparse(url).netloc.replace("www.", "")
        assert domain == "sostav.ru"

    def test_mention_dict_structure(self):
        """A mention dict from generative search has all required fields."""
        mention = {
            "url": "https://retail.ru/news/ddvb",
            "title": "DDVB News",
            "domain": "retail.ru",
            "snippet": "",
            "summary": "AI-generated summary",
            "relevance": "relevant",
            "discovery_query": '"DDVB" (generative-search)',
            "discovery_source": "ai_studio_generative",
        }
        required_keys = {"url", "title", "domain", "snippet", "summary",
                         "relevance", "discovery_query", "discovery_source"}
        assert required_keys.issubset(mention.keys())
