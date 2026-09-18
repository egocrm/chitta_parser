# main.py — Главный оркестратор Chitta Parser с поддержкой Remote Target URLs и Camoufox

import asyncio
import logging
import sys
import httpx
import json

from camoufox.async_api import AsyncCamoufox

from core.state_machine import CatalogStateMachine
from core.universal_parser import UniversalSemanticParser
from core.engine_detector import EngineDetector
from core.ollama_enricher import OllamaEnricher
from core.attribute_normalizer import AttributeNormalizer
from exporters.csv_exporter import CSVExporter
import config

# main.py — добавление импортов для v2
from core.html_cleaner import HTMLCleaner
from core.selector_generator import SelectorGenerator
from core.fast_parser import FastSelectorParser
from typing import Optional
from core.models import ProductParent

class EmojiFormatter(logging.Formatter):
    def format(self, record):
        return f"{self.formatTime(record, '%H:%M:%S')} {record.getMessage()}"

logger = logging.getLogger("ChittaParser")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(EmojiFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

def merge_selector_maps(maps_list: list[dict]) -> dict:
    """
    Объединяет карты CSS-селекторов нескольких страниц в единую мульти-карту
    с полной очисткой от висячих запятых и исключением поля add_description.
    """
    if not maps_list:
        return {}
    if len(maps_list) == 1:
        res = maps_list[0]
        res.pop("add_description", None)
        return res
    
    merged = {}
    # Собираем уникальные ключи со ВСЕХ сгенерированных карт
    all_keys = set()
    for m in maps_list:
        if isinstance(m, dict):
            all_keys.update(m.keys())
    
    for key in all_keys:
        if key == "add_description":
            continue

        # Определяем, является ли значение словарем (например, attributes)
        is_dict_key = any(isinstance(m.get(key), dict) for m in maps_list if m.get(key))

        if is_dict_key:
            merged[key] = {}
            sub_keys = set()
            for m in maps_list:
                if isinstance(m.get(key), dict):
                    sub_keys.update(m[key].keys())
            for sk in sub_keys:
                all_selectors = []
                for m in maps_list:
                    s_val = m.get(key, {}).get(sk, "")
                    if s_val and isinstance(s_val, str):
                        for part in s_val.split(','):
                            p_clean = part.strip()
                            if p_clean and p_clean not in all_selectors:
                                all_selectors.append(p_clean)
                merged[key][sk] = ", ".join(all_selectors).strip(", ")
        else:
            all_selectors = []
            for m in maps_list:
                s_val = m.get(key, "")
                if s_val and isinstance(s_val, str):
                    for part in s_val.split(','):
                        p_clean = part.strip()
                        if p_clean and p_clean not in all_selectors:
                            all_selectors.append(p_clean)
            merged[key] = ", ".join(all_selectors).strip(", ")
            
    return merged

def merge_product_data(primary: Optional[ProductParent], secondary: Optional[ProductParent]) -> Optional[ProductParent]:
    """
    Динамически сливает данные двух объектов ProductParent.
    Значения из `primary` (v2 / FastParser) имеют АБСОЛЮТНЫЙ ПРИОРИТЕТ.
    Значения из `secondary` (v1 / CMS) используются ТОЛЬКО если в primary стоит None.
    """
    if not primary:
        return secondary
    if not secondary:
        return primary
    # === ТОЧКА 2: ДИАГНОСТИКА СЛИЯНИЯ ===
    if secondary.variants:
        for sec_v in secondary.variants:
            if sec_v.product_id == primary.product_id:
                logger.warning(f"⚠️ [DIAG 2] ВНИМАНИЕ! secondary (v1) пытается передать вариант с ID={sec_v.product_id}, который равен ID родителя {primary.product_id}!")
                logger.warning(f"⚠️ [DIAG 2] secondary variant bonus: '{sec_v.bonus}' | primary parent bonus: '{primary.bonus}'")
    # ==========================================

    # 1. Слияние основных полей объекта
    for attr, sec_val in secondary.__dict__.items():
        if attr.startswith("_"):
            continue
        
        # Пропускаем сырые и структурированные атрибуты в основном цикле - они обрабатываются отдельно
        if attr in ["attributes", "modification_attributes", "raw_attributes", "variants", "modifications"]:
            continue

        pri_val = getattr(primary, attr, None)

        # СПЕЦИАЛЬНАЯ ЛОГИКА ДЛЯ ГАЛЕРЕИ ИЗОБРАЖЕНИЙ:
        if attr == "image":
            pri_imgs = [i.strip() for i in str(pri_val or "").split(",") if i.strip()]
            sec_imgs = [i.strip() for i in str(sec_val or "").split(",") if i.strip()]
            
            # Если у v2 есть свои картинки, мы их не затираем, а мержим.
            base_list = sec_imgs if len(sec_imgs) > len(pri_imgs) else pri_imgs
            append_list = pri_imgs if len(sec_imgs) > len(pri_imgs) else sec_imgs
            
            combined = []
            for img in base_list + append_list:
                if img and img not in combined:
                    combined.append(img)
            
            setattr(primary, "image", ", ".join(combined))
            continue

        # === КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Приоритет primary (v2) ===
        # Мы перезаписываем значением из secondary ТОЛЬКО если в primary значение равно None.
        # Если primary содержит "" или 0.0, мы считаем это осознанным результатом v2 и НЕ перезаписываем.
        if pri_val is None and sec_val not in [None, "", 0.0, [], {}]:
            setattr(primary, attr, sec_val)

    # 2. Объединение сырых атрибутов (raw_attributes)
    if hasattr(secondary, "raw_attributes") and isinstance(secondary.raw_attributes, list):
        if not primary.raw_attributes:
            primary.raw_attributes = []
        for sec_item in secondary.raw_attributes:
            if sec_item not in primary.raw_attributes:
                primary.raw_attributes.append(sec_item)

    # 3. Перенос вариантов и модификаций, если у primary они отсутствовали
    if not primary.variants and secondary.variants:
        primary.variants = secondary.variants
        
    if not getattr(primary, 'modifications', None) and getattr(secondary, 'modifications', None):
        primary.modifications = secondary.modifications

    return primary

def sync_parent_ids_to_variants(parent: ProductParent) -> None:
    """
    Пробрасывает parent_product_id_N от родителя во все дочерние варианты
    и выполняет дельта-анализ статических атрибутов (вынос разницы в modification_attributes).
    """
    if not parent or not parent.variants:
        return

    alt_ids = {}
    for attr, val in parent.__dict__.items():
        if attr.startswith("product_id_") and val:
            val_str = str(val).strip()
            if not (val_str.startswith("{") or val_str.startswith("[")) and len(val_str) < 60:
                suffix = attr.replace("product_id_", "")
                alt_ids[suffix] = val_str

    parent_attrs = parent.attributes or {}

    for variant in parent.variants:
        variant.parent_product_id = parent.product_id

        for suffix, alt_id in alt_ids.items():
            setattr(variant, f"parent_product_id_{suffix}", alt_id)

        # ДЕЛЬТА-АНАЛИЗ: Сравниваем статические атрибуты варианта с родителем
        v_attrs = variant.attributes or {}
        for k, v_val in list(v_attrs.items()):
            p_val = parent_attrs.get(k)
            # Если значение варианта отличается от родителя — выносим его в модификации
            if p_val and v_val and str(p_val).strip().lower() != str(v_val).strip().lower():
                clean_mod_key = k.strip()
                variant.modification_attributes[clean_mod_key] = str(v_val).strip()

async def enrich_missing_fields_with_ollama(
    page, 
    parent_product: ProductParent, 
    selector_generator: SelectorGenerator, 
    html_cleaner: HTMLCleaner
):
    """
    Точечно добирает отсутствующие скалярные поля через Ollama прямо для ТЕКУЩЕЙ страницы.
    """
    missing_fields = []
    if not parent_product.brand_name: missing_fields.append("brand_name")
    if not parent_product.manufacturer: missing_fields.append("manufacturer")
    if not parent_product.country_of_origin: missing_fields.append("country_of_origin")
    if not parent_product.price or parent_product.fact_price >= parent_product.price: missing_fields.append("old_price")
    if not parent_product.sales_notes: missing_fields.append("sales_notes")
    if not parent_product.bonus: missing_fields.append("bonus")

    if not missing_fields:
        return

    logger.info(f"🧩 [OLLAMA FILLER] Точечный добор пропущенных полей {missing_fields} для текущего товара...")

    raw_html = await page.content()
    cleaned_html = html_cleaner.clean_html_for_selectors(raw_html, provider="ollama")
    if not cleaned_html:
        return

    prompt = f"""You are an e-commerce data extraction assistant.
Look at the cleaned HTML skeleton of the current product page and directly extract values ONLY for the requested missing fields: {missing_fields}.

HTML SKELETON:
{cleaned_html}

Return ONLY a valid JSON object matching this schema:
{{
  "brand_name": "string or empty",
  "manufacturer": "string or empty",
  "country_of_origin": "string or empty",
  "old_price": number or 0.0,
  "sales_notes": "string or empty",
  "bonus": "string or empty"
}}
"""
    res = await selector_generator._call_llm(prompt, step_name="OLLAMA FILLER")
    if not res:
        return

    if "brand_name" in missing_fields and res.get("brand_name"):
        parent_product.brand_name = str(res["brand_name"]).strip()
    if "manufacturer" in missing_fields and res.get("manufacturer"):
        parent_product.manufacturer = str(res["manufacturer"]).strip()
    if "country_of_origin" in missing_fields and res.get("country_of_origin"):
        parent_product.country_of_origin = str(res["country_of_origin"]).strip()
    if "old_price" in missing_fields and res.get("old_price"):
        try:
            op = float(res["old_price"])
            if op > parent_product.fact_price:
                parent_product.price = op
        except Exception:
            pass
    if "sales_notes" in missing_fields and res.get("sales_notes"):
        parent_product.sales_notes = str(res["sales_notes"]).strip()
    if "bonus" in missing_fields and res.get("bonus"):
        parent_product.bonus = str(res["bonus"]).strip()
        parent_product.update_bonus()    

async def fetch_remote_target_urls(site_id: str, shop_engine: str = "general", history_hash: str = "") -> list:
    """Асинхронно запрашивает массив целевых ссылок с сервера chitta.site по site_id, shop_engine и history_hash."""
    api_url = getattr(config, "CHITTA_API_URL", "")
    api_key = getattr(config, "CHITTA_API_KEY", "")
    
    hash_log = f", history_hash='{history_hash}'" if history_hash else ""
    # logger.info(f"🌐 [API REQUEST] Запрос TARGET_URLS для site_id='{site_id}', shop_engine='{shop_engine}'{hash_log} ({api_url})")

    payload = {
        "api_key": api_key,
        "action": "get_target_urls",
        "site_id": site_id,
        "shop_engine": shop_engine
    }
    
    if history_hash:
        payload["history_hash"] = history_hash

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(api_url, json=payload)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    logger.info(f"✅ [API SUCCESS] Успешно получено {len(data)} целевых ссылок с сервера.")
                    return data
                
                # Вариант 2: Сервер вернул словарь {"status": "success", "target_urls": [...]}
                elif isinstance(data, dict):
                    urls = data.get("target_urls") or data.get("urls") or []
                    if isinstance(urls, list) and urls:
                        logger.info(f"✅ [API SUCCESS] Успешно получено {len(urls)} целевых ссылок с сервера.")
                        return urls
                    elif data.get("status") == "error":
                        logger.error(f"❌ [API ERROR] Сервер вернул ошибку: {data.get('message', 'Неизвестная ошибка')}")
                    else:
                        logger.warning("⚠️ [API WARN] Ответ сервера не содержит списка ссылок.")                    
            else:
                logger.error(f"❌ [API HTTP ERROR] Код ответа сервера: {response.status_code}")
    except Exception as e:
        logger.error(f"💥 [API EXCEPTION] Сбой при запросе к серверу API: {e}")
    
    return []

async def run_parser(
    target_url: str, 
    max_parents: int = 100, 
    enable_ollama: bool = False, 
    output_prefix: str = getattr(config, "DEFAULT_OUTPUT_PREFIX", "chitta_catalog"),
    remote_target_urls: bool = False,
    engine_name: str = "general",
    version: str = "v2",
    llm_provider: str = getattr(config, "DEFAULT_LLM_PROVIDER", "gemini")
):
    state_machine = CatalogStateMachine(max_parents=max_parents)
    universal_parser = UniversalSemanticParser()
    engine_detector = EngineDetector(universal_parser)

    enricher = OllamaEnricher(
        model_name=getattr(config, "DEFAULT_OLLAMA_MODEL", "qwen2.5-coder:3b"),
        host=getattr(config, "DEFAULT_OLLAMA_HOST", "http://localhost:11434"),
        timeout=getattr(config, "DEFAULT_OLLAMA_TIMEOUT", 180)
    ) if enable_ollama else None

    # logger.info(f"🚀 [START] Запуск парсинга каталога: {target_url}")
    # logger.info(f"📌 [VERSION] Режим работы парсера: {version.upper()} ({'Семантический / Универсальный v1' if version == 'v1' else 'Ollama AI-селекторы v2'})")

    # Определение источника целевых ссылок
    custom_urls = []
    if remote_target_urls:
        # Извлекаем history_hash, если он был передан строкой и не является служебным флагом "true"
        parsed_hash = ""
        if isinstance(remote_target_urls, str) and remote_target_urls.strip().lower() not in ["true", "1", "yes"]:
            parsed_hash = remote_target_urls.strip()

        hash_msg = f" (history_hash: {parsed_hash})" if parsed_hash else ""
        # logger.info(f"📡 [REMOTE] Запрашиваем TARGET_URLS из chitta.site для site_id='{output_prefix}' (Engine: {engine_name}){hash_msg}...")
        custom_urls = await fetch_remote_target_urls(
            site_id=output_prefix, 
            shop_engine=engine_name, 
            history_hash=parsed_hash
        )      
    else:
        custom_urls = getattr(config, "TARGET_URLS", [])        

    try:
        async with AsyncCamoufox(
            headless=True, 
            locale="uk-UA",
            addons=[],
        ) as browser:
            page = await browser.new_page()

            # 1. Загрузка страницы каталога и сбор ссылок на товары
            # logger.info("🔍 [SCAN] Загрузка страницы каталога...")
            target_url = target_url.strip()

            if custom_urls:
                target_links = [u for u in custom_urls if u.rstrip('/') != target_url.rstrip('/')]
                state_machine.max_parents = len(target_links)
                # logger.info(f"⚙️ [REMOTE CONFIG] Очередь сформирована из {len(target_links)} целевых ссылок API.")
            else:
                logger.info("🔍 [SCAN] Загрузка страницы каталога для сбора ссылок...")
                for attempt in range(1, 4):
                    try:
                        if page.is_closed():
                            page = await browser.new_page()
                        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(1500)
                        break
                    except Exception as e:
                        if attempt == 3:
                            logger.error(f"❌ [ERROR] Не удалось открыть целевой URL {target_url} после {attempt} попыток: {e}")
                            return
                        logger.warning(f"⚠️ [RETRY] Попытка {attempt} не удалась, повтор через 2 сек...")
                        await asyncio.sleep(2)

                catalog_engine = await engine_detector.select_engine(page, target_url, forced_engine=engine_name)            
                product_links = await catalog_engine.extract_catalog_links(page)
                if not product_links:
                    product_links = [target_url]
                target_links = product_links[:max_parents]        
            
            logger.info(f"⚙️ [CONFIG] В очереди на парсинг: {len(target_links)} товаров.")

            # 2. Обход карточек товаров
            consecutive_blocks = 0

            # Инициализация сервисов для v2
            html_cleaner = HTMLCleaner()
            selector_generator = SelectorGenerator(
                model_name=getattr(config, "DEFAULT_OLLAMA_MODEL", "qwen2.5-coder:3b"),
                host=getattr(config, "DEFAULT_OLLAMA_HOST", "http://localhost:11434"),
                timeout=getattr(config, "DEFAULT_OLLAMA_TIMEOUT", 180),
                provider=llm_provider
            )
            fast_parser = FastSelectorParser(universal_parser=universal_parser)

            # Предварительная генерация карты v2 ДО входа в основной цикл обхода
            cached_selectors_map = None

            # === ВРЕМЕННАЯ ЗАГЛУШКА ДЛЯ ЭКОНОМИИ LLM ===
            if getattr(config, "USE_HARDCODED_SELECTOR_MAP", False):
                cached_selectors_map = getattr(config, "HARDCODED_SELECTOR_MAP", {})
                logger.info("⚡ [STUB] Используется захардкоженная карта селекторов (Gemini отключен).")
            # ============================================

            # Генерируем карту через LLM только если заглушка не сработала
            if version == "v2" and target_links and not cached_selectors_map:
                training_urls = target_links[:min(1, len(target_links))]
                logger.info(f"🧠 [v2 PHASE 1 MULTI] Генерация мульти-карты по {len(training_urls)} карточкам...")
                
                generated_maps = []
                for t_idx, t_url in enumerate(training_urls, 1):
                    try:
                        if page.is_closed():
                            page = await browser.new_page()
                        
                        try:
                            await page.goto(t_url, wait_until="domcontentloaded", timeout=20000)
                        except Exception as goto_err:
                            logger.warning(f"⚠️ [v2 GOTO WARN] Повторная попытка открытия {t_url}: {goto_err}")
                            await asyncio.sleep(1)
                            await page.goto(t_url, wait_until="domcontentloaded", timeout=20000)

                        try:
                            await page.wait_for_selector("h1, main, article, [itemprop='name']", timeout=3000)
                        except Exception:
                            await page.wait_for_timeout(1500)

                        raw_html = await page.content()
                        cleaned_html = html_cleaner.clean_html_for_selectors(raw_html, provider=llm_provider)
                        
                        if not cleaned_html or len(cleaned_html) < 200:
                            p_title = await page.title()
                            logger.error(
                                f"⛔ [v2 PHASE 1 DIAGNOSTIC] Пропуск страницы {t_url} — слишком короткий скелет ({len(cleaned_html) if cleaned_html else 0} симв.)!\n"
                                f"   • Заголовок окна браузера: '{p_title}'\n"
                                f"   • Текущий URL в браузере: '{page.url}'\n"
                                f"   • Очищенный контент: '{cleaned_html}'"
                            )
                            continue

                        g_map = await selector_generator.generate_selectors(cleaned_html, engine_name=engine_name)
                        if g_map:
                            generated_maps.append(g_map)
                            logger.info(f"✅ [v2 CARD {t_idx}] Карта селекторов успешно сгенерирована!")
                        else:
                            logger.warning(f"⚠️ [v2 CARD {t_idx}] Gemini не смогла сформировать селекторы для {t_url}")
                    
                    except Exception as err:
                        logger.warning(f"⚠️ [v2 PHASE 1 ERR] Ошибка анализа {t_url}: {err}")

                if generated_maps:
                    cached_selectors_map = merge_selector_maps(generated_maps)
                    logger.info(f"✅ [v2 PHASE 1 SUCCESS] Мульти-карта успешно собрана!")
                    logger.info(f"📋 [MULTI-SELECTORS MAP]:\n{json.dumps(cached_selectors_map, ensure_ascii=False, indent=2)}")

            try:
                processed_urls = set()

                for idx, link in enumerate(target_links, 1):
                    if state_machine.is_limit_reached():
                        logger.warning("⚠️ [LIMIT] Лимит товаров достигнут, остановка обхода.")
                        break

                    link = str(link).strip()
                    clean_link_key = link.split('?')[0].split('#')[0].rstrip('/')

                    # Пропускаем URL, если эта ссылка уже обрабатывалась
                    if clean_link_key in processed_urls:
                        logger.info(f"⏩ [SKIP PROCESSED URL] Ссылка {link} уже обработана ранее.")
                        continue

                    processed_urls.add(clean_link_key)

                    # Ротация вкладки браузера каждые 25 товаров для сброса накопленной оперативной памяти (RAM)
                    if idx > 1 and (idx - 1) % 25 == 0:
                        try:
                            await page.close()
                            page = await browser.new_page()
                            logger.info(f"🔄 [MEMORY RECYCLING] Вкладка браузера перезапущена для освобождения RAM (обработано товаров: {idx - 1}).")
                        except Exception as e:
                            logger.warning(f"⚠️ [MEMORY RECYCLING WARN] Не удалось перезапустить вкладку: {e}")
                            # Если не удалось создать новую страницу, пробуем создать её при следующей итерации
                            if page.is_closed():
                                try:
                                    page = await browser.new_page()
                                except Exception as e2:
                                    logger.error(f"❌ [MEMORY RECYCLING ERROR] Критическая ошибка создания новой вкладки: {e2}")
                                    raise

                    logger.info(f"🌐 [PAGE] Обработка товара [{idx}/{len(target_links)}]: {link}") 

                    try:
                        if page.is_closed():
                            page = await browser.new_page()
                        await page.goto(link, wait_until="domcontentloaded", timeout=20000)
                        try:
                            # Ждем завершения важных стартовых запросов (API товаров, скрипты)
                            await page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            try:
                                await page.wait_for_selector("h1, main, article, [itemprop='name']", timeout=2000)
                            except Exception:
                                await page.wait_for_timeout(1000)

                        page_title = (await page.title()).lower()
                        page_content_snippet = (await page.content())[:2000].lower()
                        blocked_signals = ["sorry, you have been blocked", "access denied", "attention required", "cloudflare", "ddos-guard"]

                        if any(sig in page_title or sig in page_content_snippet for sig in blocked_signals):
                            consecutive_blocks += 1
                            logger.error(f"⛔ [ANTI-BOT BLOCK] Товар {link} заблокирован защитой (Cloudflare/DDoS-Guard). [Блокировок подряд: {consecutive_blocks}]")
                            if consecutive_blocks >= 3:
                                logger.critical("🛑 [CRITICAL ANTI-BOT] Зафиксировано 3 блокировки подряд. IP-адрес заблокирован защитой сайта. Остановка сессии.")
                                break
                            continue

                        consecutive_blocks = 0                    

                        # =========================================================================
                        # СКВОЗНОЙ ГИБРИДНЫЙ КОНВЕЙЕР (v2 FastSelector -> v1 CMS / Generic)
                        # =========================================================================
                        
                        # 1. ВЫПОЛНЕНИЕ v2 (FastSelectorParser по карте Gemini) — ПРИОРИТЕТНЫЙ ПЕРВЫЙ СЛОЙ
                        v2_product = None
                        if version == "v2" and cached_selectors_map:
                            v2_product = await fast_parser.extract_product(
                                page=page, 
                                url=link, 
                                selectors_map=cached_selectors_map, 
                                state_machine=state_machine
                            )

                        # === ОБРАБОТКА ТИХОГО ПРОПУСКА ===
                        if v2_product is None:
                            # logger.info(f"⏩ [SKIP URL] Пропуск обработки {link}, так как это уже собранный вариант.")
                            continue  # Переходим к следующей ссылке в цикле, не заходя в v1 и не вызывая ошибок

                        # ================================
                        # === ДИАГНОСТИКА 1: Что собрал FastParser? ===
                        # if v2_product and v2_product.variants:
                        #     logger.info(f"🔍 [MAIN DIAG 1] FastParser нашел {len(v2_product.variants)} вариантов.")
                        #     for v in v2_product.variants[:2]:  # Показываем первые 2 для краткости
                        #         logger.info(f"🔍 [MAIN DIAG 1] Вариант SKU={v.sku} | mod_attrs: {v.modification_attributes}")
                        # ==============================================

                        # 2. ВЫПОЛНЕНИЕ v1 (CMS / Generic Engine) — ВТОРИЧНЫЙ СЛОЙ ДОБОРА И СТРАХОВКИ
                        cms_engine = await engine_detector.select_engine(page, link, forced_engine=engine_name)
                        cms_product = await cms_engine.parse_product_page(page, link, state_machine=state_machine, existing_product=v2_product)

                        # 3. НЕРАЗРУШАЮЩЕЕ ОБЪЕДИНЕНИЕ (Приоритет у точных данных v2)
                        parent_product = merge_product_data(primary=v2_product, secondary=cms_product)

                        # 4. СОХРАНЕНИЕ И ДОБОР ИНТЕРАКТИВА (v1 DOM Swatches + Variants)
                        if parent_product and parent_product.product_id:
                            is_added = state_machine.add_parent(parent_product)

                            if not is_added:
                                # КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: Если add_parent вернул False (например, потому что 
                                # этот URL ведет на вариант, который мы уже собрали ранее), мы НЕМЕДЛЕННО 
                                # прерываем обработку этой ссылки. Это предотвращает попытки добавить 
                                # "варианты" к несуществующему родителю и убирает дубликаты из CSV.
                                # logger.info(f"⏩ [SKIP URL] Ссылка {link} ведет на уже известный вариант (ID: {parent_product.product_id}). Данные уже собраны, пропускаем.")
                                continue

                            # Проверяем, не был ли этот ID уже собран как вариант другого родительского товара
                            if parent_product.product_id in state_machine.registered_variant_ids:
                                logger.info(f"⏩ [SKIP VARIANT AS PARENT] Товар ID: {parent_product.product_id} уже собран как вариант другого товара. Пропуск добора.")
                                continue

                            logger.info(f"🔹 [VARIANTS DOM] Добор вариантов через Universal Parser для ID: {parent_product.product_id}...")
                            try:
                                await universal_parser._extract_interactive_variants(page, link, state_machine, parent_product.product_id)
                                await universal_parser._extract_link_variants(
                                    page, 
                                    link, 
                                    state_machine, 
                                    parent_product.product_id,
                                    fast_parser=fast_parser,
                                    selectors_map=cached_selectors_map
                                )
                                # === ДИАГНОСТИКА 2: Что осталось после Universal Parser? ===
                                # if parent_product and parent_product.variants:
                                #     logger.info(f"🔍 [MAIN DIAG 2] После UniversalParser в parent_product осталось {len(parent_product.variants)} вариантов.")
                                #     for v in parent_product.variants[:2]:
                                #         logger.info(f"🔍 [MAIN DIAG 2] Вариант SKU={v.sku} | mod_attrs: {v.modification_attributes}")
                                # ==========================================================

                            except Exception as var_err:
                                logger.warning(f"⚠️ [VARIANTS WARN] Ошибка добора вариантов: {var_err}")

                            sync_parent_ids_to_variants(parent_product)    

                            # 5. OLLAMA SMART FALLBACK (Delta Extraction)
                            has_variants = bool(parent_product.variants)
                            has_mod_attributes = any(v.modification_attributes for v in parent_product.variants) or bool(parent_product.modification_attributes)

                            # Нормализация атрибутов через LLM или эвристику
                            if has_variants and enable_ollama and enricher:
                                logger.info(f"🤖 [ATTRIBUTE NORMALIZER] Запуск нормализации атрибутов для ID: {parent_product.product_id}...")
                                try:
                                    # Создаем нормализатор с тем же Ollama клиентом
                                    normalizer = AttributeNormalizer(ollama_client=enricher)
                                    await normalizer.normalize_parent_attributes(parent_product)
                                    
                                    # Дополнительно запускаем Delta Extraction, если модификации все еще не заполнены
                                    has_mod_attributes = any(v.modification_attributes for v in parent_product.variants) or bool(parent_product.modification_attributes)
                                    if not has_mod_attributes:
                                        logger.info(f"🤖 [OLLAMA SMART FALLBACK] Delta Extraction для вариантов ID: {parent_product.product_id}...")
                                        html_body = await page.content()
                                        parent_md = enricher.extract_markdown_from_html(html_body, parent=parent_product)
                                        if parent_md:
                                            await enricher.process_variants_attributes(parent_product, parent_md=parent_md)
                                    
                                    # Финальная очистка и синхронизация
                                    parent_product.sanitize_modification_attributes()
                                except Exception as norm_err:
                                    logger.warning(f"⚠️ [ATTRIBUTE NORMALIZER WARN] Ошибка нормализации: {norm_err}")
                            elif has_variants and not has_mod_attributes:
                                # Эвристическая нормализация без LLM
                                logger.info(f"🔧 [HEURISTIC NORMALIZER] Применяем эвристику для ID: {parent_product.product_id}...")
                                try:
                                    normalizer = AttributeNormalizer(ollama_client=None)
                                    await normalizer.normalize_parent_attributes(parent_product)
                                    parent_product.sanitize_modification_attributes()
                                except Exception as heur_err:
                                    logger.warning(f"⚠️ [HEURISTIC NORMALIZER WARN] Ошибка эвристики: {heur_err}")
                        else:
                            logger.error(f"❌ [PARSING FAIL] Не удалось определить валидный системный product_id для: {link}")

                    except Exception as e:
                        logger.error(f"❌ [ERROR] Ошибка обработки {link}: {e}")

            except (KeyboardInterrupt, asyncio.CancelledError):
                logger.warning("\n⚠️ [INTERRUPT] Процесс прерван пользователем (Ctrl+C). Остановка обхода и экспорт собранных данных...")

            logger.info("🛑 [START] Завершение работы Camoufox.")

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.warning("\n⚠️ [INTERRUPT] Процесс прерван пользователем (Ctrl+C). Остановка обхода...")
    except Exception as e:
        logger.warning(f"\n⚠️ [BROWSER EXIT WARN] Соединение с браузером закрыто: {e}")

    all_parents = state_machine.get_all_parents()
    total = len(all_parents)

    # === ФАЗА ОЧИСТКИ ЧЕРЕЗ OLLAMA (вместо Gemini) ===
    if total > 0:
        # ПРОХОД 1: Очистка скаляров пачками по 10 штук через Ollama
        logger.info(f"🧹 [OLLAMA PASS 1] Запуск быстрой очистки скаляров для {total} товаров пачками по 10 шт...")
        scalar_batch_size = 10

        for i in range(0, total, scalar_batch_size):
            chunk = all_parents[i:i + scalar_batch_size]
            logger.info(f"  🧼 [SCALARS BATCH {i//scalar_batch_size + 1}] Обработка скаляров {i+1}–{min(i+scalar_batch_size, total)} из {total}...")

            payload = []
            for p in chunk:
                payload.append({
                    "product_id": getattr(p, "product_id", ""),
                    "title": getattr(p, "title", ""),
                    "sku": getattr(p, "sku", ""),
                    "parent_sku": getattr(p, "parent_sku", ""),
                    "brand_name": getattr(p, "brand_name", ""),
                    "model": getattr(p, "model", ""),
                    "manufacturer": getattr(p, "manufacturer", ""),
                    "country_of_origin": getattr(p, "country_of_origin", ""),
                    "currency": getattr(p, "currency", ""),
                    "available": getattr(p, "available", "")
                })

            cleaned_scalars = await selector_generator.clean_scalars_batch(payload)
            scalars_map = {str(item.get("product_id")): item for item in cleaned_scalars if isinstance(item, dict)}

            for p in chunk:
                pid = str(getattr(p, "product_id", ""))
                if pid in scalars_map:
                    cdata = scalars_map[pid]
                    if cdata.get("sku"): p.sku = str(cdata["sku"]).strip()
                    if cdata.get("parent_sku"): p.parent_sku = str(cdata["parent_sku"]).strip()
                    if cdata.get("brand_name"): p.brand_name = str(cdata["brand_name"]).strip()
                    if cdata.get("model"): p.model = str(cdata["model"]).strip()
                    if cdata.get("manufacturer"): p.manufacturer = str(cdata["manufacturer"]).strip()
                    if cdata.get("country_of_origin"): p.country_of_origin = str(cdata["country_of_origin"]).strip()
                    if cdata.get("currency"): p.currency = str(cdata["currency"]).strip()
                    if cdata.get("available"): p.available = str(cdata["available"]).strip()

        # ПРОХОД 2: Очистка характеристик через Ollama — передаем только список уникальных сырых ключей
        logger.info("🧠 [OLLAMA PASS 2] Очистка характеристик на эталонных ключах...")
        raw_keys = set()
        for p in all_parents:
            if hasattr(p, "attributes") and isinstance(p.attributes, dict):
                raw_keys.update(p.attributes.keys())
            if hasattr(p, "modification_attributes") and isinstance(p.modification_attributes, dict):
                raw_keys.update(p.modification_attributes.keys())
            for v in p.variants:
                if hasattr(v, "modification_attributes") and isinstance(v.modification_attributes, dict):
                    raw_keys.update(v.modification_attributes.keys())

        attr_mapping = {}
        if raw_keys:
            attr_mapping = await selector_generator.clean_attributes_mapping(sorted(list(raw_keys)))
            logger.info(f"✅ [ATTR MAPPING SUCCESS] Автоматически построено правил маппинга: {len(attr_mapping)}")

        if attr_mapping:
            # logger.info("⚙️ [LOCAL PYTHON] Применение словаря атрибутов ко всем товарам...")
            for p in all_parents:
                # 1. Очистка родительских attributes
                if getattr(p, "attributes", None) and isinstance(p.attributes, dict):
                    new_attrs = {}
                    for raw_key, val in p.attributes.items():
                        clean_key = attr_mapping.get(raw_key)
                        if clean_key and isinstance(clean_key, str) and clean_key.strip():
                            new_attrs[clean_key.strip()] = val
                        elif clean_key is None and raw_key not in attr_mapping:
                            if raw_key not in ["-", "Обмін та повернення", "Гарантія"]:
                                new_attrs[raw_key] = val
                    p.attributes = new_attrs

                # 2. Очистка родительских modification_attributes
                if getattr(p, "modification_attributes", None) and isinstance(p.modification_attributes, dict):
                    new_mod_attrs = {}
                    for raw_key, val in p.modification_attributes.items():
                        clean_key = attr_mapping.get(raw_key)
                        if clean_key and isinstance(clean_key, str) and clean_key.strip():
                            new_mod_attrs[clean_key.strip()] = val
                        elif clean_key is None and raw_key not in attr_mapping:
                            new_mod_attrs[raw_key] = val
                    p.modification_attributes = new_mod_attrs

                # 3. Очистка modification_attributes у дочерних вариантов
                for v in p.variants:
                    if getattr(v, "modification_attributes", None) and isinstance(v.modification_attributes, dict):
                        v_new_mods = {}
                        for raw_key, val in v.modification_attributes.items():
                            clean_key = attr_mapping.get(raw_key)
                            if clean_key and isinstance(clean_key, str) and clean_key.strip():
                                v_new_mods[clean_key.strip()] = val
                            elif clean_key is None and raw_key not in attr_mapping:
                                v_new_mods[raw_key] = val
                        v.modification_attributes = v_new_mods

    # 3. Экспорт результатов в 5 CSV-файлов
    logger.info(f"📦 [DATA] Итого собрано уникальных товаров: {total}")

    if total > 0:
        logger.info("💾 [EXPORT] Генерация 5 CSV файлов импорта...")

        # ------------------------------------------------------------------
        # ФИНАЛЬНАЯ СИНХРОНИЗАЦИЯ ВАЛЮТ И БАТЧ-НОРМАЛИЗАЦИЯ НАЛИЧИЯ (OLLAMA)
        # ------------------------------------------------------------------
        # logger.info("🧹 [FINAL SANITIZE] Наследование валют и нормализация наличия...")

        # 1. Наследование валюты родителя всеми вариантами + сбор сырых статусов наличия
        raw_availability_statuses = set()

        for parent in state_machine.parents.values():
            if parent.available:
                raw_availability_statuses.add(parent.available)
            for variant in parent.variants:
                # Валюта строго от родителя
                if parent.currency:
                    variant.currency = parent.currency
                if variant.available:
                    raw_availability_statuses.add(variant.available)

        # 2. Нормализация статусов наличия через Ollama (если доступна)
        avail_mapping = {}
        if raw_availability_statuses:
            if enricher:
                avail_mapping = await enricher.normalize_availability_batch(raw_availability_statuses)
            else:
                # Фолбэк без Ollama
                for st in raw_availability_statuses:
                    st_lower = str(st).lower()
                    if any(kw in st_lower for kw in ["instock", "yes", "true", "наявності", "наличии"]):
                        avail_mapping[st] = "yes"
                    else:
                        avail_mapping[st] = "no"

        # 3. Применение нормализованного 'yes' / 'no' ко всем товарам
        if avail_mapping:
            for parent in state_machine.parents.values():
                if parent.available in avail_mapping:
                    parent.available = avail_mapping[parent.available]
                for variant in parent.variants:
                    if variant.available in avail_mapping:
                        variant.available = avail_mapping[variant.available]

        # logger.info(f"✅ [FINAL SANITIZE DONE] Нормализовано {len(avail_mapping)} уникальных статусов наличия строго в 'yes'/'no'.")

        # ------------------------------------------------------------------
        # ПАКЕТНОЕ КЛАСТЕРИЗОВАННОЕ ПЕРЕИМЕНОВАНИЕ БЕЗЫМЯННЫХ ОПЦИЙ (OLLAMA)
        # ------------------------------------------------------------------
        if enricher:
            # logger.info("🏷️ [BATCH CLUSTERING] Сбор и кластеризация безымянных модификаций...")

            signature_to_group = {}  # signature -> group_id
            group_to_values = {}     # group_id -> list of values
            targets = []             # list of (parent_product_obj, old_key, group_id)
            group_counter = 1

            # 1. Группировка уникальных наборов значений со всех товаров
            for parent in state_machine.parents.values():
                anon_keys = set()
                for k in parent.modification_attributes.keys():
                    if k.lower() == "option" or k.lower().startswith("option_"):
                        anon_keys.add(k)
                for v in parent.variants:
                    for k in v.modification_attributes.keys():
                        if k.lower() == "option" or k.lower().startswith("option_"):
                            anon_keys.add(k)

                for old_key in anon_keys:
                    raw_vals = set()
                    for v in parent.variants:
                        val = str(v.modification_attributes.get(old_key, "")).strip()
                        if val:
                            raw_vals.add(val)

                    if not raw_vals:
                        continue

                    sorted_vals = sorted(list(raw_vals))
                    sig = "|".join(v.lower() for v in sorted_vals)

                    if sig not in signature_to_group:
                        gid = f"group_{group_counter}"
                        group_counter += 1
                        signature_to_group[sig] = gid
                        group_to_values[gid] = sorted_vals
                    else:
                        gid = signature_to_group[sig]

                    targets.append((parent, old_key, gid))

            # 2. Однократный запрос в Ollama для всех дедуплицированных групп
            if group_to_values:
                logger.info(f"🤖 [OLLAMA OPTION RENAME] Отправка {len(group_to_values)} уникальных групп значений в Ollama...")
                rename_map = await enricher.rename_anonymous_options_batch(group_to_values)

                # 3. Переименование ключей в памяти Python у всех товаров и вариантов
                renamed_count = 0
                for parent, old_key, gid in targets:
                    new_key = rename_map.get(gid)
                    if new_key and new_key.lower() != old_key.lower():
                        if old_key in parent.modification_attributes:
                            val = parent.modification_attributes.pop(old_key)
                            parent.modification_attributes[new_key] = val
                        else:
                            parent.modification_attributes[new_key] = ""

                        for v in parent.variants:
                            if old_key in v.modification_attributes:
                                val = v.modification_attributes.pop(old_key)
                                v.modification_attributes[new_key] = val

                        renamed_count += 1

                logger.info(f"✅ [OLLAMA OPTION RENAME DONE] Переименовано {renamed_count} ключей модификаций.")

        # === ДИАГНОСТИКА 3: Финальное состояние перед экспортом ===
        # logger.info("🔍 [MAIN DIAG 3] === ФИНАЛЬНАЯ ПРОВЕРКА ПЕРЕД ЭКСПОРТОМ ===")
        # for p in state_machine.get_all_parents()[:1]: # Проверяем первого родителя
        #     logger.info(f"🔍 [MAIN DIAG 3] Родитель {p.product_id} имеет {len(p.variants)} вариантов.")
        #     for v in p.variants[:2]:
        #         logger.info(f"🔍 [MAIN DIAG 3] Вариант SKU={v.sku} | mod_attrs: {v.modification_attributes}")
        # logger.info("🔍 [MAIN DIAG 3] ===========================================")
        # ==============================================
        for parent in state_machine.get_all_parents():
            original_count = len(parent.variants)
            # Оставляем только те варианты, чей ID НЕ равен ID родителя
            parent.variants = [v for v in parent.variants if str(v.product_id) != str(parent.product_id)]
            
            if len(parent.variants) < original_count:
                logger.info(f"🧹 [CLEANUP] Удалено {original_count - len(parent.variants)} невалидных вариантов с ID={parent.product_id} из родителя {parent.product_id}")

        # ------------------------------------------------------------------
        # ТОЧЕЧНАЯ ПРОВЕРКА НАЛИЧИЯ ЧЕРЕЗ LLM (для товаров с available=None)
        # ------------------------------------------------------------------
        logger.info("🔍 [LLM AVAILABILITY] Поиск товаров с неопределенным наличием...")
        availability_check_items = []
        
        for parent in state_machine.get_all_parents():
            # Проверяем родителя
            if parent.available is None:
                availability_md = ""
                if getattr(parent, 'raw_availability_html', ''):
                    availability_md = OllamaEnricher.html_to_markdown(parent.raw_availability_html)
                
                availability_check_items.append({
                    "variant_id": parent.product_id,
                    "title": parent.title,
                    "variant_info": "",
                    "availability_md": availability_md or "No availability context found on page"
                })
            
            # Проверяем варианты
            for variant in parent.variants:
                if variant.available is None:
                    availability_md = ""
                    if getattr(variant, 'raw_availability_html', ''):
                        availability_md = OllamaEnricher.html_to_markdown(variant.raw_availability_html)
                    
                    variant_info = ", ".join([f"{k}: {v}" for k, v in variant.modification_attributes.items()])
                    availability_check_items.append({
                        "variant_id": variant.product_id,
                        "title": parent.title,
                        "variant_info": variant_info,
                        "availability_md": availability_md or "No availability context found on page"
                    })

        if availability_check_items:
            logger.info(f"🔍 [LLM AVAILABILITY] Найдено {len(availability_check_items)} товаров/вариантов с неопределенным наличием. Запуск LLM-батчинга...")
            
            # Разбиваем на чанки: не более 20 товаров и не более 50000 символов
            chunk_size = 20
            max_chars = 50000
            
            chunks = []
            current_chunk = []
            current_chars = 0
            
            for item in availability_check_items:
                item_chars = len(item['availability_md']) + len(item['title']) + len(item['variant_info'])
                if len(current_chunk) >= chunk_size or (current_chars + item_chars > max_chars and current_chunk):
                    chunks.append(current_chunk)
                    current_chunk = []
                    current_chars = 0
                current_chunk.append(item)
                current_chars += item_chars
            if current_chunk:
                chunks.append(current_chunk)
            
            logger.info(f"🔍 [LLM AVAILABILITY] Данные разбиты на {len(chunks)} чанков.")
            
            # Обрабатываем чанки
            all_results = {}
            for i, chunk in enumerate(chunks):
                logger.info(f"  🧼 [AVAILABILITY BATCH {i+1}/{len(chunks)}] Обработка {len(chunk)} товаров...")
                if enricher:
                    batch_res = await enricher.check_availability_batch(chunk)
                    all_results.update(batch_res)
                else:
                    logger.warning("⚠️ [LLM AVAILABILITY] Ollama не инициализирована, пропускаем LLM-проверку наличия.")
            
            # Применяем результаты
            updated_count = 0
            for parent in state_machine.get_all_parents():
                if parent.product_id in all_results:
                    parent.available = all_results[parent.product_id]
                    updated_count += 1
                for variant in parent.variants:
                    if variant.product_id in all_results:
                        variant.available = all_results[variant.product_id]
                        updated_count += 1
            
            logger.info(f"✅ [LLM AVAILABILITY DONE] Обновлено наличие для {updated_count} товаров/вариантов через LLM.")
        else:
            logger.info("✅ [LLM AVAILABILITY] Все товары имеют определенное наличие, LLM-проверка не требуется.")

        # ------------------------------------------------------------------
        # ФИНАЛЬНЫЙ ДЕФОЛТ ДЛЯ НАЛИЧИЯ (перед экспортом)
        # ------------------------------------------------------------------
        default_count = 0
        for parent in state_machine.get_all_parents():
            if parent.available is None:
                parent.available = "yes"
                default_count += 1
            for variant in parent.variants:
                if variant.available is None:
                    variant.available = "yes"
                    default_count += 1
        logger.info(f"✅ [FINAL AVAILABILITY DEFAULT] Установлено значение 'yes' по умолчанию для {default_count} товаров/вариантов.")

        exporter = CSVExporter(output_dir=config.DEFAULT_OUTPUT_DIR)
        exporter.export_all(
            categories=state_machine.get_all_categories(),
            parents=state_machine.get_all_parents(),
            prefix=output_prefix
        )
    else:
        logger.warning("⚠️ [WARN] Товары не собраны. Файлы не созданы.")

if __name__ == "__main__":
    asyncio.run(run_parser(config.DEFAULT_URL, max_parents=2, enable_ollama=False, version="v2"))