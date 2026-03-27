# brand-mention-monitor — CLAUDE.md

Technical reference for Claude Code and developers working on the Brand Mention Monitor agent.

---

## 1. Project Overview

**Brand Mention Monitor** is a Python service that automatically discovers and tracks editorial mentions of the DDVB brand in Russian-language media. It runs as a scheduled Yandex Cloud Function and delivers results by email.

The service answers one business question: "Where has DDVB been mentioned in Russian trade press and business media this week?" It targets publications like sostav.ru, retail.ru, and new-retail.ru — industry sources that cover branding and marketing clients.

The pipeline is fully autonomous: it searches via two complementary methods (AI Studio generative search and Yandex Search API v2), runs results through a 6-layer filter pipeline, classifies with AI, deduplicates against historical records, stores results in Supabase, and emails a formatted HTML digest to the team. A `--dry-run` flag allows safe inspection without side effects.

---

## 2. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                  Yandex Cloud Functions (scheduled)                     │
│                                                                         │
│   handler(event, context)  ←── cron trigger                            │
│          │                                                              │
│          ▼                                                              │
│   run_pipeline()  [main.py]                                             │
│          │                                                              │
│   ┌──────┴─────────────────────────────────────────────────────────┐   │
│   │                      PIPELINE STAGES                            │   │
│   │                                                                 │   │
│   │  1. load_settings()       ←── mention_settings table           │   │
│   │         │                     (Supabase)                        │   │
│   │  2. get_existing_urls()   ←── brand_mentions table             │   │
│   │         │                     (Supabase)                        │   │
│   │                                                                 │   │
│   │  3. agent_search()        ──► Yandex AI Studio SDK             │   │
│   │      [yandex_agent.py]        sdk.search_api.generative()      │   │
│   │      (PRIMARY — all queries   Returns: used sources only        │   │
│   │       including "ДДВБ")       discovery_source=ai_studio_       │   │
│   │         │                     generative                        │   │
│   │  4. search_web()  ×N      ──► Yandex Search API v2             │   │
│   │      [yandex_ai.py]           searchAsync (async + poll)        │   │
│   │      (FALLBACK — Latin        date filter + sort by time        │   │
│   │       "DDVB" only,            + period=PERIOD_2_WEEKS           │   │
│   │       Cyrillic skipped)       discovery_source=yandex_search_   │   │
│   │         │                     api                               │   │
│   │  5. merge + deduplicate() — agent results first (priority)     │   │
│   │         │                                                        │   │
│   │  ═══════ 6-LAYER FILTER PIPELINE ══════════════════════════    │   │
│   │                                                                 │   │
│   │  [L1] deduplicate()       URL normalization + DB cross-check   │   │
│   │  [L2] filter_blocked()    BLOCKED_DOMAINS set + TLD allowlist  │   │
│   │  [L3] brand gate          "DDVB"/"ДДВБ" must be in title/snip  │   │
│   │  [L4] year filter         rejects pre-2026 from URL / text     │   │
│   │  [L5] page verification   fetches URL, checks DDVB in HTML     │   │
│   │         │                 (skipped for ai_studio_generative)    │   │
│   │  [L6] classify_relevance  YandexGPT Lite binary classifier     │   │
│   │         │                 (skipped for ai_studio_generative)    │   │
│   │  ══════════════════════════════════════════════════════════    │   │
│   │         │                                                        │   │
│   │  save_mentions()          ──► brand_mentions table             │   │
│   │         │                     (Supabase, upsert on url)         │   │
│   │  send_digest()            ──► SMTP → email recipients          │   │
│   └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘

External APIs:
  Yandex AI Studio SDK     sdk.search_api.generative()           (primary)
  Yandex Search API v2     searchapi.api.cloud.yandex.net/v2/web/searchAsync  (fallback)
  Yandex Operations API    operation.api.cloud.yandex.net/operations/{id}
  YandexGPT (OpenAI SDK)   llm.api.cloud.yandex.net/v1          (L6 classifier)
  Supabase REST            SUPABASE_URL (project-specific)
  SMTP                     Configurable host/port (STARTTLS only — see email_digest.py)
