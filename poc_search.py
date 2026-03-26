#!/usr/bin/env python3
"""
PoC: Validate that Yandex Search API can discover DDVB brand mentions.

Run: python poc_search.py
"""

import sys
import io
import time
from urllib.parse import urlparse

# Fix Windows encoding for Cyrillic
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from config import (
    DEFAULT_EXCLUDE_DOMAINS,
    DEFAULT_SEARCH_QUERIES,
    DEFAULT_TARGET_DOMAINS,
    YANDEX_RATE_LIMIT_SECONDS,
)
from yandex_ai import classify_relevance, search_web


# Domains that are DDVB's own resources (not third-party mentions)
OWN_DOMAINS = {
    "ddvb.ru", "www.ddvb.ru", "ddvb.tech", "www.ddvb.tech",
    "t.me", "vk.com", "instagram.com",
    "yandex.ru", "google.com",  # search engine results pages
}


def deduplicate(results: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for r in results:
        url = r["url"].rstrip("/").split("?")[0]  # normalize
        if url not in seen:
            seen.add(url)
            unique.append(r)
    return unique


def filter_own_and_excluded(results: list[dict], exclude: list[str]) -> list[dict]:
    """Remove DDVB's own sites and excluded domains."""
    exclude_set = set(exclude) | OWN_DOMAINS
    return [
        r for r in results
        if r["domain"].replace("www.", "") not in exclude_set
    ]


def main():
    print("=" * 60)
    print("DDVB Brand Mention Monitor — PoC Search")
    print("=" * 60)

    all_results = []

    # --- Batch A: Domain-restricted search ---
    print(f"\n📡 Batch A: Target domains: {DEFAULT_TARGET_DOMAINS}")
    for query in DEFAULT_SEARCH_QUERIES:
        print(f"  Searching: {query} on {DEFAULT_TARGET_DOMAINS}")
        results = search_web(query, site_filter=DEFAULT_TARGET_DOMAINS)
        print(f"  → {len(results)} results")
        for r in results:
            r["discovery_query"] = f"{query} (domain-restricted)"
        all_results.extend(results)
        time.sleep(YANDEX_RATE_LIMIT_SECONDS)

    # --- Batch B: Broad search ---
    print(f"\n🌐 Batch B: Broad web search")
    for query in DEFAULT_SEARCH_QUERIES:
        print(f"  Searching: {query} (all web)")
        results = search_web(query)
        print(f"  → {len(results)} results")
        for r in results:
            r["discovery_query"] = f"{query} (broad)"
        all_results.extend(results)
        time.sleep(YANDEX_RATE_LIMIT_SECONDS)

    # --- Deduplicate ---
    before = len(all_results)
    all_results = deduplicate(all_results)
    print(f"\n🔄 Deduplicated: {before} → {len(all_results)}")

    # --- Filter own + excluded domains ---
    before = len(all_results)
    all_results = filter_own_and_excluded(all_results, DEFAULT_EXCLUDE_DOMAINS)
    print(f"🚫 Filtered own/excluded: {before} → {len(all_results)}")

    if not all_results:
        print("\n⚠️  No third-party results found.")
        return

    # --- Print all results before classification ---
    print(f"\n📋 Results to classify ({len(all_results)}):")
    for i, r in enumerate(all_results):
        print(f"  [{i+1}] {r['domain']}: {r['title'][:70]}")

    # --- Classify relevance ---
    print(f"\n🏷️  Classifying relevance...")
    relevant = []
    for i, r in enumerate(all_results):
        label = classify_relevance(r["title"], r["snippet"])
        r["relevance"] = label
        icon = "✅" if label == "relevant" else "❌"
        print(f"  [{i+1}/{len(all_results)}] {icon} {r['domain']}: {r['title'][:60]}")
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
        print("\n✅ RELEVANT THIRD-PARTY MENTIONS:")
        for r in relevant:
            print(f"\n  📰 {r['title']}")
            print(f"     URL:    {r['url']}")
            print(f"     Domain: {r['domain']}")
            print(f"     Query:  {r.get('discovery_query', '?')}")
            if r["snippet"]:
                print(f"     Snippet: {r['snippet'][:200]}")

    # Domain breakdown
    domains = {}
    for r in relevant:
        d = r["domain"]
        domains[d] = domains.get(d, 0) + 1
    if domains:
        print("\n📊 DOMAIN BREAKDOWN:")
        for domain, count in sorted(domains.items(), key=lambda x: -x[1]):
            print(f"   {domain}: {count}")

    print("\n✅ PoC complete.")


if __name__ == "__main__":
    main()
