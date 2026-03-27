# Operations Runbook — Brand Mention Monitor

This runbook covers day-to-day operation of the Brand Mention Monitor: how to verify it ran, how to debug failures, how to inspect stored data, and how to trigger manual runs.

---

## Normal Operation

The service runs once per scheduled invocation (recommended: weekly). A successful run produces:

1. **Execution log** in Yandex Cloud Functions showing INFO-level pipeline steps
2. **Rows upserted** into Supabase `brand_mentions` table (zero or more)
3. **Email delivered** to `ilya@ddvb.tech` and `maria@ddvb.tech` — either a digest with mentions or a "no new mentions" notification

If the recipients receive either type of email, the pipeline completed successfully.

---

## Checking Execution Status

### View the last execution result

```bash
yc serverless function invoke brand-mention-monitor
```

The response body contains the pipeline summary:
```json
{
    "statusCode": 200,
    "body": {
        "agent_found": 4,
        "api_found": 14,
        "total_raw": 18,
        "after_dedup": 12,
        "after_filter": 9,
        "relevant": 3,
        "saved": 3
    }
}
```

Interpreting summary values:

| Condition | Likely meaning |
|---|---|
| `agent_found = 0` and `api_found = 0` | Both search sources failed or returned no results |
| `agent_found = 0` only | Generative search failed or timed out — check for `ERROR Generative search failed` in logs |
| `api_found = 0` only | Search API v2 failed or returned no results — check for `WARNING Search API returned 403` etc. |
| `total_raw > 0` but `after_dedup = 0` | All results were already in Supabase (dedup removed everything) — normal if running twice in quick succession |
| `after_filter = 0` | All results blocked by domain blocklist, TLD filter, or exclude list |
| `after_filter > 0` but `relevant = 0` | All results failed the brand gate, year filter, page verification, or AI classifier |
| `relevant > 0` but `saved = 0` | Supabase upsert failed — check for `ERROR Failed to save mentions` in logs |

### View execution logs

```bash
# View logs from the last hour
yc logging read \
    --resource-types serverless.function \
    --resource-ids <function-id> \
    --since 1h

# Stream live logs during a manual invocation
yc logging read \
    --resource-types serverless.function \
    --resource-ids <function-id> \
    --follow
```

Or in the Yandex Cloud console: Cloud Functions → brand-mention-monitor → Logs tab.

Log format: `TIMESTAMP [LEVEL] brand-mention-monitor: message`

A normal run produces log output similar to:
```
INFO  Loading settings...
INFO  Loaded 3 settings from Supabase
INFO  Existing URLs in DB: 47
INFO  Stage 3: Generative search (date_from=2026-03-20)...
INFO  Generative search: query="DDVB" (attempt 1/3)
INFO  Generative search returned 6 sources for "DDVB"
INFO    4 used / 6 total sources for "DDVB"
INFO  Generative search: query="ДДВБ" (attempt 1/3)
INFO  Generative search returned 3 sources for "ДДВБ"
INFO    2 used / 3 total sources for "ДДВБ"
INFO  Generative search total: 6 mentions
INFO  Stage 4: Search API v2 fallback (queries: ['"DDVB"'])...
INFO    Query: "DDVB" on ['sostav.ru', 'retail.ru', 'unipack.ru', 'new-retail.ru']
INFO  Search submitted: operation=op-abc123 query='"DDVB"'
INFO  Search completed: 8 results for '"DDVB"'
INFO    Query: "DDVB" (broad)
INFO  Search submitted: operation=op-def456 query='"DDVB"'
INFO  Search completed: 10 results for '"DDVB"'
INFO  Search API found 18 raw results
INFO  Total raw results (agent + API): 24
INFO  After dedup: 16
INFO  After blocked domain filter: 11
INFO  After brand gate: 9 (rejected 2 without DDVB in text)
INFO  After year filter: 9 (rejected 0 old)
INFO  After page verification: 8 (rejected 1)
INFO  Classifying 8 results...
INFO    [ai_st] sostav.ru — DDVB разработала айдентику для ...
INFO    RELEVANT: retail.ru — DDVB стал партнёром ...
DEBUG   irrelevant: pr-agency.ru — Рейтинг PR-агентств
INFO  Relevant mentions: 3 / 8
INFO  Saved 3 mentions to Supabase
INFO  Email sent to ilya@ddvb.tech, maria@ddvb.tech
INFO  ==================================================
INFO  PIPELINE SUMMARY
INFO    Agent found:       6
INFO    Search API found:  18
INFO    Total raw:         24
INFO    After dedup:       16
INFO    After filter:      11
INFO    Relevant:          3
INFO    Saved:             3
INFO  ==================================================
```

