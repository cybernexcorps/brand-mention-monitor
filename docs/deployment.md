# Deployment Guide — Brand Mention Monitor

This guide covers deploying the Brand Mention Monitor to Yandex Cloud Functions. The deployment model uses a pre-packaged zip archive containing all source files and vendored Python dependencies.

---

## Prerequisites

Before deploying, ensure you have:

1. **Yandex Cloud CLI (`yc`)** installed and authenticated
   ```bash
   yc --version    # verify installation
   yc config list  # verify authentication
   ```

2. **AWS CLI** (for Object Storage upload — required because `function.zip` exceeds the 3.5 MB console upload limit)
   ```bash
   aws --version
   ```

3. **A Yandex Cloud folder** with:
   - Cloud Functions service enabled
   - Foundation Models (YandexGPT) API enabled
   - Search API v2 enabled
   - AI Studio generative search API enabled
   - Object Storage bucket for deployment artifacts

4. **A Yandex Cloud IAM API key** with permissions for:
   - `search.yandex.net` (Search API v2)
   - `llm.yandex.net` (Foundation Models / YandexGPT)
   - AI Studio generative search

5. **A Supabase project** with the required tables created (see Database Setup below)

6. **SMTP credentials** for the outgoing email account

---

## Database Setup (Supabase)

The service expects two tables before first run. Create them in the Supabase SQL editor.

### `brand_mentions` table

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

-- Required: unique constraint on url (used for upsert conflict resolution)
CREATE UNIQUE INDEX brand_mentions_url_idx ON brand_mentions (url);
```

The `UNIQUE` constraint on `url` is mandatory — without it, `save_mentions()` fails on the `upsert(..., on_conflict="url")` call. The `discovery_source` column stores `"ai_studio_generative"` or `"yandex_search_api"`. The `summary` column stores the AI-synthesized answer text (for generative results) or the snippet fallback (for Search API results).

### `mention_settings` table

```sql
CREATE TABLE mention_settings (
    id    bigserial PRIMARY KEY,
    key   text UNIQUE NOT NULL,
    value jsonb
);

-- Optional: seed with default overrides
-- Leave empty to use config.py defaults
INSERT INTO mention_settings (key, value) VALUES
    ('target_domains',  '["sostav.ru", "retail.ru", "unipack.ru", "new-retail.ru"]'),
    ('search_queries',  '['"DDVB"', '"ДДВБ"']'),
    ('exclude_domains', '["sostav.ru"]');
```

The `mention_settings` table can remain empty — `load_settings()` falls back to `config.py` defaults.

---

## Understanding the `deploy/` Directory

The `deploy/` directory is the production artifact. The Yandex Cloud Functions Python runtime resolves imports relative to the function root, so all source modules and their dependencies must be co-located in a single directory tree.

```
deploy/
├── main.py              # copy of root main.py
├── yandex_agent.py      # copy of root yandex_agent.py (required — primary search)
├── yandex_ai.py         # copy of root yandex_ai.py
├── config.py            # copy of root config.py (must be present before zipping)
├── supabase_client.py   # copy of root supabase_client.py
├── email_digest.py      # copy of root email_digest.py
└── deps/                # pre-installed Python packages (pip install --target)
    ├── httpx/
    ├── dotenv/
    ├── openai/
    ├── supabase/
    ├── yandex_ai_studio_sdk/   # required for generative search
    ├── httpcore/
    ├── anyio/
    ├── certifi/
    ├── ...
```

**Important:** `yandex_agent.py` must be copied to `deploy/` — it is required for the primary generative search stage. `config.py` must also be present in `deploy/` before zipping.

---

## Size Constraint: Object Storage Required

The `function.zip` archive typically **exceeds the Yandex Cloud Functions console upload limit of 3.5 MB** because `deploy/deps/` contains compiled binary extensions (cryptography, cffi from the Supabase SDK). The `yandex-ai-studio-sdk` package adds additional dependencies that push the archive further over this limit.

The deployment workflow uses **Yandex Object Storage (S3) as an intermediary** — the zip is uploaded to a bucket, and the `yc` CLI deploys from the bucket URL. There is no size limit via this path.

---

## Build Process

### Step 1 — Copy source files to deploy/

```bash
cd /path/to/brand-mention-monitor