```

### Module responsibilities

| File | Responsibility |
|---|---|
| `main.py` | Pipeline orchestration, 6-layer filter implementation, CLI entry point, Cloud Function handler |
| `config.py` | Environment variable loading, constants, BLOCKED_DOMAINS and SOCIAL_MEDIA_DOMAINS sets |
| `yandex_agent.py` | Yandex AI Studio SDK client — generative search (primary: search + contextual AI analysis in one call) |
| `yandex_ai.py` | Yandex Search API v2 (fallback: async search + XML parsing) and YandexGPT classifier/summarizer |
| `supabase_client.py` | Supabase reads (settings, existing URLs) and writes (upsert mentions with summary column) |
| `email_digest.py` | HTML email construction and SMTP delivery (STARTTLS only) |
| `poc_search.py` | Interactive PoC script for manual validation (not used in production) |

---

## 3. Primary Search: Yandex AI Studio SDK Generative Search

The primary search method uses the `yandex-ai-studio-sdk` package. Unlike the Search API v2, generative search combines web retrieval and AI analysis in a single call — the model searches Yandex, reads full page content, and returns a synthesized answer with source URLs.

### SDK call pattern

```python
from yandex_ai_studio_sdk import AIStudio

sdk = AIStudio(folder_id=YC_FOLDER_ID, auth=YC_API_KEY)
search = sdk.search_api.generative(
    search_filters=[{"date": f">{date_from.replace('-', '')}"}]
)
result = search.run(prompt, timeout=120)
# result.text    — AI-synthesized answer text
# result.sources — list of source objects with .url, .title, .used attributes
```

Only sources where `source.used == True` are included in output. Unused sources are tangential context the AI consulted but did not cite — they are skipped with a `DEBUG` log.

### Prompt construction

The prompt (`yandex_agent.py:69-77`) explicitly names DDVB as a branding agency and instructs the model to ignore automotive engine code matches ("DDVB" is also a VAG/Audi engine code). This context disambiguation is only practical with generative search — the Search API v2 cannot reason about query intent.

### Why "ДДВБ" runs in generative search only

Both `"DDVB"` and `"ДДВБ"` are passed to generative search. The Cyrillic query is intentionally excluded from Search API v2 (`main.py:176`): it matches VAG engine codes and random Cyrillic character sequences, producing massive noise that a keyword-based search cannot filter. The generative AI model handles "ДДВБ" contextually and suppresses irrelevant matches.

### Result shape

Each generative search result has `discovery_source = "ai_studio_generative"` and `snippet = ""` (the AI does not extract page passages — the summary field contains the AI-synthesized answer text up to 200 characters). These results skip both page verification (layer 5) and AI classification (layer 6) in the filter pipeline because the generative model already performed equivalent analysis.

### Retry logic

`yandex_agent.py` retries up to 2 times with 3-second backoff per query on any exception. A 1-second rate limit sleep is applied between queries.

---

## 4. Fallback Search: Yandex Search API v2

The Search API v2 provides breadth coverage — it finds mentions the generative model may not surface, particularly on less prominent domains.

### Async search pattern

Every search goes through two HTTP calls:

1. **Submit** — POST to `searchAsync` with the query body; receive an operation ID immediately.
2. **Poll** — GET `operations/{id}` every 2 seconds until `done == true` (max 30 seconds / 15 attempts).

### Query construction with date and sort filters

`search_web()` in `yandex_ai.py` assembles a query body with three recency controls applied simultaneously:

- `date:>YYYYMMDD` appended to the query text
- `"sortMode": "SORT_MODE_BY_TIME"` with `"sortOrder": "SORT_ORDER_DESC"` — results sorted newest-first
- `"period": "PERIOD_2_WEEKS"` in the request body — Yandex's native date window filter (more reliable than the query operator alone)

The number of results per page is controlled by `SEARCH_RESULTS_PER_PAGE = 50` (in `config.py`), passed as `groupsOnPage` in the `groupSpec`.

### Two-batch strategy (Latin "DDVB" only)

**Batch A — domain-restricted:** Constructs a compound Yandex query using the `site:` operator. For target domains `["sostav.ru", "retail.ru", "unipack.ru", "new-retail.ru"]`, the submitted query becomes:

```
("DDVB") (site:sostav.ru | site:retail.ru | site:unipack.ru | site:new-retail.ru) date:>20260319
```

**Batch B — broad web:** Submits the raw query without any `site:` filter, catching mentions anywhere on the Russian web.

Each result is tagged with `discovery_query` (e.g., `'"DDVB" (domain-restricted)'` or `'"DDVB" (broad)'`) and `discovery_source = "yandex_search_api"`.

### XML response parsing

The Yandex Search API returns results as base64-encoded XML inside a JSON envelope:

```
operation_result["response"]["rawData"]  →  base64 decode  →  XML string
```

The XML is parsed with regex (not an XML library) extracting `<group>` blocks. From each group, the parser extracts `<url>`, `<title>`, `<domain>`, `<passage>`, and the `modtime` attribute (format `YYYYMMDDTHHMMSS`). `<hlword>` highlight tags within titles and passages are stripped by `_clean_html()`. Up to two passages are joined with a space to form the snippet.

### Retry logic

The HTTP client retries on status codes `{403, 429, 500, 502, 503, 504}` with exponential backoff of 2, 4, 8 seconds across a maximum of 3 attempts.

---

## 5. The 6-Layer Filter Pipeline

After merging agent results (first) and Search API results, the pipeline applies six sequential filters. Agent-sourced results are pre-classified and skip layers 5 and 6.

### Layer 1 — Deduplication (`deduplicate()`, `main.py`)

URL normalization before comparison: trailing slash stripped, query parameters (`?...`) and hash fragments (`#...`) stripped, `www.` prefix stripped. Checks against both the in-session `seen` set and the full set of existing URLs from Supabase.

