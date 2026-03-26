"""Yandex Cloud client — Search API v2 + YandexGPT classification."""

import base64
import json
import re
import time
from urllib.parse import urlparse

import httpx
from openai import OpenAI

from config import YC_API_KEY, YC_FOLDER_ID, YANDEX_RATE_LIMIT_SECONDS

# --- HTTP client ---

_http = httpx.Client(
    headers={"Authorization": f"Api-Key {YC_API_KEY}"},
    timeout=60.0,
)

SEARCH_API_URL = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
OPERATIONS_URL = "https://operation.api.cloud.yandex.net/operations"


# --- Yandex Search API v2 ---

def search_web(
    query: str,
    site_filter: list[str] | None = None,
    max_results: int = 10,
) -> list[dict]:
    """
    Search Russian web via Yandex Search API v2 (async).
    Returns list of {url, title, snippet, domain} dicts.

    Args:
        query: Search query text
        site_filter: Optional list of domains to restrict search (e.g. ["retail.ru"])
        max_results: Max results to return (up to 10 per page)
    """
    # Build query with site: filter if provided
    full_query = query
    if site_filter:
        site_clause = " | ".join(f"site:{d}" for d in site_filter)
        full_query = f"({query}) ({site_clause})"

    body = {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": full_query,
            "familyMode": "FAMILY_MODE_NONE",
        },
        "sortSpec": {
            "sortMode": "SORT_MODE_BY_RELEVANCE",
            "sortOrder": "SORT_ORDER_DESC",
        },
        "maxPassages": 2,
        "region": "225",  # Russia
        "folderId": YC_FOLDER_ID,
    }

    try:
        # Submit async search
        resp = _http.post(SEARCH_API_URL, json=body)
        resp.raise_for_status()
        operation = resp.json()
        operation_id = operation["id"]

        # Poll for results (max 30 seconds)
        for _ in range(15):
            time.sleep(2)
            poll = _http.get(f"{OPERATIONS_URL}/{operation_id}")
            poll.raise_for_status()
            result = poll.json()
            if result.get("done"):
                return _parse_search_xml(result, max_results)

        print(f"[WARN] Search timed out for query: {query}")
        return []

    except Exception as e:
        print(f"[ERROR] Search failed for '{query}': {e}")
        return []


def _parse_search_xml(operation_result: dict, max_results: int) -> list[dict]:
    """Parse base64-encoded XML response from Yandex Search API."""
    raw_data = operation_result.get("response", {}).get("rawData", "")
    if not raw_data:
        return []

    xml = base64.b64decode(raw_data).decode("utf-8")

    # Extract doc groups
    results = []
    groups = re.findall(r"<group>(.*?)</group>", xml, re.DOTALL)

    for group in groups[:max_results]:
        url_match = re.search(r"<url>(.*?)</url>", group)
        title_match = re.search(r"<title>(.*?)</title>", group)
        domain_match = re.search(r"<domain>(.*?)</domain>", group)
        passage_matches = re.findall(r"<passage>(.*?)</passage>", group)

        if not url_match:
            continue

        url = url_match.group(1)
        title = _clean_html(title_match.group(1)) if title_match else ""
        domain = domain_match.group(1).replace("www.", "") if domain_match else urlparse(url).netloc
        snippet = " ".join(_clean_html(p) for p in passage_matches[:2])

        results.append({
            "url": url,
            "title": title,
            "snippet": snippet,
            "domain": domain,
        })

    return results


def _clean_html(text: str) -> str:
    """Remove HTML/XML tags from text."""
    return re.sub(r"<[^>]+>", "", text).strip()


# --- YandexGPT Classification ---

def get_llm_client() -> OpenAI:
    """Create YandexGPT client (OpenAI-compatible SDK)."""
    return OpenAI(
        base_url="https://llm.api.cloud.yandex.net/v1",
        api_key="unused",
        default_headers={"Authorization": f"Api-Key {YC_API_KEY}"},
    )


def classify_relevance(title: str, snippet: str) -> str:
    """
    Classify whether a search result is a genuine DDVB brand mention
    (not DDVB's own website or social media).
    Returns 'relevant' or 'irrelevant'.
    """
    client = get_llm_client()
    text = f"Заголовок: {title}\nОписание: {snippet}"

    messages = [
        {
            "role": "system",
            "content": (
                "Ты — классификатор медиа-упоминаний бренда DDVB (брендинговое агентство). "
                "Определи, является ли данный результат поиска СТОРОННИМ упоминанием DDVB "
                "в СМИ или отраслевом ресурсе (relevant), или это собственный ресурс DDVB "
                "(сайт ddvb.ru, соцсети, каталог) или нерелевантный результат (irrelevant). "
                "Ответь одним словом: relevant или irrelevant."
            ),
        },
        {"role": "user", "content": text[:1000]},
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
        return "relevant"  # err on side of inclusion


def summarize_mention(title: str, snippet: str) -> str:
    """Generate a 2-3 sentence Russian summary of a mention."""
    client = get_llm_client()

    messages = [
        {
            "role": "system",
            "content": (
                "Кратко опиши упоминание бренда DDVB в 2-3 предложениях на русском. "
                "Укажи контекст упоминания и ключевую информацию."
            ),
        },
        {"role": "user", "content": f"Заголовок: {title}\nОписание: {snippet}"},
    ]

    try:
        response = client.chat.completions.create(
            model=f"gpt://{YC_FOLDER_ID}/yandexgpt-lite/latest",
            messages=messages,
            temperature=0.3,
            max_tokens=200,
        )
        return (response.choices[0].message.content or "").strip()

    except Exception as e:
        print(f"[ERROR] Summarization failed: {e}")
        return snippet[:200]