cp main.py deploy/main.py
cp config.py deploy/config.py
cp yandex_agent.py deploy/yandex_agent.py
cp yandex_ai.py deploy/yandex_ai.py
cp supabase_client.py deploy/supabase_client.py
cp email_digest.py deploy/email_digest.py
```

All six source files must be copied. Missing `yandex_agent.py` will cause an `ImportError` when the function starts — generative search is a required import in `main.py`.

### Step 2 — Install dependencies into deploy/deps/

If `deploy/deps/` is out of date or missing packages, reinstall using **Linux Docker** (required for binary-compatible extensions):

```bash
docker run --rm -v "$(pwd)/deploy/deps:/deps" python:3.12-slim \
    pip install openai httpx supabase python-dotenv yandex-ai-studio-sdk --target /deps
```

**Why Docker is required:** The Yandex Cloud Functions runtime is Linux x86_64. If you install deps on Windows, binary extensions like `_cffi_backend` are compiled as `.pyd` (Windows PE) files that will not load on Linux. The `deploy/deps/` directory currently contains Windows-compiled extensions (`_cffi_backend.cp313-win_amd64.pyd`, `_cffi_backend.cp314-win_amd64.pyd`) that must be replaced before Linux deployment.

**Key dependency: `yandex-ai-studio-sdk`** — this package is required for the primary generative search stage (`yandex_agent.py`). It is not in the original `requirements.txt` if working from an older version of the repo. Install it explicitly as shown above.

Alternatively, if running on a Linux machine:
```bash
pip install \
    openai>=1.0.0 \
    httpx>=0.27.0 \
    supabase>=2.0.0 \
    python-dotenv>=1.0.0 \
    yandex-ai-studio-sdk \
    --target deploy/deps/
```

Use Python 3.12 (`python3.12 -m pip`) to match the Yandex Cloud Functions runtime.

### Step 3 — Build the zip archive

```bash
cd deploy
zip -r ../function.zip . -x "__pycache__/*" -x "*.pyc" -x ".DS_Store"
cd ..
```

**Verify the zip structure:**
```bash
unzip -l function.zip | head -20
```

Expected: `main.py`, `config.py`, `yandex_agent.py`, `yandex_ai.py`, etc. at the root level (no leading directory prefix), and `deps/httpx/`, `deps/yandex_ai_studio_sdk/`, etc. under `deps/`.

### Step 4 — Upload to Object Storage

```bash
aws s3 cp function.zip s3://your-bucket-name/function.zip \
    --endpoint-url https://storage.yandexcloud.net
```

Replace `your-bucket-name` with your actual Yandex Object Storage bucket name. The bucket must be in the same folder as the Cloud Function or accessible from it.

Alternatively, upload via the Yandex Cloud console: Object Storage → your bucket → Upload.

---

## Deploying to Yandex Cloud Functions

### Create the function (first deploy only)

```bash
yc serverless function create \
    --name brand-mention-monitor \
    --description "Automated DDVB brand mention tracking"
```

### Create a function version (deploy or update)

Deploy from Object Storage (required because zip exceeds 3.5 MB direct upload limit):

```bash
yc serverless function version create \
    --function-name brand-mention-monitor \
    --runtime python312 \
    --entrypoint main.handler \
    --memory 256m \
    --execution-timeout 300s \
    --package-bucket-name your-bucket-name \
    --package-object-name function.zip \
    --environment YC_API_KEY=AQVN...your_key \
    --environment YC_FOLDER_ID=b1g...your_folder \
    --environment SUPABASE_URL=https://abcdefgh.supabase.co \
    --environment SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOi...key \
    --environment SMTP_HOST=smtp.gmail.com \
    --environment SMTP_PORT=587 \
    --environment SMTP_USER=agent@ddvb.tech \
    --environment SMTP_PASSWORD=your_smtp_password \
    --environment SMTP_FROM=agent@ddvb.tech
