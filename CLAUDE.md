# brand-mention-monitor — CLAUDE.md

Technical reference for Claude Code and developers working on the Brand Mention Monitor agent.

---

## 1. Project Overview

**Brand Mention Monitor** is a Python service that automatically discovers and tracks editorial mentions of the DDVB brand in Russian-language media. It runs as a scheduled Yandex Cloud Function and delivers results by email.

The service answers one business question: "Where has DDVB been mentioned in Russian trade press and business media this week?" It targets publications like sostav.ru, retail.ru, and new-retail.ru — industry sources that cover branding and marketing clients.

The pipeline is fully autonomous: it searches, classifies with AI, deduplicates against historical records, stores results in Supabase, and emails a formatted HTML digest to the team. A `--dry-run` flag allows safe inspection without side effects.

---

## 2. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                  Yandex Cloud Functions (scheduled)             │
│                                                                 │
│   handler(event, context)  ←── cron trigger                    │
│          │                                                      │
│          ▼                                                      │
│   run_pipeline()  [main.py]                                     │
│          │                                                      │
│   ┌──────┴───────────────────────────────────────────────┐     │
│   │                    PIPELINE STAGES                    │     │
│   │                                                       │     │
│   │  1. load_settings()      ←── mention_settings table  │     │
│   │         │                    (Supabase)               │     │
│   │  2. get_existing_urls()  ←── brand_mentions table     │     │
│   │         │                    (Supabase)               │     │
│   │  3. agent_search()       ──► AI Studio Responses API  │     │
│   │      [yandex_agent.py]       + WebSearch tool         │     │
│   │      (search+classify+       (Alice AI LLM)          │     │
│   │       summarize in 1 call)                            │     │
│   │         │                                             │     │
│   │  4. search_web()  ×N     ──► Yandex Search API v2     │     │
│   │      [yandex_ai.py]          (fallback, with fixes:   │     │
│   │      Batch A: site-restricted  date filter +          │     │
│   │      Batch B: broad web        50 results/page)       │     │
│   │         │                                             │     │
│   │  5. merge + deduplicate()  (agent-first priority)     │     │
│   │         │                                             │     │
│   │  6. filter_blocked()     (revised: no social media    │     │
│   │         │                 blanket block)              │     │
│   │  7. save_mentions()      ──► brand_mentions table     │     │
│   │         │                    (Supabase, with summary) │     │
│   │  8. send_digest()        ──► SMTP → email recipients  │     │
│   └───────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────┘

External APIs:
  AI Studio Responses API  https://ai.api.cloud.yandex.net/v1  (OpenAI-compatible, primary)
  Yandex Search API v2     https://searchapi.api.cloud.yandex.net/v2/web/searchAsync  (fallback)
  Yandex Operations API    https://operation.api.cloud.yandex.net/operations/{id}
  YandexGPT (OpenAI SDK)   https://llm.api.cloud.yandex.net/v1  (legacy, for manual use)
  Supabase REST            SUPABASE_URL (project-specific)
  SMTP                     Configurable host/port (supports STARTTLS and SSL)