### Layer 2 — Blocklist + TLD filter (`filter_blocked()`, `main.py`)

Combines `BLOCKED_DOMAINS` (hardcoded in `config.py`) with the configurable `exclude_domains` list from `mention_settings`. Additionally enforces a TLD allowlist (`ALLOWED_TLDS` in `main.py`) — domains with foreign TLDs (`.de`, `.uk`, `.shop`, etc.) are rejected because DDVB editorial mentions only appear on Russian-zone sites.

**Social media is NOT blanket-blocked.** `t.me`, `vk.com`, `ok.ru`, and similar platforms are listed in `SOCIAL_MEDIA_DOMAINS` (a documentation constant in `config.py`) but are not added to `BLOCKED_DOMAINS`. Third-party editorial mentions published on these platforms (e.g., a Telegram channel article) are legitimate and pass through layer 2 to be assessed by the generative AI or the YandexGPT classifier.

### Layer 3 — Brand gate (`main.py:237-249`)

Hard check: the lowercase string `"ddvb"` or `"ддвб"` must appear literally in `title + snippet`. This is the most reliable single-pass filter — if the brand name is absent from the text the search engine returned, it cannot be a brand mention regardless of what the AI classifier might decide. Results rejected here are logged at DEBUG level.

### Layer 4 — Year filter (`_extract_publication_year()`, `main.py:47-65`)

Extracts the publication year from:
1. URL path patterns: `/2024/08/`, `/2024-03-26`, `-2024-` (most reliable — news sites embed dates in paths)
2. Four-digit year mentions in title and snippet text (takes the most recent year found)

If an extractable year is found and it is less than the current calendar year, the result is rejected. If no year can be extracted, the result passes through (fail-open).

### Layer 5 — Page verification (`_verify_page_mentions_brand()`, `main.py:68-88`)

Fetches each candidate URL with `httpx` (10-second timeout, follows redirects) and checks that `"ddvb"` or `"ддвб"` appears anywhere in the lowercased page HTML. This catches Yandex's context highlighting — the search API can include "DDVB" in a snippet through keyword injection even when the actual page does not mention the brand.

**Skipped for generative search results** — `ai_studio_generative` sources already had full page content read by the AI. On fetch failure (non-200, timeout, network error), the result passes through fail-open.

### Layer 6 — AI classifier (`classify_relevance()`, `yandex_ai.py`)

**Skipped for generative search results.** For Search API results, calls YandexGPT Lite at temperature 0.0 with `max_tokens=10`. The system prompt (updated to reference 2026) instructs the model to respond with exactly one word: `relevant` or `irrelevant`.