Note: `[ai_st]` prefix in classification logs indicates an `ai_studio_generative` source (pre-classified, skips YandexGPT). `[yande]` indicates a `yandex_search_api` source that went through YandexGPT.

---

## Monitoring the 6-Layer Filter Effectiveness

The pipeline logs the count at each filter layer with a "rejected N" annotation. Monitor these to understand where results are being lost:

```
After dedup:           XX (normal — removes cross-query and DB duplicates)
After blocked domain:  XX (high rejection = blocklist catching noise correctly)
After brand gate:      XX (rejected N without DDVB in text)
After year filter:     XX (rejected N old)
After page verify:     XX (rejected N — Yandex context injection caught)
After classify:        XX relevant / XX total
```

**Healthy ratios for a typical run:**
- Brand gate rejection: 10–40% (Search API finds many tangential results)
- Page verification rejection: 5–20% (Yandex context injection is common)
- AI classifier: 30–60% of page-verified results classified relevant

If brand gate is rejecting 80%+, the search queries may be too broad or the target domains are returning unrelated content. If AI classifier relevance rate drops below 10%, the quality filters before it may need strengthening.

---

## Running Locally

### Dry run (recommended for testing)

The dry-run mode runs full search and filtering but skips all writes to Supabase and skips email sending. It does not load existing URLs from Supabase — all search results are treated as new.

```bash
cd /path/to/brand-mention-monitor
source .venv/Scripts/activate  # Windows Git Bash
# or: source .venv/bin/activate  # Linux/macOS

# Dry run with INFO logging
python main.py --dry-run

# Dry run with DEBUG logging (shows individual filter decisions)
python main.py --dry-run --verbose
```

The `--verbose` flag enables DEBUG logging, which shows:
- Individual brand gate rejections (domain + truncated title)
- Individual page verification failures
- Individual AI classifier decisions (relevant/irrelevant per result)
- Unused generative search sources being skipped

### Running the PoC script

For interactive validation without any database or email side effects:

```bash
python poc_search.py
```

This script uses `yandex_ai.search_web()` directly with simplified deduplication logic (no `BLOCKED_DOMAINS` set, no TLD filter, no Supabase cross-check) and prints results to stdout. Useful for verifying that the Yandex Search API key works and that search queries return reasonable results.

### Triggering a full run locally (with writes)

```bash
python main.py
```

Runs the complete pipeline including Supabase upserts and email sending. Uses credentials from `.env`.

---

## Checking Supabase Data

### Via the Supabase dashboard

1. Open the Supabase project dashboard
2. Navigate to Table Editor → `brand_mentions`
3. Sort by `created_at DESC` to see the most recent mentions

### Via Supabase SQL editor

**Count total mentions:**
```sql
SELECT COUNT(*) FROM brand_mentions;
```

**View recent mentions with discovery source:**
```sql
SELECT url, title, source_domain, discovery_source, discovery_query, created_at
FROM brand_mentions
ORDER BY created_at DESC
LIMIT 20;
```

**Compare generative vs. Search API results:**
```sql
SELECT discovery_source, COUNT(*) as count
FROM brand_mentions
GROUP BY discovery_source
ORDER BY count DESC;
```

**Check mentions from a specific domain:**
```sql
SELECT url, title, snippet, summary, created_at
FROM brand_mentions
WHERE source_domain = 'sostav.ru'
ORDER BY created_at DESC;
```

**View all mentions from the last 7 days:**
```sql
SELECT url, title, source_domain, discovery_source, created_at
FROM brand_mentions
WHERE created_at > NOW() - INTERVAL '7 days'
ORDER BY created_at DESC;
```

**Check current runtime settings:**
```sql
SELECT key, value FROM mention_settings;
```

---

## Common Failure Modes

### No email received — no error in logs

**Symptom:** The function executes (visible in Cloud Functions logs) but no email arrives.

**Cause:** `SMTP_HOST` environment variable is not set or empty.

**Diagnosis:**
```bash
yc serverless function version get <version-id> --format json | grep -i smtp
```
Look in logs for: `WARNING SMTP_HOST is not configured — skipping email send`

**Fix:** Create a new function version with `SMTP_HOST` set.

