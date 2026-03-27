# API / Function Reference — Brand Mention Monitor

This document covers every public function and significant private helper in all six source modules. Functions are listed in call order within each module. All functions are synchronous (no async/await).

---

## main.py

Pipeline orchestration, the 6-layer filter implementation, the CLI entry point, and the Yandex Cloud Functions handler.

---

### `_extract_publication_year(url: str, title: str, snippet: str) -> int | None`

**Visibility:** Module-private (called from `run_pipeline` at Layer 4)

Extracts the likely publication year from a search result using URL path patterns first, then year mentions in title and snippet text.

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `url` | `str` | Full URL of the search result |
| `title` | `str` | Page title text |
| `snippet` | `str` | Snippet / passage text |

**Extraction strategy (in order of reliability):**
1. URL path patterns via `_URL_DATE_PATTERNS`: `/2026/03/`, `/2026-03-26`, `/2026/`, `-2026-`
2. Four-digit year mentions in `f"{title} {snippet}"` — takes the most recent year found (range: 2010 to current year)

**Returns:** `int` — the extracted year, or `None` if no year could be determined. Returns `None` on failure so callers can apply fail-open logic (a result with no extractable year passes the year filter).

**Notes:** Year extraction is a heuristic. News sites reliably embed dates in URL paths (pattern 1). Title and snippet year extraction (pattern 2) is less reliable but catches cases where the URL has no date component.

---

### `_verify_page_mentions_brand(url: str, timeout: float = 10.0) -> bool`

**Visibility:** Module-private (called from `run_pipeline` at Layer 5)

Fetches a candidate URL and verifies that the brand name actually appears in the page HTML. This catches Yandex's context highlighting — the Search API can inject "DDVB" into a snippet through keyword matching even when the actual page does not mention the brand.

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `url` | `str` | — | Full URL to fetch |
| `timeout` | `float` | `10.0` | HTTP request timeout in seconds |

**Implementation:**
- Uses `httpx.get()` with `follow_redirects=True` and `User-Agent: DDVB-BrandMonitor/1.0`
- Checks `"ddvb" in resp.text.lower() or "ддвб" in resp.text.lower()`

**Returns:** `bool` — `True` if brand name found in page or if fetch failed (fail-open); `False` if page was fetched successfully but brand name was absent.

**Fail-open cases:** Non-200 HTTP status, request exception (timeout, connection error), or any other exception — all return `True` so the result is not dropped due to a transient fetch failure.

**Skipped for:** Results with `discovery_source == "ai_studio_generative"`. Generative search already read full page content during retrieval, making re-fetching redundant.

---

### `_normalize_url(url: str) -> str`

**Visibility:** Module-private (used by `deduplicate`)

Normalizes a URL for deduplication by applying three transformations in order:
1. Strip trailing slash
2. Strip query string (`?...`) and hash fragment (`#...`)
3. Replace `://www.` with `://`

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `url` | `str` | Raw URL string as returned by the search API |

**Returns:** `str` — the normalized URL

**Examples:**
```python
_normalize_url("https://www.sostav.ru/article/123/?utm_source=yandex#top")
# → "https://sostav.ru/article/123"

_normalize_url("https://retail.ru/news/456/")
# → "https://retail.ru/news/456"
```

---

### `deduplicate(results: list[dict], existing_urls: set[str]) -> list[dict]`

**Visibility:** Public — Layer 1 of the filter pipeline (called from `run_pipeline`)

Removes duplicate search results using normalized URL comparison. A result is excluded if:
- Its normalized URL was already seen earlier in the current `results` list (intra-session duplicate across agent and API result sets), OR
- Its normalized URL matches any normalized URL in `existing_urls` (already stored in Supabase from a previous run)

First occurrence wins. The merge order (`agent_results + api_results`) means agent results take priority when the same URL appears in both sources.

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `results` | `list[dict]` | Merged list of result dicts (agent first, then api), each with at least a `"url"` key |
| `existing_urls` | `set[str]` | Set of URL strings already present in the Supabase `brand_mentions` table. Pass an empty set for dry-run mode. |

**Returns:** `list[dict]` — filtered list preserving original order, first occurrence of each URL only

---

### `filter_blocked(results: list[dict], exclude_domains: list[str]) -> list[dict]`

**Visibility:** Public — Layer 2 of the filter pipeline (called from `run_pipeline`)