Classification criteria encoded in the system prompt:
- **Relevant:** news, article, case study, rating, or analytics piece mentioning DDVB as a branding agency, including by employees (Maria Arkhangelskaya, Leonid Feigin) or clients
- **Irrelevant:** DDVB not explicitly present; automotive engine code match; directory/aggregator/link page without editorial content; DDVB mentioned only in sidebar, footer, or "similar" list; clearly old publication (2024 and earlier)

Fails open — returns `"relevant"` on API error.

---

## 6. AI Summarization

`summarize_mention()` in `yandex_ai.py` is available but not called automatically in the current pipeline. The `summary` field is populated from the snippet (first 200 characters) for Search API results, and from the generative search answer text for `ai_studio_generative` results. The summary is saved to Supabase (the `brand_mentions` table now includes a `summary` column — `save_mentions()` includes it in the upsert row).

When called manually, `summarize_mention()` uses temperature 0.3, `max_tokens=200`, and requests a 2–3 sentence Russian-language summary. On failure, falls back to the first 200 characters of the snippet.

---

## 7. Database Schema (Supabase)

The service interacts with two tables. Schema is not managed by this service — tables must be created manually in the Supabase dashboard before first run.

### `brand_mentions`

Primary storage for discovered mentions. Upserted on `url` (the conflict key, which must have a unique constraint).

| Column | Type | Notes |
|---|---|---|
| `url` | text (unique) | Canonical URL, used as upsert conflict key |
| `title` | text | Page title with HTML tags stripped |
| `snippet` | text | Up to 2 passages joined, HTML tags stripped |
| `source_domain` | text | Domain without `www.` prefix |
| `discovery_query` | text | Query string and batch type that found this URL |
| `relevance_label` | text | Always `"relevant"` for saved rows |
| `discovery_source` | text | `"ai_studio_generative"` or `"yandex_search_api"` |
| `summary` | text | AI summary (generative answer or snippet fallback) |

Supabase auto-manages `id`, `created_at`, and `updated_at` columns if configured in the table definition.

```sql
CREATE TABLE brand_mentions (
    id               bigserial PRIMARY KEY,
    url              text UNIQUE NOT NULL,
    title            text,
    snippet          text,
    source_domain    text,
    discovery_query  text,
    relevance_label  text,
    discovery_source text,
    summary          text,
    created_at       timestamptz DEFAULT now(),
    updated_at       timestamptz DEFAULT now()
);
CREATE UNIQUE INDEX brand_mentions_url_idx ON brand_mentions (url);
```

### `mention_settings`

Key-value configuration table. The service reads at startup and falls back to `config.py` defaults if empty or unreachable.

Expected row structure (the service handles both `key`/`value` and `setting_key`/`setting_value` column naming):

| `key` | Expected value type | Default |
|---|---|---|
| `target_domains` | list of strings | `["sostav.ru", "retail.ru", "unipack.ru", "new-retail.ru"]` |
| `search_queries` | list of strings | `['"DDVB"', '"ДДВБ"']` |
| `exclude_domains` | list of strings | `["sostav.ru"]` |

### `originating_publications`

A third table is referenced by `get_originating_publications()` in `supabase_client.py` but is not used in the current pipeline. It is likely intended for a future feature that tracks which publications are actively monitored.

---

## 8. Email Digest Flow

The email module (`email_digest.py`) builds and sends an HTML email using Python's standard library `smtplib` and `email.mime`.

### Two message types

**Digest with mentions** (`send_digest`): Called when at least one relevant mention was found. Renders an HTML table where each row shows the article title as a hyperlink, the source domain as a pill badge, and the snippet truncated to 300 characters.

**Empty notification** (`send_empty_notification`): Called when the pipeline finds no new relevant mentions after all filtering. Sends a brief notification — this is important so recipients can distinguish "no mentions found" from "the system failed to run."

### SMTP transport

The current `_send_email` implementation uses `smtplib.SMTP` + `STARTTLS` exclusively (the port-based SSL branch from earlier versions has been removed). Authentication with `SMTP_USER` and `SMTP_PASSWORD` is applied when both are non-empty. The sender address defaults to `agent@ddvb.tech`.

Recipients are hardcoded in `config.py` as `DEFAULT_RECIPIENTS = ["ilya@ddvb.tech", "maria@ddvb.tech"]`. There is no mechanism to override recipients at runtime without changing the source or environment.

---

## 9. Configuration (Environment Variables)