```

### Module responsibilities

| File | Responsibility |
|---|---|
| `main.py` | Pipeline orchestration, CLI entry point, Cloud Function handler |
| `config.py` | Environment variable loading, constants, blocklist definition |
| `yandex_agent.py` | AI Studio Responses API client (primary: search + classify + summarize) |
| `yandex_ai.py` | Yandex Search API v2 (fallback: async search + XML parsing) and YandexGPT |
| `supabase_client.py` | Supabase reads (settings, existing URLs) and writes (upsert mentions) |
| `email_digest.py` | HTML email construction and SMTP delivery |
| `poc_search.py` | Interactive PoC script for manual validation (not used in production) |

---

## 3. How Search Works (Yandex Search API v2)

### Async search pattern

The Yandex Search API v2 is asynchronous by design. Every search goes through two HTTP calls:

1. **Submit** — POST to `searchAsync` with the query body; receive an operation ID immediately.
2. **Poll** — GET `operations/{id}` every 2 seconds until `done == true` (max 30 seconds / 15 attempts).

This is different from synchronous search APIs. The `search_web()` function in `yandex_ai.py` handles both calls and returns only when results are ready or the timeout is reached.

### Two-batch strategy

The pipeline runs two distinct search batches per query to maximize coverage:

**Batch A — domain-restricted:** Constructs a compound Yandex query using the `site:` operator. For queries `['"DDVB"', '"ДДВБ"']` and target domains `["sostav.ru", "retail.ru", "unipack.ru", "new-retail.ru"]`, the submitted query becomes:

```
("DDVB") (site:sostav.ru | site:retail.ru | site:unipack.ru | site:new-retail.ru)
```

This prioritizes high-value trade publications and ensures their content is not crowded out by broader results.

**Batch B — broad web:** Submits the raw query without any `site:` filter, catching mentions anywhere on the Russian web.

Each result is tagged with a `discovery_query` field indicating which batch and query produced it, which is stored in Supabase for provenance.

### XML response parsing

The Yandex Search API returns results as base64-encoded XML inside a JSON envelope:

```
operation_result["response"]["rawData"]  →  base64 decode  →  XML string
```

The XML is parsed with regex (not an XML library) extracting `<group>` blocks that each represent one result. From each group, the parser extracts `<url>`, `<title>`, `<domain>`, and `<passage>` elements. `<hlword>` highlight tags within titles and passages are stripped by `_clean_html()` using a simple tag-removal regex. Up to two passages are joined with a space to form the snippet.

### Retry logic

The HTTP client retries on status codes `{403, 429, 500, 502, 503, 504}` with exponential backoff of 2, 4, 8 seconds across a maximum of 3 attempts. Rate limiting between all API calls (search and LLM) is enforced by `YANDEX_RATE_LIMIT_SECONDS = 1.0` sleeps in the pipeline loop.

---

## 4. AI Classification and Summarization

Both operations use YandexGPT Lite through the OpenAI Python SDK configured with the Yandex Cloud endpoint:

```
base_url = "https://llm.api.cloud.yandex.net/v1"
model    = "gpt://{YC_FOLDER_ID}/yandexgpt-lite/latest"
auth     = Api-Key header (not Bearer token)
```

The OpenAI SDK is used as a convenience wrapper — the actual model is YandexGPT, not OpenAI.

### Classification (`classify_relevance`)

Takes a title and snippet (capped at 1000 characters), calls YandexGPT Lite at temperature 0.0 with `max_tokens=10`. The system prompt instructs the model to respond with exactly one word: `relevant` or `irrelevant`.

The classification criteria encoded in the system prompt:
- **Relevant:** article, news item, case study, industry ranking, review, or analytics piece that mentions DDVB
- **Irrelevant:** WHOIS/domain service, SEO analyzer, company directory without editorial content, link aggregator, search results page, social network

If the API call fails, `classify_relevance` returns `"relevant"` (fail-open). This is an intentional design choice — it is better to process a false positive than to silently drop a genuine mention.

### Summarization (`summarize_mention`)

Only called for results already classified as relevant. Uses temperature 0.3, `max_tokens=200`. The system prompt requests a 2–3 sentence Russian-language summary describing the mention context and key information. On failure, falls back to the first 200 characters of the snippet.

Note: the `summary` field is generated and attached to the mention dict before saving to Supabase, but the current `save_mentions()` function does not include `summary` in the upserted row schema. The summary is available in memory for use by the email digest template, but is not persisted separately.

---

## 5. Database Schema (Supabase)

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
| `discovery_source` | text | Always `"yandex_search_api"` (hardcoded) |

Supabase auto-manages `id`, `created_at`, and `updated_at` columns if configured in the table definition.

### `mention_settings`

Key-value configuration table that allows changing pipeline parameters without redeployment. The service reads this table at startup and falls back to `config.py` defaults if the table is empty or Supabase is unreachable.

Expected row structure (the service handles both `key`/`value` and `setting_key`/`setting_value` column naming):

| `key` | Expected value type | Default |
|---|---|---|
| `target_domains` | list of strings | `["sostav.ru", "retail.ru", "unipack.ru", "new-retail.ru"]` |
| `search_queries` | list of strings | `['"DDVB"', '"ДДВБ"']` |
| `exclude_domains` | list of strings | `["sostav.ru"]` |

### `originating_publications`

A third table is referenced by `get_originating_publications()` in `supabase_client.py` but is not used in the current pipeline. It is likely intended for a future feature that tracks which publications are being actively monitored rather than discovered.

---

## 6. Email Digest Flow

The email module (`email_digest.py`) builds and sends an HTML email using Python's standard library `smtplib` and `email.mime`.

### Two message types

**Digest with mentions** (`send_digest`): Called when at least one relevant mention was found. Renders an HTML table where each row shows the article title as a hyperlink, the source domain as a pill badge, and the snippet truncated to 300 characters. The subject line and date are formatted in `dd.mm.yyyy` format. The email is in Russian context but labels are in English ("Coverage count", "Generated by Brand Mention Monitor").

**Empty notification** (`send_empty_notification`): Called when the pipeline finds no new relevant mentions after deduplication and filtering. Sends a brief notification rather than silence — this is important so recipients can distinguish "no mentions found" from "the system failed to run."

### SMTP transport selection

The `_send_email` function selects the connection method based on port:
- Ports `465` or `1127`: `smtplib.SMTP_SSL` (immediate TLS)
- All other ports (including `587`): `smtplib.SMTP` with `STARTTLS` upgrade

Authentication with `SMTP_USER` and `SMTP_PASSWORD` is applied when both are non-empty. The sender address defaults to `agent@ddvb.tech`.

Recipients are hardcoded in `config.py` as `DEFAULT_RECIPIENTS = ["ilya@ddvb.tech", "maria@ddvb.tech"]`. There is no mechanism to override recipients at runtime without changing the source or environment.

---

## 7. Deployment (Yandex Cloud Functions)

### How deployment works

The `deploy/` directory contains a self-contained copy of the production code alongside a `deps/` directory with all Python dependencies pre-installed as importable packages. This is the artifact uploaded to Yandex Cloud Functions — the function runtime finds packages by directory co-location rather than through `pip install`.

The `function.zip` at the project root is the deployment archive built from `deploy/`.

### Cloud Function entry point

The Yandex Cloud Functions runtime calls:

```python
handler(event, context)  # in main.py
```

This function configures logging and delegates to `run_pipeline(dry_run=False)`. It returns `{"statusCode": 200, "body": summary_dict}`.

Environment variables are injected through the Yandex Cloud Functions configuration UI or CLI — not through a `.env` file (which is only used for local development).

### Keeping `deploy/` in sync

`deploy/main.py` is a copy of root `main.py` — both are currently identical. When modifying the production pipeline, update both files. Similarly for `deploy/email_digest.py`, `deploy/supabase_client.py`, and `deploy/yandex_ai.py`.

There is no automated sync mechanism. The recommended workflow is:

```bash
# 1. Edit source files at project root
# 2. Copy updated files to deploy/
cp main.py deploy/main.py
cp yandex_ai.py deploy/yandex_ai.py
cp supabase_client.py deploy/supabase_client.py
cp email_digest.py deploy/email_digest.py
cp config.py deploy/config.py