Removes results whose domain appears in the hardcoded `BLOCKED_DOMAINS` set, the runtime `exclude_domains` list, or whose TLD is not in the `ALLOWED_TLDS` allowlist.

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `results` | `list[dict]` | List of result dicts, each with at least a `"domain"` key |
| `exclude_domains` | `list[str]` | Runtime domains to exclude (from `mention_settings` table or `DEFAULT_EXCLUDE_DOMAINS`) |

**Domain matching logic:**
```python
domain = result["domain"].replace("www.", "")
exclude_set = BLOCKED_DOMAINS | set(exclude_domains)
if domain in exclude_set: reject
tld = domain.rsplit(".", 1)[-1]
if tld not in ALLOWED_TLDS: reject
```

**TLD allowlist (`ALLOWED_TLDS` — defined in `main.py`, not `config.py`):**
```python
ALLOWED_TLDS = {
    "ru", "su", "by", "kz", "uz", "ua", "me", "com", "net", "org",
    "io", "info", "agency", "tech", "asia", "pro", "one", "app",
}
```

Foreign TLDs (`.de`, `.uk`, `.shop`, etc.) are rejected because DDVB editorial mentions only appear on Russian-zone sites. This blocks spam and foreign noise without requiring individual domain entries in `BLOCKED_DOMAINS`.

**Returns:** `list[dict]` — filtered list with blocked, excluded, and foreign-TLD domains removed

**Notes:** `BLOCKED_DOMAINS` is a permanent structural filter; `exclude_domains` is a runtime configuration changeable via the `mention_settings` table without redeployment.

---

### `run_pipeline(dry_run: bool = False, verbose: bool = False) -> dict`

**Visibility:** Public (called by `handler` and the CLI `__main__` block)