All configuration is loaded by `config.py` from environment variables via `python-dotenv`. For local development, create a `.env` file at the project root (not committed to git).

### Required

| Variable | Description |
|---|---|
| `YC_API_KEY` | Yandex Cloud IAM API key. Used for Search API v2, YandexGPT, and AI Studio SDK (same key). Must have permissions for `search.yandex.net`, `llm.yandex.net`, and AI Studio generative search. |
| `YC_FOLDER_ID` | Yandex Cloud folder (project) ID. Used in YandexGPT model URIs (`gpt://{YC_FOLDER_ID}/yandexgpt-lite/latest`) and AI Studio SDK initialization. |

### Optional (with defaults)

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_URL` | `""` | Supabase project URL. If empty, all Supabase operations are skipped gracefully. |
| `SUPABASE_SERVICE_ROLE_KEY` | `""` | Supabase service role key (bypasses Row Level Security). Use the service role key, not the anon key — the anon key will not have write permissions by default. |
| `SMTP_HOST` | `""` | SMTP server hostname. If empty, email sending is skipped with a warning. |
| `SMTP_PORT` | `587` | SMTP port. Must support STARTTLS. |
| `SMTP_USER` | `""` | SMTP authentication username. |
| `SMTP_PASSWORD` | `""` | SMTP authentication password. |
| `SMTP_FROM` | `"agent@ddvb.tech"` | Sender address in outgoing emails. |
| `AI_STUDIO_API_KEY` | `""` | Reserved for a separate AI Studio key. Currently unused — the generative search SDK uses `YC_API_KEY` directly. |

### Hardcoded constants (change in `config.py`)

| Constant | Value | Description |
|---|---|---|
| `DEFAULT_TARGET_DOMAINS` | `["sostav.ru", "retail.ru", "unipack.ru", "new-retail.ru"]` | Trade publications for domain-restricted Batch A search. Override via `mention_settings` table. |
| `DEFAULT_SEARCH_QUERIES` | `['"DDVB"', '"ДДВБ"']` | Both Latin and Cyrillic brand name queries. Both run in generative search; only Latin runs in Search API v2. Override via `mention_settings` table. |
| `DEFAULT_EXCLUDE_DOMAINS` | `["sostav.ru"]` | Domains excluded after search (separate from the blocked list). Override via `mention_settings` table. |
| `SEARCH_RESULTS_PER_PAGE` | `50` | `groupsOnPage` value for Search API v2 requests. |
| `SEARCH_DATE_RESTRICT_DAYS` | `7` | Look-back window in days for both generative and API search date filters. |
| `YANDEX_RATE_LIMIT_SECONDS` | `1.0` | Sleep between all Yandex API calls. The generative API enforces 1 req/sec. |
| `MAX_DOMAINS_PER_BATCH` | `5` | Reference constant for `site:` filter limit. Not enforced in current code. |
| `BLOCKED_DOMAINS` | (set of ~50 domains) | Permanent blocklist: DDVB's own domains, search engines, WHOIS/SEO tools, car parts sites, spam/unrelated domains, and select visual platforms. Social media is NOT in this set. |
| `SOCIAL_MEDIA_DOMAINS` | `{t.me, vk.com, ok.ru, ...}` | Documentation constant only — not added to `BLOCKED_DOMAINS`. Social media passes through to AI classification. |

---

## 10. Deployment (Yandex Cloud Functions)

### How deployment works

The `deploy/` directory contains a self-contained copy of all production source modules alongside a `deps/` subdirectory with all Python dependencies pre-installed as importable packages. This flat directory tree is zipped into `function.zip` and uploaded to Yandex Cloud Functions.

**Size constraint:** The Yandex Cloud Functions console upload limit is 3.5 MB for direct zip upload. Because `deploy/deps/` contains compiled binary extensions (cryptography, cffi), the zip typically exceeds this limit. The deployment workflow uses **Yandex Object Storage (S3)** as an intermediary:

```bash
# Upload the zip to an Object Storage bucket
aws s3 cp function.zip s3://your-bucket-name/function.zip \
    --endpoint-url https://storage.yandexcloud.net

