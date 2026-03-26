"""Yandex AI Studio client — Responses API with Web Search + Text Classification."""

import asyncio
import json
import re
from urllib.parse import urlparse

from openai import OpenAI

from config import YC_API_KEY, YC_FOLDER_ID, YANDEX_RATE_LIMIT_SECONDS


def get_client() -> OpenAI:
    """Create Yandex AI Studio client (OpenAI-compatible SDK)."""
    return OpenAI(
        base_url="https://llm.api.cloud.yandex.net/foundationModels/v1",
        api_key=YC_API_KEY,
    )


def search_with_web(
    query: str,
    allowed_domains: list[str] | None = None,
) -> list[dict]:
    """
    Call Yandex AI Studio Responses API with Web Search tool.
    Returns list of {url, title, snippet, domain} dicts.
    """
    client = get_client()

    web_search_config = {
        "search_context_size": "high",
        "user_location": {"region": "ru"},
    }
    if allowed_domains:
        web_search_config["allowed_domains"] = allowed_domains

    messages = [
        {
            "role": "system",
            "content": (
                "Ты — ассистент для мониторинга упоминаний бренда в СМИ. "
                "Используй веб-поиск, чтобы найти все упоминания бренда. "
                "Для каждого результата верни: URL, заголовок, краткое описание. "
                "Отвечай строго в формате JSON-массива."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Найди все публикации в российских СМИ, где упоминается {query}. "
                "Верни результаты в формате JSON-массива: "
                '[{"url": "...", "title": "...", "snippet": "..."}]'
            ),
        },
    ]

    try:
        response = client.chat.completions.create(
            model=f"gpt://{YC_FOLDER_ID}/yandexgpt-lite/latest",
            messages=messages,
            temperature=0.1,
            extra_body={
                "tools": [{"type": "web_search", "web_search": web_search_config}],
            },
        )

        content = response.choices[0].message.content or ""
        return _parse_search_results(content, query)

    except Exception as e:
        print(f"[ERROR] Yandex search failed for query '{query}': {e}")
        return []


def _parse_search_results(content: str, query: str) -> list[dict]:
    """Parse LLM response into structured results."""
    results = []

    # Try to extract JSON array from response
    json_match = re.search(r'\[[\s\S]*?\]', content)
    if json_match:
        try:
            parsed = json.loads(json_match.group())
            for item in parsed:
                if isinstance(item, dict) and item.get("url"):
                    domain = urlparse(item["url"]).netloc.replace("www.", "")
                    results.append({
                        "url": item["url"],
                        "title": item.get("title", ""),
                        "snippet": item.get("snippet", ""),
                        "domain": domain,
                        "discovery_query": query,
                    })
        except json.JSONDecodeError:
            pass

    # Fallback: extract URLs from plain text
    if not results:
        urls = re.findall(r'https?://[^\s\)\]\"\']+', content)
        for url in urls:
            domain = urlparse(url).netloc.replace("www.", "")
            results.append({
                "url": url,
                "title": "",
                "snippet": "",
                "domain": domain,
                "discovery_query": query,
            })

    return results


def classify_relevance(text: str) -> str:
    """
    Classify text as relevant/irrelevant to DDVB using YandexGPT.
    Returns 'relevant' or 'irrelevant'.
    """
    client = get_client()

    messages = [
        {
            "role": "system",
            "content": (
                "Ты — классификатор релевантности. Определи, упоминается ли бренд "
                "DDVB, ДДВБ, или их клиенты/проекты в тексте. "
                "Ответь одним словом: relevant или irrelevant."
            ),
        },
        {
            "role": "user",
            "content": f"Текст: {text[:1000]}",
        },
    ]

    try:
        response = client.chat.completions.create(
            model=f"gpt://{YC_FOLDER_ID}/yandexgpt-lite/latest",
            messages=messages,
            temperature=0.0,
            max_tokens=10,
        )
        answer = (response.choices[0].message.content or "").strip().lower()
        return "relevant" if "relevant" in answer else "irrelevant"

    except Exception as e:
        print(f"[ERROR] Classification failed: {e}")
        return "relevant"  # err on the side of inclusion
