# Configuration Reference — Brand Mention Monitor

All configuration is loaded by `config.py` at module import time. The module uses `python-dotenv` to read a `.env` file at the project root before falling back to the system environment. In production (Yandex Cloud Functions), variables are injected directly into the runtime environment through the Cloud Functions UI or CLI — no `.env` file is present.

---

## Required Environment Variables

These two variables are loaded with `os.environ["KEY"]` — if either is absent, `config.py` raises a `KeyError` at import time, which crashes the entire service on startup. Because all modules import from `config.py`, this failure happens immediately before any code executes.

### `YC_API_KEY`

**Type:** `str`
**Source:** `os.environ["YC_API_KEY"]`
**Used by:** `yandex_agent.py` (AI Studio SDK constructor), `yandex_ai.py` (shared `httpx.Client` header and OpenAI SDK `default_headers`)

The Yandex Cloud IAM static API key. A single key is reused for all four Yandex services:
- Yandex AI Studio SDK — generative search (`sdk.search_api.generative()`)
- Yandex Search API v2 (`searchapi.api.cloud.yandex.net`)
- Yandex Operations API (`operation.api.cloud.yandex.net`)
- YandexGPT / Foundation Models (`llm.api.cloud.yandex.net`)

The key must be authorized for `search.yandex.net`, `llm.yandex.net`, and AI Studio generative search. These permissions are configured when creating the key in the Yandex Cloud IAM console. If the key has Search API permission but not LLM permission (or vice versa), some pipeline stages will fail silently.

**Format:** A long alphanumeric string beginning with `AQVN...` or similar.

---

### `YC_FOLDER_ID`

**Type:** `str`
**Source:** `os.environ["YC_FOLDER_ID"]`
**Used by:** `config.py` (constructs model URIs), `yandex_agent.py` (AI Studio SDK constructor), `yandex_ai.py` (search request body `folderId`)

The Yandex Cloud folder (project) ID where the Cloud Function and API permissions are configured. It is used in three places:

1. **AI Studio SDK initialization** — `AIStudio(folder_id=YC_FOLDER_ID, auth=YC_API_KEY)` in `yandex_agent.py`
2. **YandexGPT model URIs** — constructed in `config.py`:
   ```
   YANDEX_GPT_LITE = "gpt://{YC_FOLDER_ID}/yandexgpt-lite/latest"
   ```
3. **Search request body** — `"folderId": YC_FOLDER_ID` in every Search API v2 POST

An incorrect `YC_FOLDER_ID` causes model API calls to return 404 or permission errors.

**Format:** `b1g...` — an alphanumeric string visible in the Yandex Cloud console URL bar.

---

## Optional Environment Variables

These variables are loaded with `os.getenv("KEY", default)`. Missing optional variables disable specific features gracefully rather than crashing the service.

### `AI_STUDIO_API_KEY`

**Type:** `str`
**Default:** `""` (empty string)
**Source:** `os.getenv("AI_STUDIO_API_KEY", "")`
**Used by:** Defined in `config.py` but not currently read by any module in the pipeline

Reserved for a potential future configuration where a separate Yandex AI Studio key is required. Currently the generative search SDK is initialized with `YC_API_KEY` directly. Set this variable if Yandex introduces separate authentication for AI Studio in the future. Having it defined causes no issues — it is silently unused.

---

### Supabase Variables

#### `SUPABASE_URL`

**Type:** `str`
**Default:** `""` (empty string)
**Used by:** `supabase_client.py` — passed as first argument to `supabase.create_client()`

The Supabase project URL in the format `https://{project-ref}.supabase.co`. Found in the Supabase dashboard under Project Settings > API.

**Effect of empty value:** When `SUPABASE_SERVICE_ROLE_KEY` is empty, `get_client()` returns `None` and all Supabase operations are silently skipped:
- `load_settings()` returns `config.py` defaults
- `get_existing_urls()` returns an empty set
- `save_mentions()` returns 0 without writing

---

#### `SUPABASE_SERVICE_ROLE_KEY`

