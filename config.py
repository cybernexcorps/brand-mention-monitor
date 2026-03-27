"""Configuration — loads environment variables and defines constants."""

import os
from dotenv import load_dotenv

load_dotenv()

# Yandex Cloud
YC_API_KEY = os.environ["YC_API_KEY"]
YC_FOLDER_ID = os.environ["YC_FOLDER_ID"]
YC_BASE_URL = "https://llm.api.cloud.yandex.net/v1"

# Yandex AI Studio (kept for reference — generative search uses YC_API_KEY directly)
AI_STUDIO_API_KEY = os.getenv("AI_STUDIO_API_KEY", "")

# Models
YANDEX_GPT_LITE = f"gpt://{YC_FOLDER_ID}/yandexgpt-lite/latest"
YANDEX_GPT_PRO = f"gpt://{YC_FOLDER_ID}/yandexgpt/latest"

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# Search defaults
DEFAULT_TARGET_DOMAINS = ["sostav.ru", "retail.ru", "unipack.ru", "new-retail.ru"]
DEFAULT_SEARCH_QUERIES = ['"DDVB"', '"ДДВБ"']
DEFAULT_EXCLUDE_DOMAINS = ["sostav.ru"]

# Search API v2 — improved settings
SEARCH_RESULTS_PER_PAGE = 50
SEARCH_DATE_RESTRICT_DAYS = 7

# Rate limiting
YANDEX_RATE_LIMIT_SECONDS = 1.0  # 1 req/sec for generative mode
MAX_DOMAINS_PER_BATCH = 5  # allowed_domains limit per call

# Blocked domains — never editorial mentions (hard block)
BLOCKED_DOMAINS = {
    # DDVB own resources
    "ddvb.ru", "www.ddvb.ru", "ddvb.tech", "www.ddvb.tech",
    # Search engines
    "yandex.ru", "google.com", "google.ru",
    # WHOIS / SEO / domain tools
    "cy-pr.com", "2whois.ru", "whois.ru", "pr-cy.ru",
    "tapki.com", "yapl.ru", "reddial.ru",
    "similarweb.com", "semrush.com", "alexa.com",
    "webarchive.org", "web.archive.org",
    # Generic directories without editorial content
    "catalog.tools",
    # Classifieds / car parts — "DDVB" is a VAG engine code, not the agency
    "avito.ru", "auto.ru", "drom.ru", "baza.drom.ru", "farpost.ru",
    "arpshop.ru", "avdauto.com", "autocompas.ru", "vodila.by", "zap.by",
    "partsouq.com", "hepsiburada.com", "newpartsricambi.com",
    # Spam / unrelated content / foreign sites with "DDVB" as random string
    "javip.net", "eporner.dgav14.com", "obdrgg.cn", "pbmgo.cq17u.com",
    "1doz8.atlantafigures.org", "post.rlsbb.cc", "qodov.clixi.ru",
    "log1.2chb.net", "worldwebwar.ru", "ongaku.one",
    "lichess.org", "wolf-power.ch", "blockchain.com", "kolhosniki.ru",
    "boovell.ru", "p3-tt.byteimg.com", "bog2.obraz-tmr.ru",
    "gist.github.com", "github.com",
    "shedevrum.ai", "learnsanskrit.ru", "fias.alta.ru",
    "reiting.ex-u.ru", "aviso.bz", "rutube.ru",
    "report-abuse.com", "zavpro.travyanov.ru",
    # Visual/social platforms — show other agencies, not DDVB-specific editorial
    "ru.pinterest.com", "pinterest.com",
}

# Social media — NOT blanket-blocked; agent classifies contextually.
# Third-party editorial mentions on these platforms are legitimate.
SOCIAL_MEDIA_DOMAINS = {
    "t.me", "vk.com", "ok.ru", "instagram.com", "facebook.com", "twitter.com",
}

# Email (SMTP)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "agent@ddvb.tech")
DEFAULT_RECIPIENTS = ["ilya@ddvb.tech", "maria@ddvb.tech"]