Executes the complete brand mention collection pipeline with dual-source search and 6-layer filtering.

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`: skips `get_existing_urls()`, skips `save_mentions()`, skips all email sending. Search, filtering, and classification still run normally. |
| `verbose` | `bool` | `False` | Accepted as parameter but not used directly — log level is set by the caller before `run_pipeline` is invoked |

**Returns:** `dict` with the following keys:

| Key | Type | Description |
|---|---|---|
| `agent_found` | `int` | Raw results from AI Studio generative search before any filtering |
| `api_found` | `int` | Raw results from Search API v2 before any filtering |
| `total_raw` | `int` | Combined count: `agent_found + api_found` |
| `after_dedup` | `int` | Count after Layer 1 (URL deduplication) |
| `after_filter` | `int` | Count after Layer 2 (blocked domain + TLD filter) |
| `relevant` | `int` | Count after all 6 layers (classified as relevant) |
| `saved` | `int` | Count actually upserted to Supabase (0 in dry-run mode) |

**Pipeline stages executed:**
1. `load_settings()` — fetch config from Supabase or fall back to `config.py` defaults
2. `get_existing_urls()` — fetch URL set from `brand_mentions` for dedup (skipped in dry-run)
3. `agent_search(search_queries, date_from)` — AI Studio generative search (all queries incl. Cyrillic)
4. `search_web()` × N batches — Search API v2, Latin queries only (Batch A domain-restricted + Batch B broad)
5. Merge (`agent_results + api_results`) + Layer 1: `deduplicate()`
6. Layer 2: `filter_blocked()` — blocked domains + TLD allowlist
7. Layer 3: brand gate — reject results without "ddvb"/"ддвб" in title+snippet
8. Layer 4: year filter — reject pre-current-year results via `_extract_publication_year()`
9. Layer 5: page verification — `_verify_page_mentions_brand()` (skipped for ai_studio_generative)
10. Layer 6: `classify_relevance()` — YandexGPT Lite (skipped for ai_studio_generative)
11. `save_mentions()` — batch upsert to Supabase (skipped in dry-run)
12. `send_digest()` or `send_empty_notification()` — HTML email via SMTP (skipped in dry-run)

**Early exit:** If `filter_blocked()` returns an empty list, the pipeline exits early — sends an empty notification and returns a summary with `relevant=0, saved=0`.

---

### `handler(event, context) -> dict`

**Visibility:** Public (Yandex Cloud Functions entry point)

The function invoked by the Yandex Cloud Functions runtime when the scheduler fires. Configures INFO-level logging and delegates to `run_pipeline(dry_run=False)`.

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `event` | `dict` | Cloud Functions event payload (not inspected) |
| `context` | `object` | Cloud Functions context object (not used) |

**Returns:** `dict` matching the Cloud Functions HTTP response shape:
```python
{
    "statusCode": 200,
    "body": {
        "agent_found": int,
        "api_found": int,
        "total_raw": int,
        "after_dedup": int,
        "after_filter": int,
        "relevant": int,
        "saved": int
    }
}
```

---

## yandex_agent.py

Yandex AI Studio SDK client for generative search. This is the primary search source — it combines web retrieval and AI analysis in a single SDK call.

Module-level constants: `_MAX_RETRIES = 2`, `_BACKOFF_SECONDS = 3`.

---

### `_get_sdk() -> AIStudio`

**Visibility:** Module-private (called by `search_and_classify`)

Creates and returns an AI Studio SDK client.

**Returns:** `AIStudio` instance initialized with `folder_id=YC_FOLDER_ID` and `auth=YC_API_KEY`. The same `YC_API_KEY` used for all other Yandex services is passed directly to the SDK — no separate `AI_STUDIO_API_KEY` is required for the generative search SDK.

---

### `search_and_classify(brand_queries: list[str], date_from: str | None = None) -> list[dict]`

**Visibility:** Public (imported as `agent_search` in `main.py`)

Uses the Yandex AI Studio generative search SDK to discover brand mentions. Generative search combines Yandex web search + AI analysis in a single call: the model searches the web, reads full page content, and returns an AI-synthesized answer with source URLs.

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `brand_queries` | `list[str]` | Brand name variants, e.g. `['"DDVB"', '"ДДВБ"']`. All queries are run including Cyrillic. |
| `date_from` | `str \| None` | ISO date string for recency filter, e.g. `"2026-03-19"`. Converted to `>YYYYMMDD` format for the SDK's `search_filters`. |

**Prompt construction (`yandex_agent.py:69-77`):** The prompt explicitly identifies DDVB as a branding agency and instructs the model to ignore automotive engine code matches ("DDVB" is also a VAG/Audi engine code). This disambiguation is only practical with generative search — the Search API v2 cannot reason about query intent.

**Source filtering:** Only `source.used == True` sources are included in output. Unused sources are tangential context the AI consulted but did not cite — they are skipped with a DEBUG log entry.

**Returns:** `list[dict]` — each dict has:

| Key | Type | Description |
|---|---|---|
| `url` | `str` | Source URL |
| `title` | `str` | Source title |
| `domain` | `str` | Domain with `www.` stripped |
| `snippet` | `str` | Always `""` — generative search does not extract page passages |
| `summary` | `str` | First 200 characters of the AI-synthesized answer text (`result.text`) |
| `relevance` | `str` | Always `"relevant"` — pre-set by the generative model |
| `discovery_query` | `str` | e.g., `'"DDVB" (generative-search)'` |
| `discovery_source` | `str` | Always `"ai_studio_generative"` |

Returns an empty list if `YC_API_KEY` is not set (logs a warning), or if all queries exhaust retries.

**Retry logic:** Up to `_MAX_RETRIES = 2` retries per query with `_BACKOFF_SECONDS = 3` sleep on any exception.

**Rate limiting:** `time.sleep(1)` between queries.

---

## yandex_ai.py

Yandex Search API v2 client (fallback search source) and YandexGPT classification/summarization.

Module-level state: a shared `httpx.Client` instance `_http` is created once at import time with `Authorization: Api-Key {YC_API_KEY}` header and 60-second timeout.

Retry settings: `_RETRY_STATUSES = {403, 429, 500, 502, 503, 504}`, `_MAX_RETRIES = 3`, `_BACKOFF_SECONDS = [2, 4, 8]`.

---

### `search_web(query: str, site_filter: list[str] | None = None, max_results: int = 50, date_from: str | None = None) -> list[dict]`

**Visibility:** Public (called from `main.py`)

Submits an async search to the Yandex Search API v2, polls for results, and returns parsed results.

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `query` | `str` | — | Search query text. Should be quoted for exact phrase matching (e.g., `'"DDVB"'`) |
| `site_filter` | `list[str] \| None` | `None` | List of domains for `site:` filter (Batch A). Generates `site:domain` clauses joined with ` | ` |
| `max_results` | `int` | `50` | Maximum number of results to parse. Controlled by `SEARCH_RESULTS_PER_PAGE` from `config.py` |
| `date_from` | `str \| None` | `None` | ISO date string for recency filter, e.g. `"2026-03-19"`. Appended as `date:>YYYYMMDD` to query text AND sets `period: "PERIOD_2_WEEKS"` in request body. |

**Query construction example (Batch A with `site_filter` and `date_from`):**
```
("DDVB") (site:sostav.ru | site:retail.ru | site:unipack.ru | site:new-retail.ru) date:>20260319
```

**Request body (current, reflecting actual code):**
```python
{
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
        "groupsOnPage": SEARCH_RESULTS_PER_PAGE,  # 50
        "docsInGroup": 1,
    },
    "maxPassages": 2,
    "region": "225",      # Russia
    "folderId": YC_FOLDER_ID,
    "period": "PERIOD_2_WEEKS",   # only when date_from is set
}
```

Three simultaneous recency controls are applied when `date_from` is set: `date:>YYYYMMDD` in query text, `SORT_MODE_BY_TIME` sort, and `PERIOD_2_WEEKS` native date filter. The native filter is more reliable than the query operator alone according to Yandex Search API documentation.

**Returns:** `list[dict]` — each dict has:

| Key | Type | Description |
|---|---|---|
| `url` | `str` | Full canonical URL |
| `title` | `str` | Page title with HTML tags stripped |
| `snippet` | `str` | Up to 2 passages joined with a space, HTML tags stripped |
| `modtime` | `str` | Document modification date as `YYYYMMDD` string (from `modtime` attribute in XML) |
| `domain` | `str` | Domain with `www.` prefix removed |

Returns an empty list on timeout, HTTP error after retries, or any exception.

**Retry behavior:** Up to 3 attempts with exponential backoff (2s, 4s, 8s) on status codes in `_RETRY_STATUSES`. If the initial response status is in `_RETRY_STATUSES`, the retry loop catches it before `raise_for_status()`.

**Polling behavior:** Polls `GET /operations/{id}` every 2 seconds, up to 15 attempts (30-second maximum). Returns empty list if `done` is never `True` within the window.

---

### `_parse_search_xml(operation_result: dict, max_results: int) -> list[dict]`

**Visibility:** Module-private (called from `search_web`; directly imported in `tests/test_parsing.py`)

Parses the Yandex Search API operation result. The result contains base64-encoded XML in `operation_result["response"]["rawData"]`.

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `operation_result` | `dict` | Completed operation dict from the Yandex Operations API with `done=True` |
| `max_results` | `int` | Maximum number of `<group>` blocks to parse |

**Returns:** `list[dict]` with `url`, `title`, `snippet`, `modtime`, `domain` keys. Returns empty list if `rawData` is absent or empty.

**Parsing strategy:** Uses `re.findall(r"<group>(.*?)</group>", xml, re.DOTALL)` to extract result groups. Within each group, separate regex searches extract individual fields. The `modtime` attribute (`modtime="20260320T..."`) provides publication date in `YYYYMMDD` format — the date part before `T` is extracted.

**Snippet construction:** `" ".join(_clean_html(p) for p in passage_matches[:2])` — up to 2 passages joined.

**Domain normalization:** `domain_match.group(1).replace("www.", "")` — falls back to `urlparse(url).netloc` if no `<domain>` element found.

---

### `_clean_html(text: str) -> str`

**Visibility:** Module-private (directly imported in `tests/test_parsing.py`)

Removes all HTML/XML tags from a string using a single regex substitution.

**Implementation:** `re.sub(r"<[^>]+>", "", text).strip()`

The primary use case is removing `<hlword>` highlight tags that Yandex wraps around matched keywords in titles and passages.

---

### `get_llm_client() -> OpenAI`

**Visibility:** Public (called within `classify_relevance` and `summarize_mention`)

Creates and returns a configured `openai.OpenAI` client pointed at the Yandex Cloud LLM endpoint.

**Returns:** `openai.OpenAI` instance configured with:
- `base_url="https://llm.api.cloud.yandex.net/v1"`
- `api_key="unused"` (real auth is in `default_headers`)
- `default_headers={"Authorization": f"Api-Key {YC_API_KEY}"}`

A new client instance is created on every call.

---

### `classify_relevance(title: str, snippet: str) -> str`

**Visibility:** Public (called from `main.py` at Layer 6)

Calls YandexGPT Lite to determine whether a search result is a genuine editorial brand mention. This function is called **only for Search API results** — generative search results bypass this layer.

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `title` | `str` | Article title from the search result |
| `snippet` | `str` | Passage text from the search result |

Combined text passed to the model: `f"Заголовок: {title}\nОписание: {snippet}"`, truncated to 1000 characters.

**Returns:** `str` — either `"relevant"` or `"irrelevant"`

**Model call parameters:**
- Model: `gpt://{YC_FOLDER_ID}/yandexgpt-lite/latest`
- Temperature: `0.0` (deterministic)
- Max tokens: `10`