**Type:** `str`
**Default:** `""` (empty string)
**Used by:** `supabase_client.py` — passed as second argument to `supabase.create_client()`

The service role JWT from the Supabase dashboard under Project Settings > API > `service_role` key. This key bypasses Row Level Security (RLS) policies. The `anon` key will not have write permission to `brand_mentions` by default.

**Security note:** The service role key has full database access. Do not expose it in client-side code or commit it to version control. In production, set it as a secret environment variable in the Yandex Cloud Functions configuration.

---

### SMTP Variables

#### `SMTP_HOST`

**Type:** `str`
**Default:** `""` (empty string)
**Used by:** `email_digest._send_email()`

SMTP server hostname. If empty, `_send_email()` logs a warning and returns `False` without attempting a connection. This is the primary gate — all other SMTP variables are irrelevant if `SMTP_HOST` is not set.

**Examples:** `smtp.gmail.com`, `smtp.mail.ru`, `smtp.yandex.ru`

---

#### `SMTP_PORT`

**Type:** `int`
**Default:** `587`
**Used by:** `email_digest._send_email()`

SMTP port number. The current `_send_email()` implementation uses STARTTLS exclusively — `smtplib.SMTP` + `server.starttls()`. Port 587 is the standard submission port for STARTTLS. The port-based SSL branch (which used `smtplib.SMTP_SSL` for ports 465/1127) has been removed from this version.

---

#### `SMTP_USER`

**Type:** `str`
**Default:** `""` (empty string)
**Used by:** `email_digest._send_email()`

SMTP authentication username. If empty (or `SMTP_PASSWORD` is empty), `server.login()` is not called.

---

#### `SMTP_PASSWORD`

**Type:** `str`
**Default:** `""` (empty string)
**Used by:** `email_digest._send_email()`

SMTP authentication password. Authentication is only attempted when both `SMTP_USER` and `SMTP_PASSWORD` are non-empty.

---

#### `SMTP_FROM`

**Type:** `str`
**Default:** `"agent@ddvb.tech"`
**Used by:** `email_digest._send_email()`

Sender address for outgoing emails. Appears in the `From:` header and is used as the `MAIL FROM` envelope address. Must be authorized to send via the configured SMTP server.

---

## Hardcoded Constants

Defined directly in `config.py` and can only be changed by editing the source and redeploying.

### Search Defaults (overridable via `mention_settings` table)

#### `DEFAULT_TARGET_DOMAINS`

**Type:** `list[str]`
**Value:** `["sostav.ru", "retail.ru", "unipack.ru", "new-retail.ru"]`

The trade publications used in Search API v2 Batch A (domain-restricted search). The pipeline constructs a compound `site:` filter query from these domains.

Override via the `mention_settings` table with key `target_domains` and a JSON array value.

---

#### `DEFAULT_SEARCH_QUERIES`

**Type:** `list[str]`
**Value:** `['"DDVB"', '"ДДВБ"']`

Both Latin and Cyrillic brand name variants. Both are passed to the AI Studio generative search. Only Latin queries (`"DDVB"`) are passed to the Search API v2 — Cyrillic is excluded at `main.py:176` to prevent VAG engine code noise.

Override via the `mention_settings` table with key `search_queries`.

---

#### `DEFAULT_EXCLUDE_DOMAINS`

**Type:** `list[str]`
**Value:** `["sostav.ru"]`

Domains excluded by `filter_blocked()` (Layer 2) in addition to the hardcoded `BLOCKED_DOMAINS` set. Override via the `mention_settings` table with key `exclude_domains`.

**Important quirk:** `sostav.ru` appears in both `DEFAULT_TARGET_DOMAINS` and `DEFAULT_EXCLUDE_DOMAINS`. Batch A actively searches sostav.ru (generating domain-restricted results), but those results are then removed by Layer 2 before classification. Sostav.ru content is never saved or emailed. This may be intentional (monitored separately) or a misconfiguration — check the `mention_settings` table to see if the default has been overridden.

---

### Search API Configuration (new in current version)

#### `SEARCH_RESULTS_PER_PAGE`