# 3. Rebuild the zip
cd deploy && zip -r ../function.zip . && cd ..

# 4. Upload to Yandex Cloud Functions via console or CLI
yc serverless function version create \
  --function-name brand-mention-monitor \
  --runtime python312 \
  --entrypoint main.handler \
  --source-path function.zip \
  --environment YC_API_KEY=... \
  --environment YC_FOLDER_ID=... \
  --environment SUPABASE_URL=... \
  --environment SUPABASE_SERVICE_ROLE_KEY=... \
  --environment SMTP_HOST=... \
  --environment SMTP_PORT=587 \
  --environment SMTP_USER=... \
  --environment SMTP_PASSWORD=...
```

### Scheduling

The function should be invoked on a schedule using Yandex Cloud Scheduler (or a timer trigger configured on the function). Recommended frequency: weekly (e.g., every Monday morning). The service does not enforce its own schedule — it runs once per invocation.

---

## 8. Configuration (Environment Variables)

All configuration is loaded by `config.py` from environment variables via `python-dotenv`. For local development, create a `.env` file at the project root (not committed to git).

### Required

| Variable | Description |
|---|---|
| `YC_API_KEY` | Yandex Cloud API key. Used for both Search API v2 and YandexGPT (same key). Must have permissions for `search.yandex.net` and `llm.yandex.net` services. |
| `YC_FOLDER_ID` | Yandex Cloud folder (project) ID. Used in YandexGPT model URIs: `gpt://{YC_FOLDER_ID}/yandexgpt-lite/latest` |