**Classification criteria encoded in the system prompt (2026 reference year):**
- **Relevant:** news, article, case study, rating, or analytics piece explicitly mentioning DDVB as a branding agency, by employees (Maria Arkhangelskaya, Leonid Feigin), or by clients. Must be current year (2026).
- **Irrelevant:** DDVB not explicitly in text; automotive engine code match; catalog/aggregator/link page without editorial content; DDVB mentioned only in sidebar/footer/"similar" list; old publication (2024 and earlier).

**Result extraction:** `"relevant" if "relevant" in answer else "irrelevant"`. At `temperature=0.0` the model consistently returns a single word.

**Failure behavior:** Returns `"relevant"` on any exception (fail-open). Logs the error at ERROR level.

---

### `summarize_mention(title: str, snippet: str) -> str`

**Visibility:** Public (available but not called automatically in the current pipeline)

Calls YandexGPT Lite to generate a 2–3 sentence Russian summary describing the context and key information of a brand mention.

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `title` | `str` | Article title |
| `snippet` | `str` | Passage text (full length, not pre-truncated) |

**Returns:** `str` — Russian-language summary text, or the first 200 characters of `snippet` on failure

**Model call parameters:**
- Model: `gpt://{YC_FOLDER_ID}/yandexgpt-lite/latest`
- Temperature: `0.3`
- Max tokens: `200`

