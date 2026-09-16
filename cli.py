# cli.py — Интерактивный CLI с поддержкой удаленных TARGET_URLS (argparse + input)
# python cli.py

import asyncio
import argparse
import logging
import sys
import re
from urllib.parse import urlparse
import config
from main import run_parser


class EmojiFormatter(logging.Formatter):
    def format(self, record):
        return f"{self.formatTime(record, '%H:%M:%S')} {record.getMessage()}"

logger = logging.getLogger("ChittaParser")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(EmojiFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

def prompt(text: str, default: str) -> str:
    user_val = input(f"❓ {text} [{default}]: ").strip()
    return user_val if user_val else str(default)

def extract_domain(url: str) -> str:
    """Извлекает домен из ссылки без 'www.' для безопасного значения префикса."""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc or parsed.path.split('/')[0]
        domain = re.sub(r'^www\.', '', netloc).split(':')[0].strip()
        if domain:
            return domain
    except Exception:
        pass
    return getattr(config, "DEFAULT_OUTPUT_PREFIX", "chitta_catalog")

def main():
    parser = argparse.ArgumentParser(
        description="Chitta™ Universal E-commerce Catalog & Product Parser"
    )
    parser.add_argument(
        "--url", 
        type=str, 
        default=config.DEFAULT_URL, 
        help="Ссылка на целевой каталог или товар"
    )
    parser.add_argument(
        "--max", 
        type=int, 
        default=getattr(config, "DEFAULT_MAX_PARENTS", 5), 
        help="Максимальное количество родительских товаров"
    )
    parser.add_argument(
        "--ollama", 
        action="store_true", 
        default=getattr(config, "DEFAULT_ENABLE_OLLAMA", False),
        help="Включить фактическое извлечение данных через Ollama AI"
    )
    parser.add_argument(
        "--prefix", 
        type=str, 
        default=None, 
        help="Префикс для сгенерированных CSV-файлов"
    )
    parser.add_argument(
        "--remote_target_urls", 
        nargs="?",
        const="true",
        default="",
        help="Запросить целевые URL удаленно с сервера (опционально: history_hash)"
    )

    parser.add_argument(
        "--engine", 
        type=str, 
        default=getattr(config, "DEFAULT_ENGINE", "general"), 
        help="Движок целевого сайта (general, opencart, shopify, prom-ua и др.)"
    ) 

    parser.add_argument(
        "--version", 
        type=str, 
        choices=["v1", "v2"], 
        default=getattr(config, "DEFAULT_VERSION", "v1"), 
        help="Версия подхода парсинга: v1 (универсальный/семантический) или v2 (Ollama CSS-селекторы)"
    )      

    if len(sys.argv) == 1:
        print("\n" + "=" * 60)
        print("🚀 CHITTA CATALOG PARSER — ИНТЕРАКТИВНЫЙ ЗАПУСК")
        print("=" * 60 + "\n")

        url = prompt("Введите ссылку на каталог/товар", config.DEFAULT_URL)
        
        max_raw = prompt("Максимальное количество товаров", getattr(config, "DEFAULT_MAX_PARENTS", 5))
        try:
            max_parents = int(max_raw)
        except ValueError:
            max_parents = getattr(config, "DEFAULT_MAX_PARENTS", 5)

        default_ollama_str = "y" if getattr(config, "DEFAULT_ENABLE_OLLAMA", False) else "n"
        ollama_raw = prompt("Включить AI-обогащение через Ollama? (y/n)", default_ollama_str).lower()
        enable_ollama = ollama_raw in ["y", "yes", "д", "да"]

        default_remote_str = "y" if getattr(config, "DEFAULT_REMOTE_TARGET_URLS", False) else "n"
        remote_raw = prompt("Запросить TARGET_URLS с chitta.site API? (y/n или введите history_hash)", default_remote_str).strip()
        
        remote_lower = remote_raw.lower()
        if remote_lower in ["n", "no", "н", "нет", "0", "false", ""]:
            use_remote_urls = ""
        elif remote_lower in ["y", "yes", "д", "да", "1", "true"]:
            use_remote_urls = "true"
        else:
            use_remote_urls = remote_raw  # Передан непосредственно history_hash        

        default_prefix = extract_domain(url)
        output_prefix = prompt("Префикс импортируемых файлов", default_prefix)

        default_engine_str = getattr(config, "DEFAULT_ENGINE", "general")
        engine_raw = prompt("Движок сайта (general/opencart/shopify/prom-ua)", default_engine_str).lower().strip()
        chosen_engine = engine_raw if engine_raw else default_engine_str    

        default_version_str = getattr(config, "DEFAULT_VERSION", "v1")
        version_raw = prompt("Версия подхода (v1 — семантический, v2 — AI-селекторы)", default_version_str).lower().strip()
        chosen_version = version_raw if version_raw in ["v1", "v2"] else default_version_str

        default_provider = getattr(config, "DEFAULT_LLM_PROVIDER", "gemini")
        provider_raw = prompt("LLM Провайдер (gemini/ollama)", default_provider).lower().strip()
        chosen_provider = provider_raw if provider_raw in ["gemini", "ollama"] else default_provider

        args = argparse.Namespace(
            url=url,
            max=max_parents,
            ollama=enable_ollama,
            prefix=output_prefix,
            remote_target_urls=use_remote_urls,
            engine=chosen_engine,
            version=chosen_version,
            provider=chosen_provider
        )
    else:
        args = parser.parse_args()
        if not args.prefix:
            args.prefix = extract_domain(args.url)

    print("\n" + "=" * 65)
    print("⚙️  ПАРАМЕТРЫ СЕССИИ:")
    print(f"  • Целевой URL: {args.url}")
    print(f"  • Лимит товаров: {args.max}")
    print(f"  • Ollama AI: {'Включено' if args.ollama else 'Выключено'}")
    remote_info = "Нет"
    if args.remote_target_urls:
        remote_info = f"Да (history_hash: {args.remote_target_urls})" if args.remote_target_urls != "true" else "Да"
    print(f"  • Remote TARGET_URLS: {remote_info}")    
    print(f"  • Префикс файлов: {args.prefix}")
    print(f"  • Движок сайта: {args.engine}")  
    print(f"  • Версия парсера: {args.version.upper()}")  
    print("=" * 65 + "\n")

    try:
        asyncio.run(run_parser(
            target_url=args.url,
            max_parents=args.max,
            enable_ollama=args.ollama,
            output_prefix=args.prefix,
            remote_target_urls=args.remote_target_urls,
            engine_name=args.engine,
            version=args.version,
            llm_provider=getattr(args, "provider", "gemini")
        ))
    except KeyboardInterrupt:
        logger.warning("\n🛑 [STOP] Процесс прерван пользователем.")
        sys.exit(0)

if __name__ == "__main__":
    main()