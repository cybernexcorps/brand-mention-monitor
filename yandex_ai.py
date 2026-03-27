"""Yandex Cloud client — Search API v2 + YandexGPT classification."""

import base64
import json
import logging
import re
import time
from urllib.parse import urlparse

import httpx
from openai import OpenAI

from config import YC_API_KEY, YC_FOLDER_ID, YANDEX_RATE_LIMIT_SECONDS, SEARCH_RESULTS_PER_PAGE

logger = logging.getLogger("brand-mention-monitor")

# --- HTTP client ---

_http = httpx.Client(
    headers={"Authorization": f"Api-Key {YC_API_KEY}"},
    timeout=60.0,
)

SEARCH_API_URL = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
OPERATIONS_URL = "https://operation.api.cloud.yandex.net/operations"

# Retry settings
_RETRY_STATUSES = {403, 429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_SECONDS = [2, 4, 8]


# --- Yandex Search API v2 ---

def search_web(
    query: str,
    site_filter: list[str] | None = None,
    max_results: int = 50,
    date_from: str | None = None,
) -> list[dict]:
    """
    Search Russian web via Yandex Search API v2 (async).
    Returns list of {url, title, snippet, domain} dicts.

    Retries up to 3 times on 403/429/5xx with exponential backoff.

    Args:
        query: Search query text
        site_filter: Optional list of domains to restrict search (e.g. ["retail.ru"])
        max_results: Max results to return
        date_from: ISO date string for recency filter (e.g. "2026-03-19")
    """
    # Build query with site: filter and date restriction
    full_query = query
    if site_filter:
        site_clause = " | ".join(f"site:{d}" for d in site_filter)
        full_query = f"({query}) ({site_clause})"
    if date_from:
        full_query = f"{full_query} date:>{date_from.replace('-', '')}"

    body = {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": full_query,
            "familyMode": "FAMILY_MODE_NONE",
        },
        "sortSpec": {
            "sortMode": "SORT_MODE_BY_TIME",
            "sortOrder": "SORT_ORDER_DESC",
        },
        "groupSpec": {
            "groupsOnPage": SEARCH_RESULTS_PER_PAGE,
            "docsInGroup": 1,
        },
        "maxPassages": 2,
        "region": "225",  # Russia
        "folderId": YC_FOLDER_ID,
    }
    # Native Yandex date filter — more reliable than date: operator in query
    if date_from:
        body["period"] = "PERIOD_2_WEEKS"

    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        try:
            # Submit async search
            resp = _http.post(SEARCH_API_URL, json=body)

            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                wait = _BACKOFF_SECONDS[attempt]
                logger.warning(
                    "Search API returned %d for '%s', retrying in %ds (attempt %d/%d)",
                    resp.status_code, query, wait, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            operation = resp.json()
            operation_id = operation["id"]
            logger.info("Search submitted: operation=%s query='%s'", operation_id, query)

            # Poll for results (max 30 seconds)
            for _ in range(15):
                time.sleep(2)
                poll = _http.get(f"{OPERATIONS_URL}/{operation_id}")
                poll.raise_for_status()
                result = poll.json()
                if result.get("done"):
                    parsed = _parse_search_xml(result, max_results)
                    logger.info("Search completed: %d results for '%s'", len(parsed), query)
                    return parsed

            logger.warning("Search timed out for query: %s", query)
            return []

        except httpx.HTTPStatusError as e:
            last_error = e
            if e.response.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                wait = _BACKOFF_SECONDS[attempt]
                logger.warning(
                    "Search API HTTP %d for '%s', retrying in %ds (attempt %d/%d)",
                    e.response.status_code, query, wait, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            logger.error("Search failed for '%s': %s", query, e)
            return []

        except Exception as e:
            last_error = e
            logger.error("Search failed for '%s': %s", query, e)
            return []

    logger.error("Search exhausted retries for '%s': %s", query, last_error)
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
        modtime_match = re.search(r'modtime="(\d{8})T', group)

        if not url_match:
            continue

        url = url_match.group(1)
        title = _clean_html(title_match.group(1)) if title_match else ""
        domain = domain_match.group(1).replace("www.", "") if domain_match else urlparse(url).netloc
        snippet = " ".join(_clean_html(p) for p in passage_matches[:2])
        # modtime format: YYYYMMDDTHHMMSS — extract date part
        modtime = modtime_match.group(1) if modtime_match else ""

        results.append({
            "url": url,
            "title": title,
            "snippet": snippet,
            "modtime": modtime,
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
    Classify whether a search result is a genuine editorial DDVB brand mention.
    Returns 'relevant' or 'irrelevant'.
    """
    client = get_llm_client()
    text = f"Заголовок: {title}\nОписание: {snippet}"

    messages = [
        {
            "role": "system",
            "content": (
                "Ты — строгий классификатор упоминаний бренда DDVB "
                "(брендинговое агентство полного цикла из Москвы). "
                "DDVB специализируется на стратегии бренда, дизайне упаковки, айдентике. "
                "Отвечай relevant ТОЛЬКО если ВСЕ условия выполнены: "
                "1) Слово 'DDVB' или 'ДДВБ' ЯВНО присутствует в заголовке или описании; "
                "2) Речь идёт именно о брендинговом агентстве DDVB, его проектах, "
                "сотрудниках (Мария Архангельская, Леонид Фейгин) или клиентах; "
                "3) Это НОВОСТЬ, СТАТЬЯ, КЕЙС или РЕЙТИНГ текущего года (2026). "
                "Отвечай irrelevant если ЛЮБОЕ из: "
                "- DDVB НЕ упоминается явно в тексте (просто тема 'брендинг' недостаточно); "
                "- Страница о ДРУГОЙ компании (даже если тема — брендинг/дизайн); "
                "- DDVB — это маркировка двигателя VAG/Audi (автозапчасти); "
                "- Это каталог, агрегатор, список ссылок без редакционного контента; "
                "- DDVB упоминается только в боковой панели, футере или списке 'похожие'; "
                "- Публикация явно старая (2024 год и ранее). "
                "Ответь ОДНИМ словом: relevant или irrelevant."
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
        label = "relevant" if "relevant" in answer else "irrelevant"
        logger.debug("Classified '%s' as %s", title[:60], label)
        return label

    except Exception as e:
        logger.error("Classification failed: %s", e)
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
        summary = (response.choices[0].message.content or "").strip()
        logger.debug("Summarized mention: '%s'", title[:60])
        return summary

    except Exception as e:
        logger.error("Summarization failed: %s", e)
        return snippet[:200]