**Note:** This function is not called in the production `run_pipeline()`. The `summary` field for Search API results is populated from `snippet[:200]` directly in `run_pipeline()`. For generative search results, `summary` is populated from `result.text[:200]` in `search_and_classify()`. `summarize_mention()` is available for manual invocation or future pipeline use.

---

## config.py

Loads all configuration from environment variables at module import time using `python-dotenv`. Because Python caches module imports, this module is loaded exactly once per process.

There are no functions in `config.py` — only module-level constants.

**Exported names used by other modules:**

| Name | Type | Source | Required |
|---|---|---|---|
| `YC_API_KEY` | `str` | `os.environ["YC_API_KEY"]` | Yes — raises `KeyError` on import if missing |
| `YC_FOLDER_ID` | `str` | `os.environ["YC_FOLDER_ID"]` | Yes — raises `KeyError` on import if missing |
| `YC_BASE_URL` | `str` | Hardcoded: `"https://llm.api.cloud.yandex.net/v1"` | — |
| `AI_STUDIO_API_KEY` | `str` | `os.getenv("AI_STUDIO_API_KEY", "")` | No — reserved, currently unused (SDK uses `YC_API_KEY`) |
| `YANDEX_GPT_LITE` | `str` | `f"gpt://{YC_FOLDER_ID}/yandexgpt-lite/latest"` | — |
| `YANDEX_GPT_PRO` | `str` | `f"gpt://{YC_FOLDER_ID}/yandexgpt/latest"` | — |
| `SUPABASE_URL` | `str` | `os.getenv("SUPABASE_URL", "")` | No — empty disables Supabase |
| `SUPABASE_SERVICE_ROLE_KEY` | `str` | `os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")` | No — empty disables Supabase |
| `DEFAULT_TARGET_DOMAINS` | `list[str]` | Hardcoded | — |
| `DEFAULT_SEARCH_QUERIES` | `list[str]` | Hardcoded | — |
| `DEFAULT_EXCLUDE_DOMAINS` | `list[str]` | Hardcoded | — |
| `SEARCH_RESULTS_PER_PAGE` | `int` | Hardcoded: `50` | — |
| `SEARCH_DATE_RESTRICT_DAYS` | `int` | Hardcoded: `7` | — |
| `YANDEX_RATE_LIMIT_SECONDS` | `float` | Hardcoded: `1.0` | — |
| `MAX_DOMAINS_PER_BATCH` | `int` | Hardcoded: `5` | — |
| `BLOCKED_DOMAINS` | `set[str]` | Hardcoded (~50 entries) | — |
| `SOCIAL_MEDIA_DOMAINS` | `set[str]` | Hardcoded (documentation constant, not added to blocklist) | — |
| `SMTP_HOST` | `str` | `os.getenv("SMTP_HOST", "")` | No — empty disables email |
| `SMTP_PORT` | `int` | `int(os.getenv("SMTP_PORT", "587"))` | No |
| `SMTP_USER` | `str` | `os.getenv("SMTP_USER", "")` | No |
| `SMTP_PASSWORD` | `str` | `os.getenv("SMTP_PASSWORD", "")` | No |
| `SMTP_FROM` | `str` | `os.getenv("SMTP_FROM", "agent@ddvb.tech")` | No |
| `DEFAULT_RECIPIENTS` | `list[str]` | Hardcoded: `["ilya@ddvb.tech", "maria@ddvb.tech"]` | — |

