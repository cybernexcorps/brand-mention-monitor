#!/usr/bin/env python3
"""
PoC: Validate that Yandex AI Studio can discover DDVB brand mentions.

Run: python poc_search.py

Expected output:
- List of URLs where DDVB is mentioned
- Relevance classification for each
- Summary of findings
"""

import asyncio
import time
from urllib.parse import urlparse

from config import (
    DEFAULT_EXCLUDE_DOMAINS,
    DEFAULT_SEARCH_QUERIES,
    DEFAULT_TARGET_DOMAINS,
    YANDEX_RATE_LIMIT_SECONDS,
)
from yandex_ai import classify_relevance, search_with_web


def deduplicate(results: list[dict]) -> list[dict]:
    """Remove duplicate URLs."""
    seen = set()
    unique = []
    for r in results:
        url = r["url"].rstrip("/")
        if url not in seen:
            seen.add(url)
            unique.append(r)
    return unique


def filter_excluded_domains(results: list[dict], exclude: list[str]) -> list[dict]:
    """Remove results from excluded domains (e.g., the originating publication)."""
    return [r for r in results if r["domain"] not in exclude]


def main():
    print("=" * 60)
    print("DDVB Brand Mention Monitor — PoC Search")
    print("=" * 60)

    all_results = []

    # --- Batch A: Domain-restricted search ---
    print(f"\n📡 Batch A: Searching target domains: {DEFAULT_TARGET_DOMAINS}")
    for query in DEFAULT_SEARCH_QUERIES:
        print(f"  Query: {query}")
        results = search_with_web(query, allowed_domains=DEFAULT_TARGET_DOMAINS)
        print(f"  Found: {len(results)} results")
        all_results.extend(results)
        time.sleep(YANDEX_RATE_LIMIT_SECONDS)

    # --- Batch B: Broad search (no domain restriction) ---
    print(f"\n🌐 Batch B: Broad web search")
    for query in DEFAULT_SEARCH_QUERIES:
        print(f"  Query: {query}")
        results = search_with_web(query)
        print(f"  Found: {len(results)} results")
        all_results.extend(results)
        time.sleep(YANDEX_RATE_LIMIT_SECONDS)

    # --- Deduplicate ---
    before_dedup = len(all_results)
    all_results = deduplicate(all_results)
    print(f"\n🔄 Deduplicated: {before_dedup} → {len(all_results)}")

    # --- Filter excluded domains ---
    before_filter = len(all_results)
    all_results = filter_excluded_domains(all_results, DEFAULT_EXCLUDE_DOMAINS)
    print(f"🚫 Filtered excluded domains: {before_filter} → {len(all_results)}")

    if not all_results:
        print("\n⚠️  No results found. Check API key and search queries.")
        return

    # --- Classify relevance ---
    print(f"\n🏷️  Classifying relevance ({len(all_results)} items)...")
    relevant = []
    for i, r in enumerate(all_results):
        text = f"{r['title']} {r['snippet']}"
        label = classify_relevance(text)
        r["relevance"] = label
        status = "✅" if label == "relevant" else "❌"
        print(f"  [{i+1}/{len(all_results)}] {status} {r['domain']}: {r['title'][:60]}")
        if label == "relevant":
            relevant.append(r)
        time.sleep(YANDEX_RATE_LIMIT_SECONDS)

    # --- Summary ---
    print("\n" + "=" * 60)
    print(f"📊 RESULTS SUMMARY")
    print(f"   Total found:    {len(all_results)}")
    print(f"   Relevant:       {len(relevant)}")
    print(f"   Irrelevant:     {len(all_results) - len(relevant)}")
    print("=" * 60)

    if relevant:
        print("\n✅ RELEVANT MENTIONS:")
        for r in relevant:
            print(f"\n  📰 {r['title']}")
            print(f"     URL:    {r['url']}")
            print(f"     Domain: {r['domain']}")
            print(f"     Query:  {r['discovery_query']}")
            if r['snippet']:
                print(f"     Snippet: {r['snippet'][:150]}...")

    # --- Domains breakdown ---
    domains = {}
    for r in relevant:
        domains[r["domain"]] = domains.get(r["domain"], 0) + 1
    if domains:
        print("\n📊 DOMAIN BREAKDOWN:")
        for domain, count in sorted(domains.items(), key=lambda x: -x[1]):
            print(f"   {domain}: {count}")

    print("\n✅ PoC complete.")


if __name__ == "__main__":
    main()
