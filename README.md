# Brand Mention Monitor

Automated media mention tracking for DDVB branding agency. Discovers editorial mentions of DDVB across Russian-language media, filters noise with a 6-layer pipeline, and delivers a weekly email digest.

## How it works

```
Yandex AI Studio (generative search)  ──┐
                                         ├── Merge + Dedup
Yandex Search API v2 (fallback)       ──┘
         │
   6-Layer Filter Pipeline
   ├── Blocklist + TLD allowlist
   ├── Brand gate (DDVB must be in text)
   ├── Year filter (current year only)
   ├── Page verification (fetch URL, check DDVB on page)
   └── AI classifier (YandexGPT Lite)
         │
   Save to Supabase → Email digest
```

## Quick start

```bash
# Install dependencies
pip install -r requirements.txt

# Set up credentials
cp .env.example .env
# Edit .env with your Yandex Cloud API key, Supabase credentials

# Dry run (no DB writes, no email)
python main.py --dry-run --verbose

# Full run
python main.py
```

## Deployment

Runs as a **Yandex Cloud Function** (Python 3.12), triggered weekly by a timer. See [docs/deployment.md](docs/deployment.md) for full instructions.

## Tech stack

- **Search:** Yandex AI Studio SDK (generative search) + Yandex Search API v2
- **AI:** YandexGPT Lite (classifier)
- **Database:** Supabase (PostgreSQL)
- **Email:** SMTP (STARTTLS)
- **Runtime:** Yandex Cloud Functions

## Documentation

- [CLAUDE.md](CLAUDE.md) — Technical reference (authoritative)
- [docs/architecture.md](docs/architecture.md) — System architecture and diagrams
- [docs/api-reference.md](docs/api-reference.md) — Function signatures
- [docs/configuration.md](docs/configuration.md) — Environment variables and constants
- [docs/deployment.md](docs/deployment.md) — Deployment guide
- [docs/operations.md](docs/operations.md) — Monitoring and troubleshooting
