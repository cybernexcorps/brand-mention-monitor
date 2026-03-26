"""Configuration — loads environment variables and defines constants."""

import os
from dotenv import load_dotenv

load_dotenv()

# Yandex Cloud
YC_API_KEY = os.environ["YC_API_KEY"]
YC_FOLDER_ID = os.environ["YC_FOLDER_ID"]
YC_BASE_URL = "https://llm.api.cloud.yandex.net/v1"

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

# Rate limiting
YANDEX_RATE_LIMIT_SECONDS = 1.0  # 1 req/sec for generative mode
MAX_DOMAINS_PER_BATCH = 5  # allowed_domains limit per call