**Failure mode on import:** If `YC_API_KEY` or `YC_FOLDER_ID` are absent from the environment, importing `config.py` raises a `KeyError`. Because all modules import from `config.py` at the top of their file, the entire service fails at startup if either required variable is missing.

**Note on `AI_STUDIO_API_KEY`:** This variable is defined in `config.py` and loaded from the environment, but it is not read by `yandex_agent.py`. The generative search SDK (`AIStudio`) is initialized with `auth=YC_API_KEY` directly. `AI_STUDIO_API_KEY` is reserved for a potential future configuration where a separate AI Studio key is required.

---

## supabase_client.py

All Supabase interactions. Every function handles its own connection failures gracefully, returning empty or default values instead of raising exceptions.

---

### `get_client() -> supabase.Client | None`

**Visibility:** Module-private (called by all other functions in this module)

Creates and returns a Supabase client, or `None` if `SUPABASE_SERVICE_ROLE_KEY` is empty.

**Returns:** `supabase.Client` if `SUPABASE_SERVICE_ROLE_KEY` is non-empty; `None` otherwise.

When `None` is returned, calling functions skip their operation and return a safe default (empty set, empty list, or 0).

**Note:** Creates a new client on every call rather than caching at module level. Safe for the Cloud Functions runtime where each invocation is a fresh process.

---

### `load_settings() -> dict`

**Visibility:** Public (called from `main.py`)

Reads the `mention_settings` Supabase table and returns a configuration dict. Falls back to `config.py` defaults if Supabase is unavailable or the table is empty.

**Returns:** `dict` with keys:

| Key | Type | Default value |
|---|---|---|
| `"target_domains"` | `list[str]` | `DEFAULT_TARGET_DOMAINS` from `config.py` |
| `"search_queries"` | `list[str]` | `DEFAULT_SEARCH_QUERIES` from `config.py` |
| `"exclude_domains"` | `list[str]` | `DEFAULT_EXCLUDE_DOMAINS` from `config.py` |

**Row format handling:** Accepts both `key`/`value` and `setting_key`/`setting_value` column naming:
```python
key = row.get("key") or row.get("setting_key")
value = row.get("value") or row.get("setting_value")
```

**Failure modes:** Returns full defaults dict on: `None` client, empty table, or any exception.

---

### `get_existing_urls() -> set[str]`

**Visibility:** Public (called from `main.py`)

Returns the set of all URLs already stored in `brand_mentions`. Used by `deduplicate()` at Layer 1 to prevent re-processing of previously seen mentions.

**Returns:** `set[str]` of URL strings. Returns empty set if Supabase is unavailable or query fails.

**Query:** `SELECT url FROM brand_mentions` — fetches only the `url` column.

**Note:** In dry-run mode, `run_pipeline()` skips this call entirely (`existing_urls = set()`).

---

### `save_mentions(mentions: list[dict]) -> int`

**Visibility:** Public (called from `main.py`)

Batch-upserts relevant mention dicts into the `brand_mentions` table. Returns count of rows affected.

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `mentions` | `list[dict]` | All classified-relevant dicts from `run_pipeline()` |

**Field mapping from mention dict to database row:**