# Deploy from Object Storage URL (no size limit via this path)
yc serverless function version create \
    --function-name brand-mention-monitor \
    --runtime python312 \
    --entrypoint main.handler \
    --memory 256m \
    --execution-timeout 300s \
    --package-bucket-name your-bucket-name \
    --package-object-name function.zip \
    --environment YC_API_KEY=... \
    --environment YC_FOLDER_ID=... \
    --environment SUPABASE_URL=... \
    --environment SUPABASE_SERVICE_ROLE_KEY=... \
    --environment SMTP_HOST=... \
    --environment SMTP_PORT=587 \
    --environment SMTP_USER=... \
    --environment SMTP_PASSWORD=...
```

Alternatively, use the Yandex Cloud console: upload the zip to an Object Storage bucket via the console UI, then select "Object Storage" as the source when creating a function version.

### Cloud Function entry point

The Yandex Cloud Functions runtime calls:

```python
handler(event, context)  # in main.py
```

This function configures logging and delegates to `run_pipeline(dry_run=False)`. It returns `{"statusCode": 200, "body": summary_dict}`.

Environment variables are injected through the Yandex Cloud Functions configuration UI or CLI — not through a `.env` file (which is only used for local development).

### Keeping `deploy/` in sync

`deploy/main.py` is a copy of root `main.py`. When modifying the production pipeline, update both files. Similarly for `deploy/email_digest.py`, `deploy/supabase_client.py`, `deploy/yandex_ai.py`, `deploy/yandex_agent.py`, and `deploy/config.py`.

There is no automated sync mechanism. The recommended workflow:

```bash
# 1. Edit source files at project root
# 2. Copy updated files to deploy/
cp main.py deploy/main.py
cp yandex_agent.py deploy/yandex_agent.py
cp yandex_ai.py deploy/yandex_ai.py
cp supabase_client.py deploy/supabase_client.py
cp email_digest.py deploy/email_digest.py
cp config.py deploy/config.py

# 3. Rebuild the zip (exclude pyc and pycache)
cd deploy && zip -r ../function.zip . -x "__pycache__/*" -x "*.pyc" -x ".DS_Store" && cd ..

# 4. Upload to Object Storage
aws s3 cp function.zip s3://your-bucket-name/function.zip \
    --endpoint-url https://storage.yandexcloud.net

# 5. Deploy from Object Storage
yc serverless function version create \
    --function-name brand-mention-monitor \
    --runtime python312 \
    --entrypoint main.handler \
    --memory 256m \
    --execution-timeout 300s \
    --package-bucket-name your-bucket-name \
    --package-object-name function.zip \
    --environment YC_API_KEY=... \
    # ... all other env vars
```

### Installing dependencies for Linux runtime

The `deploy/deps/` directory currently contains files compiled for Windows (`_cffi_backend.cp313-win_amd64.pyd`, `_cffi_backend.cp314-win_amd64.pyd`). These will not work on the Linux x86_64 Yandex Cloud Functions runtime. When updating dependencies, install them using Docker:

```bash
docker run --rm -v "$(pwd)/deploy/deps:/deps" python:3.12-slim \
    pip install openai httpx supabase python-dotenv yandex-ai-studio-sdk --target /deps
```

### Scheduling

Create a timer trigger to invoke the function on a schedule. Recommended frequency: weekly (every Monday at 08:00 Moscow time). Yandex Cloud cron uses UTC — Moscow is UTC+3:

```bash
yc serverless trigger create timer \
    --name brand-mention-monitor-weekly \
    --cron-expression "0 5 * * 1" \
    --invoke-function-name brand-mention-monitor \
    --invoke-function-service-account-id <service-account-id>
```

---

## 11. Local Development Setup

```bash
# Clone and set up virtualenv
cd Dev-Platform/agents/brand-mention-monitor
python -m venv .venv
source .venv/Scripts/activate   # Windows (Git Bash)
# or: source .venv/bin/activate  # Linux/macOS

# Install dependencies (includes yandex-ai-studio-sdk)
pip install -r requirements.txt

# Create .env file (never commit this)
cat > .env << 'EOF'
YC_API_KEY=your_yandex_cloud_api_key
YC_FOLDER_ID=your_folder_id
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=agent@ddvb.tech
SMTP_PASSWORD=your_smtp_password
EOF

# Validate the pipeline without saving or emailing
python main.py --dry-run --verbose

