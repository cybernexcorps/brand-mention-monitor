"""Supabase client — settings, deduplication, and mention storage."""

import sys
import io
import logging

# Windows UTF-8 stdout fix
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from supabase import create_client

from config import (
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
    DEFAULT_TARGET_DOMAINS,
    DEFAULT_SEARCH_QUERIES,
    DEFAULT_EXCLUDE_DOMAINS,
)

logger = logging.getLogger("brand-mention-monitor")


def get_client():
    """Create and return a Supabase client."""
    if not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY is empty — Supabase operations will be skipped")
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def load_settings() -> dict:
    """
    Read mention_settings table and return a config dict.
    Falls back to defaults from config.py if Supabase is unavailable.
    """
    defaults = {
        "target_domains": DEFAULT_TARGET_DOMAINS,
        "search_queries": DEFAULT_SEARCH_QUERIES,
        "exclude_domains": DEFAULT_EXCLUDE_DOMAINS,
    }

    client = get_client()
    if client is None:
        logger.info("Using default settings (no Supabase connection)")
        return defaults

    try:
        resp = client.table("mention_settings").select("*").execute()
        rows = resp.data
        if not rows:
            logger.info("mention_settings table is empty, using defaults")
            return defaults

        # Build dict from key-value rows
        settings = dict(defaults)  # start with defaults
        for row in rows:
            key = row.get("key") or row.get("setting_key")
            value = row.get("value") or row.get("setting_value")
            if key and value is not None:
                settings[key] = value

        logger.info("Loaded %d settings from Supabase", len(rows))
        return settings

    except Exception as e:
        logger.error("Failed to load settings from Supabase: %s", e)
        return defaults


def get_existing_urls() -> set[str]:
    """Return set of all URLs already stored in brand_mentions table (for dedup)."""
    client = get_client()
    if client is None:
        return set()

    try:
        resp = client.table("brand_mentions").select("url").execute()
        urls = {row["url"] for row in resp.data if row.get("url")}
        logger.info("Loaded %d existing URLs for deduplication", len(urls))
        return urls

    except Exception as e:
        logger.error("Failed to load existing URLs: %s", e)
        return set()


def save_mentions(mentions: list[dict]) -> int:
    """
    Batch upsert mentions into brand_mentions table.
    Returns count of new/updated rows.

    Each mention dict should have:
        url, title, snippet, domain, discovery_query, relevance
    """
    client = get_client()
    if client is None:
        logger.warning("Cannot save mentions — no Supabase connection")
        return 0

    if not mentions:
        return 0

    rows = []
    for m in mentions:
        rows.append({
            "url": m["url"],
            "title": m.get("title", ""),
            "snippet": m.get("snippet", ""),
            "source_domain": m.get("domain", ""),
            "discovery_query": m.get("discovery_query", ""),
            "relevance_label": m.get("relevance", "relevant"),
            "discovery_source": m.get("discovery_source", "yandex_search_api"),
            "summary": m.get("summary", ""),
        })

    try:
        resp = (
            client.table("brand_mentions")
            .upsert(rows, on_conflict="url")
            .execute()
        )
        count = len(resp.data) if resp.data else 0
        logger.info("Saved %d mentions to Supabase", count)
        return count

    except Exception as e:
        logger.error("Failed to save mentions: %s", e)
        return 0


def get_originating_publications() -> list[dict]:
    """Return all rows from originating_publications table."""
    client = get_client()
    if client is None:
        return []

    try:
        resp = client.table("originating_publications").select("*").execute()
        logger.info("Loaded %d originating publications", len(resp.data))
        return resp.data

    except Exception as e:
        logger.error("Failed to load originating publications: %s", e)
        return []