### Optional (with defaults)

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_URL` | `""` | Supabase project URL. If empty, all Supabase operations are skipped gracefully. |
| `SUPABASE_SERVICE_ROLE_KEY` | `""` | Supabase service role key (bypasses Row Level Security). Use the service role key, not the anon key — the anon key will not have write permissions by default. |
| `SMTP_HOST` | `""` | SMTP server hostname. If empty, email sending is skipped with a warning. |
| `SMTP_PORT` | `587` | SMTP port. Use `465` for SSL, `587` for STARTTLS. |
| `SMTP_USER` | `""` | SMTP authentication username. |
| `SMTP_PASSWORD` | `""` | SMTP authentication password. |
| `SMTP_FROM` | `"agent@ddvb.tech"` | Sender address in outgoing emails. |

### Hardcoded constants (change in `config.py`)

| Constant | Value | Description |
|---|---|---|
| `DEFAULT_TARGET_DOMAINS` | `["sostav.ru", "retail.ru", "unipack.ru", "new-retail.ru"]` | Trade publications for domain-restricted Batch A search. Override via `mention_settings` table. |
| `DEFAULT_SEARCH_QUERIES` | `['"DDVB"', '"ДДВБ"']` | Quoted search terms for both Latin and Cyrillic brand name. Override via `mention_settings` table. |
| `DEFAULT_EXCLUDE_DOMAINS` | `["sostav.ru"]` | Domains excluded after search (separate from the blocked list). Override via `mention_settings` table. |
| `YANDEX_RATE_LIMIT_SECONDS` | `1.0` | Sleep between all Yandex API calls (search and LLM). The generative API has a 1 req/sec rate limit. |
| `MAX_DOMAINS_PER_BATCH` | `5` | Reference constant for the `site:` filter limit. Not enforced in current code. |
| `BLOCKED_DOMAINS` | (set of ~20 domains) | Permanent blocklist of DDVB's own domains, social media, search engines, WHOIS/SEO tools, and generic directories. These are never editorial mentions. |

---

## 9. Local Development Setup

```bash
# Clone and set up virtualenv
cd Dev-Platform/agents/brand-mention-monitor
python -m venv .venv
source .venv/Scripts/activate   # Windows (Git Bash)
# or: source .venv/bin/activate  # Linux/macOS

# Install dependencies
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

## 10. Testing Instructions

Tests are in `tests/` and use pytest. They require no external API access — all Yandex API responses are mocked using base64-encoded XML fixtures.

```bash
# Run all tests
pytest tests/ -v

# Run specific test class
pytest tests/test_dedup.py -v
pytest tests/test_parsing.py -v

# Run with coverage (if pytest-cov is installed)
pytest tests/ --cov=. --cov-report=term-missing
```

### Test coverage

**`tests/test_dedup.py`** — `TestDeduplication`, `TestBlockedDomainFiltering`, `TestExcludeDomainFiltering`

Tests URL normalization (trailing slash, query parameters, hash fragments, `www.` prefix) and verify that duplicates are detected correctly across both the in-session seen set and the existing Supabase URL set. Also tests domain filtering — own domains, social media, WHOIS/SEO tools, search engines, and `www.` variants of blocked domains.

**`tests/test_parsing.py`** — `TestParseSearchXml`, `TestCleanHtml`

Tests XML parsing using a self-contained mock XML document encoded as base64. Verifies URL extraction, `www.` stripping from domain, HTML tag cleaning (including `<hlword>` highlight tags), passage joining, `max_results` limit, and graceful handling of empty or missing `rawData`. The `_clean_html` function is tested separately for tags, nested elements, self-closing tags, and whitespace stripping.

### What is not tested

- `classify_relevance` and `summarize_mention` — require live YandexGPT API calls
- `search_web` — requires live Yandex Search API calls
- `save_mentions`, `load_settings`, `get_existing_urls` — require a live Supabase connection
- Email sending — requires an SMTP server

For integration testing of the full pipeline, use `python main.py --dry-run` with real credentials.

---

## 11. Key Design Decisions

### Why Yandex Search API instead of a general web crawler

The service targets Russian media specifically. Yandex is the dominant search engine for Russian-language web content, and its `SEARCH_TYPE_RU` mode is tuned for Russian-language relevance ranking. This avoids the need to maintain a custom crawler or deal with anti-scraping protections on individual publication sites.