---

### `KeyError: 'YC_API_KEY'` on startup

**Symptom:** Function fails immediately with a `KeyError` traceback.

**Cause:** `YC_API_KEY` or `YC_FOLDER_ID` not set in the function version environment.

**Fix:** Create a new function version with the missing environment variable set.

---

### Generative search timeout or failure

**Symptom:** Logs show `ERROR Generative search failed for "DDVB" (attempt N): ...` or the function times out before completion.

**Possible causes:**

1. **Yandex AI Studio API unavailable or rate limited** — generative search has a 120-second per-query timeout. If the API is slow, a two-query run could take 4+ minutes.

2. **`YC_API_KEY` lacks AI Studio permissions** — look for `401` or `403` in the error message.

3. **Function execution timeout too short** — increase `--execution-timeout` to 600s to accommodate slow generative search responses.

**Diagnosis:** Check logs for `ERROR Generative search failed` entries. The error includes the exception message which usually indicates the root cause (auth error, network timeout, etc.).

**Behavior on failure:** Each query retries up to 2 times with 3-second backoff. After retrying, the generative search stage returns an empty list for that query. The pipeline continues with Search API v2 as fallback — a generative search failure is non-fatal.

---

### Search API returns 0 results consistently

**Symptom:** `api_found = 0` in the summary. Logs show "Search completed: 0 results" for all queries.

**Possible causes and fixes:**

1. `YC_API_KEY` lacks Search API permission (`search.yandex.net`)
   - Look for HTTP 403: `WARNING Search API returned 403 for ...`
   - Verify in the Yandex Cloud IAM console

2. Quota exhausted
   - Look for HTTP 429 in logs

3. Search terms produce no results on Yandex
   - Verify manually: search for `"DDVB"` on yandex.ru
   - If brand has low web presence, sparse results may be correct

---

### Classification returns all `irrelevant`

**Symptom:** `after_filter > 0` (or results pass page verification) but `relevant = 0`.

**Note:** Only Search API results reach the AI classifier. Generative search results are pre-classified as `relevant` and skip Layer 6. If all generative results were filtered before Layer 6, this is expected.

**Possible causes and fixes:**

1. **Wrong `YC_FOLDER_ID`** — the model URI is `gpt://{YC_FOLDER_ID}/yandexgpt-lite/latest`. If wrong, API returns 404 or permission error, `classify_relevance()` fails-open to `"relevant"` — this would produce relevant, not irrelevant. Check for `ERROR Classification failed` in logs instead.

2. **All Search API results are genuinely irrelevant** — the classifier working correctly. Use `--dry-run --verbose` locally to see individual decisions.

3. **All results filtered before Layer 6** — brand gate, year filter, or page verification removed everything. Check the `after_brand_gate` and `after_page_verify` log lines.

---

### Page verification rejects too many results

**Symptom:** Logs show many `Page verification FAILED` entries; good articles being dropped.

**Cause:** Some sites use JavaScript rendering — the page HTML returned by a plain `httpx.get()` does not contain the article text (and therefore not the brand name), even though a browser-rendered version would.

**Fix:** This is a known limitation of the static HTML fetch approach. If a site consistently fails page verification despite being a legitimate source, add it to a skip-list or move it to a target domain for Batch A (domain-restricted generative search will handle it better).

**Alternative:** For well-known editorial sources, you can note them in `DEFAULT_TARGET_DOMAINS` so they are prioritized in the generative search stage, which reads full page content before returning results and bypasses page verification.

---

### `save_mentions` saves 0 rows

**Symptom:** `relevant > 0` but `saved = 0`. Logs show "Failed to save mentions: ..."

**Possible causes:**

1. **Missing unique constraint on `url`**:
   ```sql
   SELECT indexname, indexdef
   FROM pg_indexes
   WHERE tablename = 'brand_mentions' AND indexdef LIKE '%url%';
   ```
   Fix: `CREATE UNIQUE INDEX brand_mentions_url_idx ON brand_mentions (url);`

2. **Wrong or expired Supabase credentials** — check logs for `ERROR Failed to save mentions`

3. **Supabase project paused** — free-tier projects pause after inactivity; log in to the Supabase dashboard and resume

4. **`summary` or `discovery_source` column missing from `brand_mentions`** — the current `save_mentions()` writes these fields. If deploying to a database created before these columns were added, the upsert will fail:
   ```sql
   ALTER TABLE brand_mentions ADD COLUMN IF NOT EXISTS discovery_source text;
   ALTER TABLE brand_mentions ADD COLUMN IF NOT EXISTS summary text;
   ```