# Run the full PoC interactively (no DB or email, prints to terminal)
python poc_search.py
```

---

## 12. Testing Instructions

Tests are in `tests/` and use pytest.

```bash
# Run all tests
pytest tests/ -v

# Run specific test modules
pytest tests/test_dedup.py -v
pytest tests/test_parsing.py -v
pytest tests/test_agent_parsing.py -v

# Run with coverage (if pytest-cov is installed)
pytest tests/ --cov=. --cov-report=term-missing
```

### Test coverage

**`tests/test_dedup.py`** — `TestDeduplication`, `TestBlockedDomainFiltering`, `TestExcludeDomainFiltering`

Tests URL normalization (trailing slash, query parameters, hash fragments, `www.` prefix) and verify that duplicates are detected correctly across both the in-session seen set and the existing Supabase URL set. Also tests domain filtering — own domains, social media (verify they are NOT blocked at this layer), WHOIS/SEO tools, search engines, and `www.` variants of blocked domains.

**`tests/test_parsing.py`** — `TestParseSearchXml`, `TestCleanHtml`

Tests XML parsing using a self-contained mock XML document encoded as base64. Verifies URL extraction, `www.` stripping from domain, HTML tag cleaning (including `<hlword>` highlight tags), passage joining, `max_results` limit, and graceful handling of empty or missing `rawData`. The `_clean_html` function is tested separately for tags, nested elements, self-closing tags, and whitespace stripping.

**`tests/test_agent_parsing.py`** — `TestMentionNormalization`

Tests that the generative search result dict has all required fields and that domain extraction from URLs works correctly (including `www.` stripping). Does not require API access — all assertions are on in-memory data structures.

### What is not tested

- `search_and_classify` (generative search) — requires live AI Studio SDK call
- `classify_relevance` and `summarize_mention` — require live YandexGPT API calls
- `search_web` — requires live Yandex Search API calls
- `_verify_page_mentions_brand` — requires live HTTP fetches
- `save_mentions`, `load_settings`, `get_existing_urls` — require a live Supabase connection
- Email sending — requires an SMTP server
- Brand gate, year filter, TLD filter — not yet covered by unit tests (logic is in `main.py`)

For integration testing of the full pipeline, use `python main.py --dry-run --verbose` with real credentials.

---

## 13. Key Design Decisions

### Why Yandex AI Studio SDK is the primary search method

The generative search SDK combines Yandex's full-text web index with LLM reasoning in a single call. The AI reads complete page content and makes a contextual judgment about whether a result is a genuine DDVB branding agency mention — something impossible with a keyword search API. This also solves the "DDVB" homonym problem (VAG engine code) without post-processing heuristics.

### Why Search API v2 is retained as a fallback

Generative search is limited by the AI model's page selection. The Search API v2 provides deterministic, exhaustive retrieval from specific domains via `site:` filtering, which ensures coverage of target publications even if the generative model does not surface them. The two-source merge gives both precision (agent) and recall (API).

### Why "ДДВБ" is excluded from the Search API v2

The Cyrillic query generates massive noise: VAG engine code descriptions, random Cyrillic strings, and unrelated automotive content all contain "ДДВБ". A keyword-based search cannot distinguish these without page-level reading. The AI Studio generative search handles the Cyrillic query because the LLM can reject automotive contexts from prompt context alone.

### Why social media is not blanket-blocked

Earlier versions blocked `t.me`, `vk.com`, and similar platforms. This was removed because third-party editorial coverage — a journalist's Telegram channel, a trade publication's VK community — represents genuine brand mentions. These pass through layer 2 and are assessed by the brand gate, year filter, page verification, and YandexGPT classifier, which together filter out re-posts and aggregators without editorial content.

### Why the brand gate (layer 3) exists separately from AI classification (layer 6)

The YandexGPT classifier is fail-open (returns "relevant" on error) and processes input capped at 1000 characters. A result with no "DDVB" in the title or snippet cannot be a brand mention by definition — running AI classification on it wastes API quota and risks false positives when the snippet is truncated. The brand gate is a zero-cost deterministic pre-filter.

### Why page verification (layer 5) is needed

Yandex's search API includes keyword context from its index, which may highlight "DDVB" in a passage even if the current page content does not mention it (e.g., the page was updated since indexing, or the highlighting is from anchor text). Fetching the actual page and checking HTML content catches these false positives before they reach the AI classifier.

### Why fail-open on classification and page fetch errors

The cost of a missed genuine mention (PR opportunity not tracked) outweighs the cost of a false positive (a person manually reviews one irrelevant entry in the email digest). False positives are caught by human review of the digest.

### Why the blocked domain list is a hardcoded set

Blocked domains (own sites, WHOIS tools, SEO analyzers, car parts classifieds, spam) represent categories that will structurally never produce editorial mentions. Making them configurable via the database would add complexity with no practical benefit. The `mention_settings` table controls parameters that legitimately change over time (target publications, search queries).

### Why the `deploy/` directory exists as a flat copy

Yandex Cloud Functions for Python expects all imports to be resolvable relative to the function root. The simplest packaging approach is to copy all source modules and pre-installed dependencies into one directory and zip it. The trade-off is that `deploy/` must be kept in sync with source changes manually.

### Why the OpenAI SDK is used for YandexGPT

Yandex Cloud's LLM API is OpenAI-compatible (same endpoints, same request/response shape). Using the OpenAI SDK avoids writing a custom HTTP client for LLM calls. The model URI (`gpt://...`) and the `Api-Key` auth header are the only differences from a standard OpenAI call.