| DB column | Dict key | Notes |
|---|---|---|
| `url` | `"url"` | Upsert conflict key — must have unique constraint in Supabase |
| `title` | `"title"` | Defaults to `""` |
| `snippet` | `"snippet"` | Defaults to `""` |
| `source_domain` | `"domain"` | Defaults to `""` |
| `discovery_query` | `"discovery_query"` | Defaults to `""` |
| `relevance_label` | `"relevance"` | Defaults to `"relevant"` |
| `discovery_source` | `"discovery_source"` | `"ai_studio_generative"` or `"yandex_search_api"` |
| `summary` | `"summary"` | AI answer text (generative) or `snippet[:200]` (Search API) |

The `discovery_source` and `summary` fields are now saved to the database. Previously `discovery_source` was hardcoded to `"yandex_search_api"` and `summary` was not persisted.

**Upsert behavior:** `on_conflict="url"` — if a URL already exists, the existing row is updated.

**Returns:** `int` — count of rows in response data. Returns 0 on `None` client, empty input, or exception.

---

### `get_originating_publications() -> list[dict]`

**Visibility:** Public (not called from the current pipeline)

Returns all rows from the `originating_publications` table. Implemented but unused — reserved for a future feature tracking monitored publications separately from discovered mentions.

**Returns:** `list[dict]` of all rows, or empty list on failure.

---

## email_digest.py

HTML email construction and SMTP delivery using Python's standard library (`smtplib`, `email.mime`). STARTTLS is the only supported transport — the SSL branch from earlier versions has been removed.

---

### `_build_mention_html(mentions: list[dict]) -> str`

**Visibility:** Module-private (called from `send_digest`)

Renders the HTML email body for a digest with one or more mentions.

**Template fields used from each mention dict:**

| Template variable | Dict key | Fallback |
|---|---|---|
| Article title (link text) | `"title"` | `"Без заголовка"` |
| Hyperlink href | `"url"` | `"#"` |
| Domain pill badge | `"domain"` or `"source_domain"` | `""` |
| Snippet text | `"snippet"` | `""` (truncated at 300 chars in template) |

**Returns:** `str` — UTF-8 HTML document with inline styles, single-column layout with header, date line, mention table, and footer.

---

### `_build_empty_html() -> str`

**Visibility:** Module-private (called from `send_empty_notification`)

Renders the HTML body for the "no new mentions" notification. Sends a brief paragraph — this is important so recipients can distinguish "no mentions found" from "the system failed to run."

**Returns:** `str` — HTML document with same header/footer as digest but with a single paragraph.

---

### `_send_email(subject: str, html_body: str, recipients: list[str]) -> bool`

**Visibility:** Module-private (called from `send_digest` and `send_empty_notification`)

Sends an HTML email via SMTP using STARTTLS exclusively.

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `subject` | `str` | Email subject line |
| `html_body` | `str` | Complete HTML document string |
| `recipients` | `list[str]` | List of recipient email addresses |

**Transport:** Always uses `smtplib.SMTP` + `STARTTLS` (the port-based SSL branch has been removed from this version). Port 587 is the recommended and default port.

**Authentication:** `server.login()` is called only when both `SMTP_USER` and `SMTP_PASSWORD` are non-empty.

**Returns:** `bool` — `True` on successful `sendmail()`; `False` if `SMTP_HOST` is empty (warning logged), or if any exception occurs.

**Timeout:** 30 seconds on SMTP connection.

---

### `send_digest(mentions: list[dict], recipients: list[str]) -> bool`

**Visibility:** Public (called from `main.py`)

Sends the HTML digest email. Subject line format: `"DDVB Media Mentions — {dd.mm.yyyy}"`.

**Returns:** `bool` — result from `_send_email()`

---

### `send_empty_notification(recipients: list[str]) -> bool`

**Visibility:** Public (called from `main.py`)

Sends the "no new mentions" notification. Uses the same subject line format as `send_digest`.

**Returns:** `bool` — result from `_send_email()`

---

## poc_search.py

A standalone proof-of-concept script for interactive validation of the search and classification pipeline. Not part of the production pipeline — intended for manual developer use only.

---

### `main() -> None`

Runs the PoC interactively: searches both batches using `search_web()` from `yandex_ai.py`, deduplicates with simplified logic (no `www.` stripping, no Supabase cross-check), filters with a module-local `OWN_DOMAINS` constant (not the full `BLOCKED_DOMAINS` set), classifies all results with `classify_relevance()`, and prints a summary to stdout with domain breakdown of relevant mentions.

No database writes, no email sending. Useful for verifying that the Yandex Search API key works and that search queries return reasonable results.