---

### All results filtered out immediately

**Symptom:** `after_dedup > 0` but `after_filter = 0`. Empty notification is sent.

**Cause:** The `exclude_domains` list overlaps completely with the results from all search batches, or all result domains have foreign TLDs.

**Diagnosis:** This is the known `sostav.ru` issue — it appears in both `DEFAULT_TARGET_DOMAINS` (searched in Batch A) and `DEFAULT_EXCLUDE_DOMAINS` (filtered in Layer 2). If Batch B returns only DDVB's own domains or blocked domains, nothing survives Layer 2.

**Verification:**
```sql
SELECT key, value FROM mention_settings;
```

**Fix:** Remove sostav.ru from `exclude_domains` or add more target domains:
```sql
INSERT INTO mention_settings (key, value) VALUES ('exclude_domains', '[]')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
```

---

### Function times out

**Symptom:** Function execution exceeds the configured timeout.

**Cause analysis:** The generative search has a 120-second per-query timeout (2 queries × 120s = up to 240s). Add Search API polling (up to 32s per batch), rate limit sleeps, and page verification (N × 10s), and a worst-case run can approach or exceed 300s.

**Fix:** Increase the function timeout to 600s:
```bash
yc serverless function version create \
    ... \
    --execution-timeout 600s
```

---

### Search polling times out

**Symptom:** Logs show `WARNING Search timed out for query: ...`. Non-fatal — the function continues without results from that query.

**Cause:** The Yandex Operations API did not return `done=True` within 30 seconds (15 polls × 2 seconds). Usually transient.

**Diagnosis:** Self-resolving. If consistent, check Yandex Cloud service status.

---

### `ImportError: No module named 'yandex_ai_studio_sdk'`

**Symptom:** Function fails on startup with `ImportError` for `yandex_ai_studio_sdk`.

**Cause:** The `yandex-ai-studio-sdk` package is missing from `deploy/deps/`. This is required for `yandex_agent.py` and was added in the current version.

**Fix:** Reinstall all deps using the Docker command in the Deployment Guide Step 2, rebuild the zip, and redeploy.

---

## Updating Search Configuration Without Redeployment

The `mention_settings` table allows changing three pipeline parameters at runtime:

### Add a new target domain (Batch A)

```sql
UPDATE mention_settings
SET value = '["sostav.ru", "retail.ru", "unipack.ru", "new-retail.ru", "kommersant.ru"]'
WHERE key = 'target_domains';
```

### Add a new search query

```sql
UPDATE mention_settings
SET value = '['"DDVB"', '"ДДВБ"', '"ДДВБ Брендинг"']'
WHERE key = 'search_queries';
```

Note: all queries run in generative search. Only Latin queries (without "ДДВБ") run in Search API v2 — this filtering happens automatically in `main.py:176`.

### Remove a domain from the exclude list

```sql
UPDATE mention_settings
SET value = '[]'
WHERE key = 'exclude_domains';
```

Changes take effect on the next invocation — no redeployment needed.

---

## Manual Trigger

To run the pipeline outside its normal schedule:

```bash
# Via yc CLI
yc serverless function invoke brand-mention-monitor

# Or via the Yandex Cloud console:
# Cloud Functions → brand-mention-monitor → Testing tab → Run test
```

Manual triggers use the full production configuration and will write to Supabase and send emails. To avoid duplicate emails or double-writes, prefer using `python main.py --dry-run` locally for testing.

---

## Resetting the Deduplication State

If you need the pipeline to re-process all previously seen URLs (for example, after a data loss or schema change):

```sql
-- Clear all stored mentions (DESTRUCTIVE — cannot be undone)
TRUNCATE brand_mentions;
```

On the next run, `get_existing_urls()` returns an empty set and all search results are treated as new. All previously seen URLs will be re-classified and re-stored.

---

## Adding or Removing Recipients

Recipients are hardcoded in `config.py`:
```python
DEFAULT_RECIPIENTS = ["ilya@ddvb.tech", "maria@ddvb.tech"]
```

To change them:
1. Edit `config.py`
2. Copy to `deploy/config.py`
3. Rebuild the zip, upload to Object Storage, and create a new function version

There is no runtime mechanism to change recipients without a code change and redeployment.
