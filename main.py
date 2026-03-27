#!/usr/bin/env python3
"""Brand Mention Monitor — production collection pipeline."""

import sys
import io
import argparse
import logging
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

    # --- 5. Merge + Deduplicate + Date validation ---
    # Agent results first (higher quality — pre-classified with summaries)
    all_results = agent_results + api_results
    total_raw = len(all_results)
    logger.info("Total raw results (agent + API): %d", total_raw)

    all_results = deduplicate(all_results, existing_urls)
    after_dedup = len(all_results)
    logger.info("After dedup: %d", after_dedup)

    # Filter by publication date — reject anything older than search window
    date_from_int = int(date_from.replace("-", ""))  # e.g. 20260320
    before_date_filter = len(all_results)
    date_filtered = []
    for r in all_results:
        modtime = r.get("modtime", "")
        if modtime and len(modtime) == 8:
            try:
                if int(modtime) < date_from_int:
                    logger.debug(
                        "Rejected old content (modtime=%s): %s",
                        modtime, r.get("title", "")[:60],
                    )
                    continue
            except ValueError:
                pass
        date_filtered.append(r)
    all_results = date_filtered
    logger.info(
        "After date filter: %d (rejected %d old)",
        len(all_results), before_date_filter - len(all_results),
    )

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

    # --- 7. Classify Search API results with YandexGPT ---
    # Generative search results are already pre-classified (high quality).
    # Search API results need AI classification to filter noise
    # (e.g., "DDVB" matching car engine codes, random string fragments).
    logger.info("Classifying Search API results...")
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
