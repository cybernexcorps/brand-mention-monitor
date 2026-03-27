#!/usr/bin/env python3
"""Brand Mention Monitor — production collection pipeline."""

import sys
import io
import argparse
import logging
import re
import time

# Windows UTF-8 stdout fix
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from datetime import datetime, timedelta

from config import (
    BLOCKED_DOMAINS,
    DEFAULT_EXCLUDE_DOMAINS,
    DEFAULT_RECIPIENTS,
    DEFAULT_SEARCH_QUERIES,
    DEFAULT_TARGET_DOMAINS,
    SEARCH_DATE_RESTRICT_DAYS,
    YANDEX_RATE_LIMIT_SECONDS,
)
from email_digest import send_digest, send_empty_notification
from supabase_client import get_existing_urls, load_settings, save_mentions
from yandex_agent import search_and_classify as agent_search
from yandex_ai import classify_relevance, search_web

logger = logging.getLogger("brand-mention-monitor")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Year extraction patterns for publication date heuristics
_URL_DATE_PATTERNS = [
    re.compile(r'/(\d{4})/\d{1,2}/'),       # /2024/08/
    re.compile(r'/(\d{4})-\d{2}'),           # /2024-03-26
    re.compile(r'[/-](\d{4})[/-]'),          # generic /2024/ or -2024-
]
_TEXT_YEAR_PATTERN = re.compile(r'\b(20[12]\d)\b')  # matches 2010-2029


def _extract_publication_year(url: str, title: str, snippet: str) -> int | None:
    """Extract publication year from URL path or title/snippet text."""
    current_year = datetime.now().year

    # Check URL first (most reliable — news sites embed dates in paths)
    for pattern in _URL_DATE_PATTERNS:
        match = pattern.search(url)
        if match:
            year = int(match.group(1))
            if 2010 <= year <= current_year:
                return year

    # Check title and snippet for year mentions
    text = f"{title} {snippet}"
    years = [int(y) for y in _TEXT_YEAR_PATTERN.findall(text) if 2010 <= int(y) <= current_year]
    if years:
        return max(years)  # most recent year mentioned

    return None


def _verify_page_mentions_brand(url: str, timeout: float = 10.0) -> bool:
    """Fetch a URL and check if DDVB/ДДВБ appears in the page content."""
    import httpx

    try:
        resp = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "DDVB-BrandMonitor/1.0"},
        )
        if resp.status_code != 200:
            logger.debug("Page fetch failed (%d): %s", resp.status_code, url)
            return True  # fail-open: don't reject on fetch errors

        text = resp.text.lower()
        return "ddvb" in text or "ддвб" in text

    except Exception as e:
        logger.debug("Page fetch error for %s: %s", url, e)
        return True  # fail-open


def _normalize_url(url: str) -> str:
    """Normalize URL for deduplication: strip trailing slash, query params, www."""
    url = url.rstrip("/").split("?")[0].split("#")[0]
    url = url.replace("://www.", "://")
    return url


def deduplicate(results: list[dict], existing_urls: set[str]) -> list[dict]:
    """Remove duplicate URLs (internal + already stored in Supabase)."""
    normalized_existing = {_normalize_url(u) for u in existing_urls}
    seen: set[str] = set()
    unique: list[dict] = []

    for r in results:
        norm = _normalize_url(r["url"])
        if norm not in seen and norm not in normalized_existing:
            seen.add(norm)
            unique.append(r)

    return unique


# TLDs that can contain DDVB editorial mentions (Russian-language media)
ALLOWED_TLDS = {
    "ru", "su", "by", "kz", "uz", "ua", "me", "com", "net", "org",
    "io", "info", "agency", "tech", "asia", "pro", "one", "app",
}