**Type:** `int`
**Value:** `50`

The `groupsOnPage` value passed in the `groupSpec` of each Search API v2 request. Controls how many result groups Yandex returns per async search. Also used as the `max_results` default in `search_web()`. Increasing this value beyond 50 may not be honored by the Yandex Search API depending on the account tier.

---

#### `SEARCH_DATE_RESTRICT_DAYS`

**Type:** `int`
**Value:** `7`

Look-back window in days for both generative search and Search API v2 date filters. Used in `run_pipeline()`:
```python
date_from = (datetime.now() - timedelta(days=SEARCH_DATE_RESTRICT_DAYS)).strftime("%Y-%m-%d")
```
The resulting `date_from` string is passed to both `agent_search()` and `search_web()`. For Search API v2, this becomes both the `date:>YYYYMMDD` query suffix and triggers the `PERIOD_2_WEEKS` native filter.

---

### Rate Limiting Constants

#### `YANDEX_RATE_LIMIT_SECONDS`

**Type:** `float`
**Value:** `1.0`

Seconds to sleep between Yandex API calls in `run_pipeline()`. Applies to Search API v2 calls and YandexGPT classifier calls. The generative search SDK applies its own internal 1-second sleep between queries in `yandex_agent.py`. Reducing this value risks HTTP 429 rate limit errors from the LLM API.

---

#### `MAX_DOMAINS_PER_BATCH`

**Type:** `int`
**Value:** `5`

A reference constant noting the practical limit on domains per `site:` filter batch. Not enforced in the current code — all `target_domains` are passed to a single `site:` filter. Serves as a reminder that if `target_domains` grows significantly, the query may need to be split.

---

### Blocked Domains

#### `BLOCKED_DOMAINS`

**Type:** `set[str]`
**Size:** Approximately 50 entries

The hardcoded permanent blocklist used by `filter_blocked()` (Layer 2). These domains are structurally excluded — they will never contain editorial brand mentions regardless of query.

| Category | Examples |
|---|---|
| DDVB own resources | `ddvb.ru`, `www.ddvb.ru`, `ddvb.tech`, `www.ddvb.tech` |
| Search engines | `yandex.ru`, `google.com`, `google.ru` |
| WHOIS / SEO tools | `cy-pr.com`, `2whois.ru`, `whois.ru`, `pr-cy.ru`, `tapki.com`, `yapl.ru`, `reddial.ru`, `similarweb.com`, `semrush.com`, `alexa.com` |
| Web archives | `webarchive.org`, `web.archive.org` |
| Generic directories | `catalog.tools` |
| Classifieds / car parts | `avito.ru`, `auto.ru`, `drom.ru`, `baza.drom.ru`, `farpost.ru`, `arpshop.ru`, `avdauto.com`, `autocompas.ru`, `vodila.by`, `zap.by`, `partsouq.com`, `hepsiburada.com`, `newpartsricambi.com`, `dviglo.by` |
| Yandex aggregator pages | `tel.yandex.by`, `tel.yandex.ru` |
| Spam / foreign noise | `javip.net`, `obdrgg.cn`, `lichess.org`, `wolf-power.ch`, `blockchain.com`, and others |
| Visual / design aggregators | `ru.pinterest.com`, `pinterest.com`, `behance.net`, `referest.ru`, `drive2.ru` |

Car parts sites are blocked because "DDVB" is also a VAG/Audi engine code designation — these domains produce high-volume false positives. Social media (`t.me`, `vk.com`, etc.) is explicitly NOT in this set — see `SOCIAL_MEDIA_DOMAINS`.

This list is not configurable at runtime. To add or remove entries, edit `config.py` and redeploy.

---

#### `SOCIAL_MEDIA_DOMAINS`

**Type:** `set[str]`
**Value:** `{"t.me", "vk.com", "ok.ru", "instagram.com", "facebook.com", "twitter.com"}`

A documentation constant only — this set is NOT added to `BLOCKED_DOMAINS` and is NOT used in any filter logic. Social media platforms are intentionally allowed through the pipeline because third-party editorial mentions published on Telegram channels or VK pages are legitimate brand coverage. These results pass Layer 2 and are assessed by the generative AI (for agent results) or the YandexGPT classifier (for Search API results) at Layer 6.

