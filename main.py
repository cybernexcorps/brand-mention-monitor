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

from config import (
    BLOCKED_DOMAINS,
    DEFAULT_EXCLUDE_DOMAINS,
    DEFAULT_RECIPIENTS,
    DEFAULT_SEARCH_QUERIES,
    DEFAULT_TARGET_DOMAINS,
    YANDEX_RATE_LIMIT_SECONDS,
)
from email_digest import send_digest, send_empty_notification
from supabase_client import get_existing_urls, load_settings, save_mentions
from yandex_ai import classify_relevance, search_web, summarize_mention

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


def filter_blocked(results: list[dict], exclude_domains: list[str]) -> list[dict]:
    """Remove blocked domains and explicitly excluded domains."""
    exclude_set = BLOCKED_DOMAINS | set(exclude_domains)
    return [
        r for r in results
        if r["domain"].replace("www.", "") not in exclude_set
    ]


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

    # --- 3. Search ---
    all_results: list[dict] = []

    # Batch A: domain-restricted search
    logger.info("Batch A: searching target domains %s", target_domains)
    for query in search_queries:
        logger.info("  Query: %s on %s", query, target_domains)
        results = search_web(query, site_filter=target_domains)
        for r in results:
            r["discovery_query"] = f"{query} (domain-restricted)"
        all_results.extend(results)
        time.sleep(YANDEX_RATE_LIMIT_SECONDS)

    # Batch B: broad web search
    logger.info("Batch B: broad web search")
    for query in search_queries:
        logger.info("  Query: %s (broad)", query)
        results = search_web(query)
        for r in results:
            r["discovery_query"] = f"{query} (broad)"
        all_results.extend(results)
        time.sleep(YANDEX_RATE_LIMIT_SECONDS)

    total_searched = len(all_results)
    logger.info("Total raw results: %d", total_searched)

    # --- 4. Deduplicate ---
    all_results = deduplicate(all_results, existing_urls)
    after_dedup = len(all_results)
    logger.info("After dedup: %d", after_dedup)

    # --- 5. Filter blocked domains ---
    all_results = filter_blocked(all_results, exclude_domains)
    after_filter = len(all_results)
    logger.info("After blocked domain filter: %d", after_filter)

    if not all_results:
        logger.info("No new results to classify")
        if not dry_run:
            send_empty_notification(DEFAULT_RECIPIENTS)
        return {
            "total_searched": total_searched,
            "after_dedup": after_dedup,
            "after_filter": after_filter,
            "relevant": 0,
            "saved": 0,
        }

    # --- 6. Classify relevance ---
    logger.info("Classifying %d results...", len(all_results))
    relevant: list[dict] = []
    for i, r in enumerate(all_results):
        label = classify_relevance(r["title"], r["snippet"])
        r["relevance"] = label
        if label == "relevant":
            relevant.append(r)
            logger.info(
                "  [%d/%d] RELEVANT: %s — %s",
                i + 1, len(all_results), r["domain"], r["title"][:60],
            )
        else:
            logger.debug(
                "  [%d/%d] irrelevant: %s — %s",
                i + 1, len(all_results), r["domain"], r["title"][:60],
            )
        time.sleep(YANDEX_RATE_LIMIT_SECONDS)

    logger.info("Relevant mentions: %d / %d", len(relevant), len(all_results))

    # --- 7. Summarize relevant mentions ---
    if relevant:
        logger.info("Summarizing %d relevant mentions...", len(relevant))
        for r in relevant:
            r["summary"] = summarize_mention(r["title"], r["snippet"])
            time.sleep(YANDEX_RATE_LIMIT_SECONDS)

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
        "total_searched": total_searched,
        "after_dedup": after_dedup,
        "after_filter": after_filter,
        "relevant": len(relevant),
        "saved": saved,
    }

    logger.info("=" * 50)
    logger.info("PIPELINE SUMMARY")
    logger.info("  Total searched:    %d", summary["total_searched"])
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