def filter_blocked(results: list[dict], exclude_domains: list[str]) -> list[dict]:
    """Remove blocked domains, excluded domains, and non-Russian TLDs."""
    exclude_set = BLOCKED_DOMAINS | set(exclude_domains)
    filtered = []
    for r in results:
        domain = r["domain"].replace("www.", "")
        if domain in exclude_set:
            continue
        # Block foreign TLDs — DDVB mentions only appear on Russian-zone sites
        tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
        if tld not in ALLOWED_TLDS:
            logger.debug("Blocked foreign TLD: %s", domain)
            continue
        filtered.append(r)
    return filtered


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(dry_run: bool = False, verbose: bool = False) -> dict:
    """
    Execute the full brand mention collection pipeline.

    Returns summary dict with counts.
    """
    # --- 1. Load settings ---
    logger.info("Loading settings...")
    settings = load_settings()
    target_domains = settings.get("target_domains", DEFAULT_TARGET_DOMAINS)
    search_queries = settings.get("search_queries", DEFAULT_SEARCH_QUERIES)
    exclude_domains = settings.get("exclude_domains", DEFAULT_EXCLUDE_DOMAINS)

    logger.info(
        "Settings: %d target domains, %d queries, %d excluded domains",
        len(target_domains), len(search_queries), len(exclude_domains),
    )

    # --- 2. Get existing URLs for dedup ---
    existing_urls = get_existing_urls() if not dry_run else set()
    logger.info("Existing URLs in DB: %d", len(existing_urls))

    # --- 3. AI Studio generative search (primary — search + AI analysis) ---
    date_from = (
        datetime.now() - timedelta(days=SEARCH_DATE_RESTRICT_DAYS)
    ).strftime("%Y-%m-%d")

    logger.info("Stage 3: Generative search (date_from=%s)...", date_from)
    agent_results = agent_search(search_queries, date_from=date_from)
    logger.info("Generative search found %d mentions", len(agent_results))

    # --- 4. Search API v2 fallback (breadth — with date filter + pagination) ---
    # Only use Latin "DDVB" for Search API — the Cyrillic "ДДВБ" query matches
    # VAG engine codes and random text fragments, producing massive noise.
    # Generative search (stage 3) handles "ДДВБ" well because the AI filters contextually.
    api_queries = [q for q in search_queries if "ДДВБ" not in q]
    if not api_queries:
        api_queries = ['"DDVB"']  # Always have at least one query

    logger.info("Stage 4: Search API v2 fallback (queries: %s)...", api_queries)
    api_results: list[dict] = []

    # Batch A: domain-restricted search
    for query in api_queries:
        logger.info("  Query: %s on %s", query, target_domains)
        results = search_web(query, site_filter=target_domains, date_from=date_from)
        for r in results:
            r["discovery_query"] = f"{query} (domain-restricted)"
            r["discovery_source"] = "yandex_search_api"
        api_results.extend(results)
        time.sleep(YANDEX_RATE_LIMIT_SECONDS)

    # Batch B: broad web search
    for query in api_queries:
        logger.info("  Query: %s (broad)", query)
        results = search_web(query, date_from=date_from)
        for r in results:
            r["discovery_query"] = f"{query} (broad)"
            r["discovery_source"] = "yandex_search_api"
        api_results.extend(results)
        time.sleep(YANDEX_RATE_LIMIT_SECONDS)

    logger.info("Search API found %d raw results", len(api_results))

    # --- 5. Merge + Deduplicate ---
    # Agent results first (higher quality — pre-classified with summaries)
    all_results = agent_results + api_results
    total_raw = len(all_results)
    logger.info("Total raw results (agent + API): %d", total_raw)

    all_results = deduplicate(all_results, existing_urls)
    after_dedup = len(all_results)
    logger.info("After dedup: %d", after_dedup)

    # --- 6. Filter blocked domains ---
    all_results = filter_blocked(all_results, exclude_domains)
    after_filter = len(all_results)
    logger.info("After blocked domain filter: %d", after_filter)

    if not all_results:
        logger.info("No new results after filtering")
        if not dry_run:
            send_empty_notification(DEFAULT_RECIPIENTS)
        return {
            "total_raw": total_raw,
            "agent_found": len(agent_results),
            "api_found": len(api_results),
            "after_dedup": after_dedup,
            "after_filter": after_filter,
            "relevant": 0,
            "saved": 0,
        }

    # --- 7. Hard gate: DDVB must appear in title or snippet ---
    # This is the most reliable filter — if the brand name isn't in the text,
    # it's not a brand mention regardless of what the AI classifier says.
    brand_terms = {"ddvb", "ддвб"}
    before_brand_gate = len(all_results)
    brand_gated = []
    for r in all_results:
        text = f"{r.get('title', '')} {r.get('snippet', '')}".lower()
        if any(term in text for term in brand_terms):
            brand_gated.append(r)
        else:
            logger.debug("Brand gate rejected: %s — %s", r["domain"], r["title"][:60])
    all_results = brand_gated
    logger.info(
        "After brand gate: %d (rejected %d without DDVB in text)",
        len(all_results), before_brand_gate - len(all_results),
    )

    # --- 8. Year filter: reject content from previous years ---
    current_year = datetime.now().year
    before_year_filter = len(all_results)
    year_filtered = []
    for r in all_results:
        year = _extract_publication_year(
            r.get("url", ""), r.get("title", ""), r.get("snippet", ""),
        )
        if year is not None and year < current_year:
            logger.info(
                "Rejected old content (year=%d): %s — %s",
                year, r["domain"], r["title"][:60],
            )
            continue
        year_filtered.append(r)
    all_results = year_filtered
    logger.info(
        "After year filter: %d (rejected %d old)",
        len(all_results), before_year_filter - len(all_results),
    )

    # --- 9. Page verification: fetch actual page and confirm DDVB is mentioned ---
    # Search snippets can contain "DDVB" from Yandex context highlighting
    # even when the actual page doesn't mention DDVB at all.
    before_page_verify = len(all_results)
    page_verified = []
    for r in all_results:
        if r.get("discovery_source") == "ai_studio_generative":
            # Generative search already reads full page content
            page_verified.append(r)
            continue
        url = r.get("url", "")
        if _verify_page_mentions_brand(url):
            page_verified.append(r)
        else:
            logger.info(
                "Page verification FAILED: %s — %s",
                r["domain"], r["title"][:60],
            )
    all_results = page_verified
    logger.info(
        "After page verification: %d (rejected %d)",
        len(all_results), before_page_verify - len(all_results),
    )

    # --- 10. Classify remaining Search API results with YandexGPT ---
    logger.info("Classifying %d results...", len(all_results))
    relevant: list[dict] = []
    for r in all_results:
        r.setdefault("discovery_source", "yandex_search_api")
        r.setdefault("summary", r.get("snippet", "")[:200])

        if r.get("discovery_source") == "ai_studio_generative":
            # Already classified by generative search AI
            r["relevance"] = "relevant"
            relevant.append(r)
        else:
            # Classify with YandexGPT Lite
            label = classify_relevance(r["title"], r["snippet"])
            r["relevance"] = label
            if label == "relevant":
                relevant.append(r)
                logger.info("  RELEVANT: %s — %s", r["domain"], r["title"][:60])
            else:
                logger.debug("  irrelevant: %s — %s", r["domain"], r["title"][:60])
            time.sleep(YANDEX_RATE_LIMIT_SECONDS)

    logger.info("Relevant mentions: %d / %d", len(relevant), len(all_results))

    for r in relevant:
        logger.info(
            "  [%s] %s — %s",
            r.get("discovery_source", "?")[:5],
            r["domain"],
            r["title"][:60],
        )

    # --- 8. Save to Supabase ---
    saved = 0
    if not dry_run and relevant:
        saved = save_mentions(relevant)
        logger.info("Saved %d mentions to Supabase", saved)
    elif dry_run:
        logger.info("[DRY RUN] Would save %d mentions", len(relevant))

    # --- 9. Send email digest ---
    if not dry_run:
        if relevant:
            send_digest(relevant, DEFAULT_RECIPIENTS)
        else:
            send_empty_notification(DEFAULT_RECIPIENTS)

    # --- Summary ---
    summary = {
        "total_raw": total_raw,
        "agent_found": len(agent_results),
        "api_found": len(api_results),
        "after_dedup": after_dedup,
        "after_filter": after_filter,
        "relevant": len(relevant),
        "saved": saved,
    }

    logger.info("=" * 50)
    logger.info("PIPELINE SUMMARY")
    logger.info("  Agent found:       %d", summary["agent_found"])
    logger.info("  Search API found:  %d", summary["api_found"])
    logger.info("  Total raw:         %d", summary["total_raw"])
    logger.info("  After dedup:       %d", summary["after_dedup"])
    logger.info("  After filter:      %d", summary["after_filter"])
    logger.info("  Relevant:          %d", summary["relevant"])
    logger.info("  Saved:             %d", summary["saved"])
    logger.info("=" * 50)

    return summary


# ---------------------------------------------------------------------------
# Cloud Function handler
# ---------------------------------------------------------------------------

def handler(event, context):
    """Entry point for Cloud Function (e.g. Yandex Cloud Functions)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    summary = run_pipeline(dry_run=False, verbose=False)
    return {"statusCode": 200, "body": summary}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Brand Mention Monitor — production collection pipeline"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and classify but don't save to DB or send email",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging",
    )
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    run_pipeline(dry_run=args.dry_run, verbose=args.verbose)