```

**Parameter notes:**
- `--package-bucket-name` and `--package-object-name` — use these instead of `--source-path` to deploy from Object Storage
- `--runtime python312` — matches the Python version used to install deps
- `--entrypoint main.handler` — the `handler` function in `main.py`
- `--memory 256m` — sufficient for the pipeline; not memory-intensive
- `--execution-timeout 300s` — 5-minute timeout. A full run takes 60–180 seconds. Set to at least 300s to allow for slow API responses including the 120-second generative search timeout.

**AI_STUDIO_API_KEY is not required** — the generative search SDK uses `YC_API_KEY` directly. You do not need to add `AI_STUDIO_API_KEY` to the function environment unless you switch to separate AI Studio authentication.

---

## Setting Up the Cron Schedule

Create a timer trigger to run the function on a schedule. Recommended: weekly on Monday at 08:00 Moscow time:

```bash
yc serverless trigger create timer \
    --name brand-mention-monitor-weekly \
    --cron-expression "0 5 * * 1" \
    --invoke-function-name brand-mention-monitor \
    --invoke-function-service-account-id <service-account-id>
```

**Cron format:** `minute hour day-of-month month day-of-week` using **UTC**. Moscow is UTC+3, so:
- "Monday 08:00 MSK" → `0 5 * * 1`

The `--invoke-function-service-account-id` must reference a service account with the `serverless.functions.invoker` role on the function.

---

## Verifying the Deployment

### Check that the function version exists

```bash
yc serverless function version list --function-name brand-mention-monitor
```

### Invoke the function manually

```bash
yc serverless function invoke brand-mention-monitor
```

A successful run returns:
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

Note the new summary fields: `agent_found` (generative search results), `api_found` (Search API results), and `total_raw` (combined). Earlier versions returned `total_searched` instead of these three fields.

### View execution logs

```bash
yc logging read \
    --resource-types serverless.function \
    --resource-ids <function-id> \
    --since 1h \
    --follow
```

---

## Updating an Existing Deployment

When source files change:

```bash
# 1. Edit source files at project root
# 2. Copy updated files to deploy/
cp main.py deploy/main.py
cp yandex_agent.py deploy/yandex_agent.py
cp yandex_ai.py deploy/yandex_ai.py
cp config.py deploy/config.py
cp supabase_client.py deploy/supabase_client.py
cp email_digest.py deploy/email_digest.py

# 3. Rebuild zip
cd deploy && zip -r ../function.zip . -x "__pycache__/*" -x "*.pyc" && cd ..

# 4. Upload to Object Storage
aws s3 cp function.zip s3://your-bucket-name/function.zip \
    --endpoint-url https://storage.yandexcloud.net

# 5. Create new function version
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
    # ... all other env vars
```

Each `version create` call creates a new immutable version. The function automatically routes invocations to the latest version.

---

## Environment Variable Management

Environment variables are stored in the function version and are immutable after creation. To update a variable, create a new version. Maintain a secure record of all values (e.g., in a password manager or Yandex Lockbox) because all variables must be re-specified on each version creation.

---

## Dependency Management

The `deploy/deps/` directory contains all runtime dependencies vendored at install time. The deployed function is fully self-contained — no internet access is required during function startup.

**Current required packages:**

```
openai>=1.0.0
httpx>=0.27.0
supabase>=2.0.0
python-dotenv>=1.0.0
yandex-ai-studio-sdk     # new — required for generative search
pytest>=8.0.0            # dev only, do not install into deploy/deps/
```

`pytest` is listed in `requirements.txt` for local development but must not be installed into `deploy/deps/` — it would increase zip size unnecessarily.

Updating a dependency requires re-running the Docker install command (Step 2) and rebuilding the zip.

---

## Security Considerations

- **Never commit** `.env`, `.env.production`, or any file containing API keys or passwords
- **Service role key scope:** The Supabase service role key bypasses RLS — use it only in trusted server environments like Cloud Functions
- **API key rotation:** When rotating `YC_API_KEY`, create a new function version with the new key before revoking the old one to avoid a gap in availability
- **Least-privilege IAM:** The Yandex Cloud service account used for invoking the function should have only `serverless.functions.invoker` — it does not need access to the IAM key used for Search API, LLM, and AI Studio calls
- **Object Storage bucket ACL:** The deployment bucket containing `function.zip` should be private (not public). The `yc` CLI accesses it using the authenticated user's permissions.
