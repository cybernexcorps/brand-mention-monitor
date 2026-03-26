"""Yandex AI Studio Responses API client — WebSearch-based brand mention discovery."""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from openai import OpenAI

from config import (
    AI_STUDIO_API_KEY,
    AI_STUDIO_BASE_URL,
    AI_STUDIO_PROJECT_ID,
    AI_STUDIO_AGENT_ID,
)

logger = logging.getLogger("brand-mention-monitor")

_MAX_RETRIES = 2
_BACKOFF_SECONDS = 3


def get_agent_client() -> OpenAI:
    """Create AI Studio client (OpenAI-compatible Responses API)."""
    return OpenAI(
        api_key=AI_STUDIO_API_KEY,
        base_url=AI_STUDIO_BASE_URL,
        project=AI_STUDIO_PROJECT_ID,
    )


def search_and_classify(
    brand_queries: list[str],
    date_from: str | None = None,
) -> list[dict]:
    """
    Call AI Studio agent with WebSearch to discover, classify, and summarize
    brand mentions in a single API call.

    Args:
        brand_queries: Brand name variants, e.g. ['"DDVB"', '"ДДВБ"']
        date_from: ISO date string for recency filter, e.g. "2026-03-19"

    Returns:
        List of mention dicts: {url, title, domain, snippet, summary,
        relevance, discovery_query, discovery_source}
    """
    if not AI_STUDIO_API_KEY:
        logger.warning("AI_STUDIO_API_KEY not set — skipping agent search")
        return []

    client = get_agent_client()
    message = _build_agent_input(brand_queries, date_from)

    for attempt in range(_MAX_RETRIES + 1):
        try:
            logger.info(
                "AI Studio agent call (attempt %d/%d)...",
                attempt + 1, _MAX_RETRIES + 1,
            )
            response = client.responses.create(
                prompt={"id": AI_STUDIO_AGENT_ID},
                input=message,
            )

            output = response.output_text or ""
            logger.debug("Agent response length: %d chars", len(output))

            mentions = _parse_agent_response(output)

            # Tag each mention with discovery metadata
            for m in mentions:
                m.setdefault("relevance", "relevant")
                m.setdefault("discovery_query", "ai_studio_agent")
                m.setdefault("discovery_source", "ai_studio_agent")
                # Ensure domain is set
                if not m.get("domain") and m.get("url"):
                    m["domain"] = urlparse(m["url"]).netloc.replace("www.", "")

            logger.info("Agent found %d pre-classified mentions", len(mentions))
            return mentions

        except Exception as e:
            logger.error("Agent call failed (attempt %d): %s", attempt + 1, e)
            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_SECONDS)
            else:
                logger.error("Agent exhausted retries, returning empty list")
                return []

    return []


def _build_agent_input(brand_queries: list[str], date_from: str | None) -> str:
    """Build the input message for the AI Studio agent."""
    queries_str = ", ".join(brand_queries)
    date_to = datetime.now().strftime("%Y-%m-%d")

    if date_from:
        date_range = f"с {date_from} по {date_to}"
    else:
        date_range = "за последние 2 недели"

    return (
        f"Найди все упоминания бренда DDVB (брендинговое агентство, Москва) "
        f"в российских онлайн-СМИ, отраслевых порталах и бизнес-изданиях "
        f"{date_range}.\n\n"
        f"Ищи по запросам: {queries_str}\n\n"
        f"Для каждого найденного упоминания определи:\n"
        f"- Это РЕДАКЦИОННОЕ упоминание (статья, новость, кейс, рейтинг, обзор, аналитика)?\n"
        f"- Или это шум (WHOIS, SEO-инструмент, каталог без редакционного контента, агрегатор)?\n\n"
        f"Верни ТОЛЬКО редакционные упоминания в формате JSON:\n\n"
        f"```json\n"
        f'{{\n'
        f'  "mentions": [\n'
        f'    {{\n'
        f'      "url": "полный URL страницы",\n'
        f'      "title": "точный заголовок страницы",\n'
        f'      "domain": "домен без www.",\n'
        f'      "snippet": "ключевая цитата с упоминанием DDVB, 1-2 предложения",\n'
        f'      "summary": "краткое описание контекста упоминания, 2-3 предложения"\n'
        f'    }}\n'
        f'  ]\n'
        f'}}\n'
        f"```\n\n"
        f'Если упоминаний не найдено, верни: {{"mentions": []}}'
    )


def _parse_agent_response(output_text: str) -> list[dict]:
    """
    Parse the agent's response into a list of mention dicts.

    Three-tier strategy:
    1. Find ```json ... ``` code block and parse JSON
    2. Find raw JSON object/array in text
    3. Fallback: extract URLs with regex
    """
    if not output_text:
        return []

    # Tier 1: JSON code block
    json_block = re.search(r"```json\s*(.*?)\s*```", output_text, re.DOTALL)
    if json_block:
        try:
            data = json.loads(json_block.group(1))
            mentions = data.get("mentions", data) if isinstance(data, dict) else data
            if isinstance(mentions, list):
                return [_normalize_mention(m) for m in mentions if isinstance(m, dict)]
        except json.JSONDecodeError:
            logger.warning("JSON code block found but failed to parse")

    # Tier 2: Raw JSON object in text
    json_match = re.search(r'\{[^{}]*"mentions"\s*:\s*\[.*?\]\s*\}', output_text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            mentions = data.get("mentions", [])
            if isinstance(mentions, list):
                return [_normalize_mention(m) for m in mentions if isinstance(m, dict)]
        except json.JSONDecodeError:
            logger.warning("Raw JSON found but failed to parse")

    # Tier 3: Extract URLs as fallback
    logger.warning("No parseable JSON in agent response, falling back to URL extraction")
    return _extract_urls_fallback(output_text)


def _normalize_mention(m: dict) -> dict:
    """Ensure a mention dict has all required fields with defaults."""
    url = m.get("url", "")
    domain = m.get("domain", "")
    if not domain and url:
        domain = urlparse(url).netloc
    domain = domain.replace("www.", "")

    return {
        "url": url,
        "title": m.get("title", ""),
        "domain": domain,
        "snippet": m.get("snippet", ""),
        "summary": m.get("summary", ""),
    }


def _extract_urls_fallback(text: str) -> list[dict]:
    """Extract http(s) URLs from free text and build minimal mention dicts."""
    url_pattern = re.compile(r'https?://[^\s<>"\')\]]+')
    urls = url_pattern.findall(text)

    # Deduplicate while preserving order
    seen = set()
    mentions = []
    for url in urls:
        url = url.rstrip(".,;:)")
        if url not in seen:
            seen.add(url)
            domain = urlparse(url).netloc.replace("www.", "")
            mentions.append({
                "url": url,
                "title": "",
                "domain": domain,
                "snippet": "",
                "summary": "",
            })

    return mentions