### Why YandexGPT for classification instead of a keyword filter

Simple keyword matching on the brand name would produce many false positives: WHOIS records, SEO analyzer reports, link directories, and social media aggregators all contain "DDVB" without being editorial mentions. A language model classifier can distinguish "sostav.ru published a case study about a DDVB client project" from "cy-pr.com shows domain metrics for ddvb.ru". The classification prompt is tightly scoped to a binary decision (one word output), which makes it fast and low-cost using YandexGPT Lite.

### Why fail-open on classification errors

`classify_relevance` returns `"relevant"` when the API call fails. The cost of a missed genuine mention (PR opportunity not tracked) outweighs the cost of a false positive (a person manually reviews one irrelevant entry). False positives are caught by the editorial review of the email digest.

### Why two search batches (domain-restricted + broad)

Batch A with `site:` filters ensures that the highest-value publications (industry trade press) are always covered even when a broad search might not surface them at the top of results. Batch B catches unexpected mentions on sites not in the target list. The two batches can produce overlapping results, which the deduplication step handles.

### Why the blocked domain list is a hardcoded set

Blocked domains (DDVB own sites, social media, WHOIS services, SEO tools) represent categories that will never produce editorial mentions regardless of query. Making them configurable via the database would add complexity with no practical benefit — these categories are structurally not editorial content. The `mention_settings` table controls the search parameters that change over time (which publications to target, what queries to use).

### Why the `deploy/` directory exists as a flat copy

Yandex Cloud Functions for Python expects all imports to be resolvable relative to the function root. The simplest packaging approach is to copy all source modules and pre-installed dependencies into one directory and zip it. This avoids the need for a build system or Docker image. The trade-off is that `deploy/` must be kept in sync with source changes manually.

### Why the OpenAI SDK is used for YandexGPT

Yandex Cloud's LLM API is OpenAI-compatible (same endpoints, same request/response shape). Using the OpenAI SDK avoids writing a custom HTTP client for LLM calls and leverages the SDK's retry and streaming handling. The model URI (`gpt://...`) and the `Api-Key` auth header are the only differences from a standard OpenAI call.

---

## 12. Operational Notes

### Monitoring

The function logs at `INFO` level for all pipeline steps and at `DEBUG` level for individual classification decisions. In Yandex Cloud Functions, logs are accessible through the Yandex Cloud console under the function's execution history. The `handler` returns a summary dict in the response body:

```json
{
  "statusCode": 200,
  "body": {
    "total_searched": 18,
    "after_dedup": 12,
    "after_filter": 9,
    "relevant": 3,
    "saved": 3
  }
}
```

### Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| No email received, no error | `SMTP_HOST` is empty | Set SMTP environment variables |
| `KeyError: 'YC_API_KEY'` on startup | Missing required env var | Add to Cloud Function configuration |
| Search returns 0 results consistently | `YC_API_KEY` lacks Search API permission | Add `search.yandex.net` permission to the key |
| Classification returns all `irrelevant` | YandexGPT model URI is wrong (wrong folder ID) | Verify `YC_FOLDER_ID` |
| `save_mentions` saves 0 rows | Supabase `brand_mentions` table missing unique constraint on `url` | Add unique index on `url` column |
| All results filtered out | `DEFAULT_EXCLUDE_DOMAINS` overlaps with `target_domains` | Review config — `sostav.ru` is in both by default |

### The `sostav.ru` exclusion

Note that `sostav.ru` appears in both `DEFAULT_TARGET_DOMAINS` (Batch A site filter) and `DEFAULT_EXCLUDE_DOMAINS`. This means Batch A searches sostav.ru for content, those results are tagged with their discovery query, but they are then removed by `filter_blocked()` before classification. This may be intentional (the search validates that sostav.ru has content but it is currently excluded from the digest) or a configuration inconsistency. Check the `mention_settings` table in Supabase to see if this has been overridden.

---

## 13. Relationship to Existing README.md

The existing `README.md` at the project root contains only a one-line description:

```
PR Brand Mention Monitor — automated media mention tracking for DDVB TECH
```

This `CLAUDE.md` supersedes it as the primary technical reference. The `README.md` could be expanded to include a brief project description, setup instructions, and a pointer to this file, but it does not need to duplicate the detailed content here. For AI-assisted development, this `CLAUDE.md` is the authoritative source.
