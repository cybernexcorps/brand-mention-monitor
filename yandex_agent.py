"""Yandex AI Studio SDK — generative search for brand mention discovery."""

import logging
import time
from urllib.parse import urlparse

from yandex_ai_studio_sdk import AIStudio

from config import (
    YC_API_KEY,
    YC_FOLDER_ID,
    DEFAULT_SEARCH_QUERIES,
)

logger = logging.getLogger("brand-mention-monitor")

_MAX_RETRIES = 2
_BACKOFF_SECONDS = 3


def _get_sdk() -> AIStudio:
    """Create AI Studio SDK client."""
    return AIStudio(folder_id=YC_FOLDER_ID, auth=YC_API_KEY)


def search_and_classify(
    brand_queries: list[str],
    date_from: str | None = None,
) -> list[dict]:
    """
    Use Yandex AI Studio generative search to discover brand mentions.

    Generative search combines Yandex Search + AI analysis in a single call:
    the model searches the web, reads full page content, and returns an
    AI-synthesized answer with source URLs.

    Args:
        brand_queries: Brand name variants, e.g. ['"DDVB"', '"ДДВБ"']
        date_from: ISO date string for recency filter, e.g. "2026-03-19"

    Returns:
        List of mention dicts: {url, title, domain, snippet, summary,
        relevance, discovery_query, discovery_source}
    """
    if not YC_API_KEY:
        logger.warning("YC_API_KEY not set — skipping generative search")
        return []

    sdk = _get_sdk()
    all_mentions: list[dict] = []

    # Build search filters
    search_filters = []
    if date_from:
        search_filters.append({"date": f">{date_from.replace('-', '')}"})

    for query in brand_queries:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                logger.info(
                    "Generative search: query=%s (attempt %d/%d)",
                    query, attempt + 1, _MAX_RETRIES + 1,
                )

                search = sdk.search_api.generative(
                    search_filters=search_filters if search_filters else None,
                )

                prompt = (
                    f"Найди все упоминания бренда {query} "
                    f"(DDVB — брендинговое агентство полного цикла, Москва, "
                    f"специализация: стратегия бренда, дизайн упаковки, айдентика, нейминг) "
                    f"в российских онлайн-СМИ, отраслевых порталах и бизнес-изданиях. "
                    f"ВАЖНО: DDVB — это именно брендинговое агентство, а НЕ маркировка "
                    f"автомобильных двигателей VAG/Audi. Игнорируй автозапчасти. "
                    f"Перечисли все найденные статьи с заголовками и URL."
                )

                result = search.run(prompt, timeout=120)

                summary_text = result.text or ""
                sources = result.sources or []

                logger.info(
                    "Generative search returned %d sources for %s",
                    len(sources), query,
                )

                used_count = 0
                for source in sources:
                    url = getattr(source, "url", "")
                    title = getattr(source, "title", "")
                    used = getattr(source, "used", False)

                    if not url:
                        continue

                    # Only include sources the AI actually cited about DDVB.
                    # Unused sources are tangential context, often unrelated.
                    if not used:
                        logger.debug("Skipping unused source: %s", url)
                        continue

                    used_count += 1
                    domain = urlparse(url).netloc.replace("www.", "")

                    all_mentions.append({
                        "url": url,
                        "title": title,
                        "domain": domain,
                        "snippet": "",
                        "summary": summary_text[:200],
                        "relevance": "relevant",
                        "discovery_query": f"{query} (generative-search)",
                        "discovery_source": "ai_studio_generative",
                    })

                logger.info(
                    "  %d used / %d total sources for %s",
                    used_count, len(sources), query,
                )

                break  # Success — exit retry loop

            except Exception as e:
                logger.error(
                    "Generative search failed for %s (attempt %d): %s",
                    query, attempt + 1, e,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(_BACKOFF_SECONDS)
                else:
                    logger.error("Exhausted retries for query %s", query)

        time.sleep(1)  # Rate limit between queries

    logger.info("Generative search total: %d mentions", len(all_mentions))
    return all_mentions