---

## 14. Operational Notes

### Pipeline summary dict

The `handler` returns a summary dict in the response body with the following shape (updated to reflect the current 10-stage numbering):

```json
{
  "statusCode": 200,
  "body": {
    "total_raw": 24,
    "agent_found": 6,
    "api_found": 18,
    "after_dedup": 16,
    "after_filter": 11,
    "relevant": 4,
    "saved": 4
  }
}
```

The field `after_filter` reflects the count after the blocklist/TLD filter only. Counts for the brand gate, year filter, and page verification stages are logged at `INFO` level but not included in the summary dict.

### Monitoring

The function logs at `INFO` level for all pipeline steps and at `DEBUG` level for brand gate rejections and individual classification decisions. In Yandex Cloud Functions, logs are accessible through the console under the function's execution history.

### Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| No email received, no error | `SMTP_HOST` is empty | Set SMTP environment variables |
| `KeyError: 'YC_API_KEY'` on startup | Missing required env var | Add to Cloud Function configuration |
| Generative search returns 0 results | AI Studio SDK not initialized (no `YC_API_KEY`) or no internet access from function | Verify `YC_API_KEY` and function network settings |
| Search API returns 0 results consistently | `YC_API_KEY` lacks Search API permission | Add `search.yandex.net` permission to the key |
| Classification returns all `irrelevant` | YandexGPT model URI wrong (wrong folder ID) | Verify `YC_FOLDER_ID` |
| `save_mentions` saves 0 rows | Supabase `brand_mentions` table missing unique constraint on `url` | Add unique index on `url` column |
| All results filtered by brand gate | Snippet empty (generative results have empty snippet) and title also lacks DDVB | Check that generative sources have title populated by SDK |
| Function upload fails with size error | `function.zip` exceeds 3.5 MB console limit | Upload via Object Storage (see Section 10) |
| `ImportError: yandex_ai_studio_sdk` | Package not in `deploy/deps/` | Re-run `pip install yandex-ai-studio-sdk --target deploy/deps/` on Linux |

### The `sostav.ru` exclusion

Note that `sostav.ru` appears in both `DEFAULT_TARGET_DOMAINS` (Batch A site filter) and `DEFAULT_EXCLUDE_DOMAINS`. This means Batch A searches sostav.ru for content, but those results are removed by `filter_blocked()` before the brand gate. This may be intentional (validating that sostav.ru coverage exists) or a configuration inconsistency. Check the `mention_settings` table in Supabase to see if this has been overridden.

---

## 15. Relationship to Existing README.md

The existing `README.md` at the project root contains only a one-line description:

```
PR Brand Mention Monitor — automated media mention tracking for DDVB TECH
```

This `CLAUDE.md` supersedes it as the primary technical reference. The `docs/` directory contains additional detail on specific subsystems (`architecture.md`, `deployment.md`, `configuration.md`, `api-reference.md`, `operations.md`) that expand on sections of this file. For AI-assisted development, this `CLAUDE.md` is the authoritative summary source.
