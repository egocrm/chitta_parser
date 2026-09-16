# config.py — Дефолтные настройки парсера Chitta
# python cli.py

import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# DEFAULT_URL = "https://hugo.com.ua/ua/g100223183-odezhda"
# DEFAULT_URL = "https://prom.ua/ua/p2485870821-stomatologicheskaya-modelnaya-fotopolimernaya.html"
# DEFAULT_URL = "https://goodzoo.com.ua/category/aktsionnie-tovari"
# DEFAULT_URL = "https://frutta.ua/collections/all-products"
# DEFAULT_URL = "https://frutta.ua/products/ananas"
# DEFAULT_URL = "https://skims.com/collections/nikeskims-jackets-outerwear"
# DEFAULT_URL = "https://skims.com/products/nikeskims-stretch-nylon-oversized-track-jacket-cobalt"

# openCart
# DEFAULT_URL = "https://elporte.com.ua/ua/dveri-mezhkomnatnye/" 

# WooComerce
# DEFAULT_URL = "https://frezycnc.in.ua/"
# Magento
DEFAULT_URL = os.getenv("DEFAULT_URL", "https://www.vitamix.com/vr/en_us/shop/blenders?product_series_value=334")

# TARGET_URLS Вставьте массив страниц для париснга или оставьте пусттым для автоматического поиска страниц
TARGET_URLS = []
DEFAULT_OUTPUT_PREFIX = os.getenv("DEFAULT_OUTPUT_PREFIX", "chitta_catalog")
DEFAULT_OUTPUT_DIR = os.getenv("DEFAULT_OUTPUT_DIR", "output_csv")


DEFAULT_MAX_PARENTS = int(os.getenv("DEFAULT_MAX_PARENTS", "5"))
DEFAULT_ENABLE_OLLAMA = os.getenv("DEFAULT_ENABLE_OLLAMA", "True").lower() in ("true", "1", "t", "yes")
# 3b / 7b
DEFAULT_OLLAMA_MODEL = os.getenv("DEFAULT_OLLAMA_MODEL", "qwen2.5-coder:3b")
DEFAULT_OLLAMA_HOST = os.getenv("DEFAULT_OLLAMA_HOST", "http://localhost:11434")
DEFAULT_OLLAMA_TIMEOUT = int(os.getenv("DEFAULT_OLLAMA_TIMEOUT", "180"))

# API-интеграция для дистанционного получения целевых ссылок
DEFAULT_REMOTE_TARGET_URLS = os.getenv("DEFAULT_REMOTE_TARGET_URLS", "False").lower() in ("true", "1", "t", "yes")
CHITTA_API_URL = os.getenv("CHITTA_API_URL", "https://chitta.site/")
CHITTA_API_KEY = os.getenv("CHITTA_API_KEY", "cht_333")

DEFAULT_ENGINE = os.getenv("DEFAULT_ENGINE", "general")

DEFAULT_VERSION = os.getenv("DEFAULT_VERSION", "v1")

DEFAULT_LLM_PROVIDER = os.getenv("DEFAULT_LLM_PROVIDER", "gemini")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "")  # или "gemini-2.5-flash"