The separation of `SOCIAL_MEDIA_DOMAINS` from `BLOCKED_DOMAINS` documents the deliberate architectural choice to allow social media through.

---

### TLD Allowlist

#### `ALLOWED_TLDS`

**Type:** `set[str]`
**Defined in:** `main.py` (not `config.py`)
**Value:** `{"ru", "su", "by", "kz", "uz", "ua", "me", "com", "net", "org", "io", "info", "agency", "tech", "asia", "pro", "one", "app"}`

The set of TLDs that can legitimately contain DDVB editorial mentions. Applied by `filter_blocked()` (Layer 2) after the domain blocklist check. Domains with TLDs outside this set are rejected as foreign noise.

This allowlist blocks foreign country TLDs (`.de`, `.uk`, `.fr`, `.shop`, etc.) that have appeared in search results containing "DDVB" as a random string, without requiring individual domain entries in `BLOCKED_DOMAINS`. The allowlist approach is more maintainable than an ever-growing blocklist for foreign domains.

**Location note:** `ALLOWED_TLDS` is defined in `main.py` rather than `config.py` because it is implementation logic for the `filter_blocked()` function, not a configuration constant intended for environment-based override.

---

### Email Recipients

#### `DEFAULT_RECIPIENTS`

**Type:** `list[str]`
**Value:** `["ilya@ddvb.tech", "maria@ddvb.tech"]`

The hardcoded list of email recipients for both the digest and the empty notification. There is no runtime mechanism to change this — to update recipients, edit `config.py` and redeploy.

---

## Model URI Constants

`YANDEX_GPT_LITE` and `YANDEX_GPT_PRO` are constructed from `YC_FOLDER_ID` at import time:

```python
YANDEX_GPT_LITE = f"gpt://{YC_FOLDER_ID}/yandexgpt-lite/latest"
YANDEX_GPT_PRO  = f"gpt://{YC_FOLDER_ID}/yandexgpt/latest"
```

`YANDEX_GPT_LITE` is used in production by `classify_relevance()` and `summarize_mention()`. `YANDEX_GPT_PRO` is defined but not referenced by the current pipeline.

`YC_BASE_URL = "https://llm.api.cloud.yandex.net/v1"` is defined in `config.py` but the actual base URL is hardcoded again in `yandex_ai.get_llm_client()`. If the LLM endpoint changes, update both locations.

---

## Local Development .env File

For local development, create a `.env` file at the project root. This file is loaded by `python-dotenv` when `config.py` is imported. It must not be committed to git.

```bash
# Yandex Cloud (required)
YC_API_KEY=AQVN...your_key_here
YC_FOLDER_ID=b1g...your_folder_id

# Supabase (optional — leave empty to skip DB operations)
SUPABASE_URL=https://abcdefghij.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOi...your_service_role_key

# SMTP (optional — leave SMTP_HOST empty to skip email)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=agent@ddvb.tech
SMTP_PASSWORD=your_app_password
SMTP_FROM=agent@ddvb.tech

# AI Studio (optional — reserved, currently unused)
# AI_STUDIO_API_KEY=
```

In dry-run mode (`python main.py --dry-run`), Supabase reads are skipped and no emails are sent, so you can test without setting those variables.

---

## Configuration Precedence

For variables with defaults, the resolution order is:

1. **Yandex Cloud Functions environment** (production) — injected at function version creation via `--environment` flags or the console UI
2. **`.env` file** (local development) — read by `python-dotenv` on `load_dotenv()` call in `config.py`
3. **System environment** — any variables already set in the OS environment
4. **Hardcoded defaults** in `config.py` — applies to optional variables only

For runtime search parameters (queries, target domains, exclude domains), there is an additional override layer:

5. **Supabase `mention_settings` table** — read at pipeline start by `load_settings()`. Values from this table override the `config.py` defaults for `search_queries`, `target_domains`, and `exclude_domains`.
