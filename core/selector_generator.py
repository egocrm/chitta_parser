# core/selector_generator.py — Разделенный генератор карт CSS-селекторов (v2, 4 промпта)

import logging
import json
import httpx
from typing import Dict, Any
import config

logger = logging.getLogger("ChittaParser")


class SelectorGenerator:
    """Генерирует карту CSS-селекторов, используя 4 узкоспециализированных запроса к Ollama."""

    def __init__(
        self, 
        model_name: str = "qwen2.5-coder:7b", 
        host: str = "http://localhost:11434", 
        timeout: int = 180,
        provider: str = getattr(config, "DEFAULT_LLM_PROVIDER", "gemini")
    ):
        self.model_name = model_name
        self.host = host.rstrip('/')
        self.timeout = timeout
        self.provider = provider.lower()

    async def _call_llm(self, prompt: str, step_name: str = "") -> Dict[str, Any]:
        """Единая точка вызова LLM. Маршрутизирует запрос в Gemini или Ollama."""
        if self.provider == "gemini":
            return await self._call_gemini(prompt, step_name)
        return await self._call_ollama(prompt, step_name)

    async def _call_ollama(self, prompt: str, step_name: str = "") -> Dict[str, Any]:
        """Отправляет промпт в Ollama с логированием отправленного текста и сырого ответа."""
        url = f"{self.host}/api/generate"
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "num_ctx": 16384,
                "num_predict": 2048,
                "temperature": 0.0,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=float(self.timeout)) as client:
                response = await client.post(url, json=payload)
                if response.status_code == 200:
                    res_json = response.json()
                    response_text = res_json.get("response", "").strip()

                    # if step_name:
                        # logger.info(
                        #     f"\n==================== [OLLAMA RAW RESPONSE: {step_name}] ====================\n"
                        #     f"{response_text}\n"
                        #     f"============================================================================="
                        # )

                    return json.loads(response_text)
                else:
                    logger.error(f"❌ [OLLAMA ERROR] Статус HTTP: {response.status_code}")
        except json.JSONDecodeError as e:
            logger.error(f"❌ [OLLAMA JSON ERROR] Невалидный JSON от Ollama: {e}")
        except Exception as e:
            logger.error(f"💥 [OLLAMA EXCEPTION] Ошибка запроса к Ollama: {e}")

        return {}

    async def _call_gemini(self, prompt: str, step_name: str = "") -> Dict[str, Any]:
        """Отправляет промпт в Google Gemini API с затребованной формой response_mime_type='application/json'."""
        api_key = getattr(config, "GEMINI_API_KEY", "")
        model = getattr(config, "GEMINI_MODEL", "gemini-2.5-flash")
        
        if not api_key:
            logger.error("❌ [GEMINI ERROR] Не задан GEMINI_API_KEY в config.py!")
            return {}

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.0
            }
        }

        gemini_timeout = 60.0  # Оптимальный таймаут для Gemini Flash

        for attempt in range(1, 3):
            try:
                async with httpx.AsyncClient(timeout=gemini_timeout) as client:
                    response = await client.post(url, json=payload)
                    
                    if response.status_code == 200:
                        res_json = response.json()
                        candidates = res_json.get("candidates", [])
                        
                        if not candidates:
                            logger.error(f"❌ [GEMINI BLOCKED] API вернул 200 OK, но список candidates пуст!")
                            logger.error(f"📄 [GEMINI RAW BODY]: {json.dumps(res_json, ensure_ascii=False, indent=2)}")
                            return {}

                        candidate = candidates[0]
                        finish_reason = candidate.get("finishReason", "UNKNOWN")

                        if finish_reason not in ["STOP", "MAX_TOKENS"]:
                            logger.error(f"⚠️ [GEMINI FINISH REASON] Генерация прервана по причине: '{finish_reason}'")

                        parts = candidate.get("content", {}).get("parts", [])
                        if not parts or "text" not in parts[0]:
                            logger.error(f"❌ [GEMINI NO TEXT] В ответе отсутствуют текстовые данные (parts)!")
                            return {}

                        response_text = parts[0]["text"].strip()
                        return json.loads(response_text)

                    else:
                        logger.error(f"❌ [GEMINI HTTP ERROR {response.status_code}]: {response.text}")

            except httpx.TimeoutException:
                logger.warning(f"⏰ [GEMINI TIMEOUT] Попытка {attempt}/2 превысила {gemini_timeout}s.")
                if attempt == 2:
                    logger.error("❌ [GEMINI TIMEOUT FAIL] Запрос окончательно сорван по таймауту.")
            except json.JSONDecodeError as e:
                logger.error(f"❌ [GEMINI JSON ERROR] Невалидный JSON от Gemini: {e}")
                break
            except Exception as e:
                logger.error(f"💥 [GEMINI EXCEPTION] Ошибка: {type(e).__name__} - {e}")
                break

        return {}      

    def _build_prompt_pass1(self, cleaned_html: str, engine_name: str) -> str:
        """Проход 1 для Gemini: 100% детализация скаляров, медиа, описания и вариантов без сокращений."""
        example_schema = {
            "product_id_element": "CSS_SELECTOR targeting primary product ID elements inside main buy block",
            "product_id_attr": "ATTRIBUTE_NAME for primary product ID elements (e.g. value, data-product-id)",
            "title": "CSS_SELECTOR (e.g. .product-title h1)",
            "model": "CSS_SELECTOR or empty string",
            "sku": "CSS_SELECTOR (e.g. .sku-value)",
            "price_container": "CSS_SELECTOR for product price block/wrapper containing current or both prices",
            "currency": "CSS_SELECTOR or empty string",
            "available": "CSS_SELECTOR (e.g. .stock-status)",
            "brand_name": "CSS_SELECTOR (e.g. .brand-link a)",
            "sales_notes": "CSS_SELECTOR or empty string",
            "main_image_element": "CSS_SELECTOR (e.g. .main-image img or parent a)",
            "main_image_attr": "ATTRIBUTE_NAME (e.g. src, href, data-src)",
            "gallery_item_element": "CSS_SELECTOR matching ALL thumbnails/gallery links",
            "gallery_item_attr": "ATTRIBUTE_NAME (e.g. href, src, data-href)",
            "description": "CSS_SELECTOR for main product description block/tab",
            "variant_selectors": {
                "<actual_group_name_1>": {
                    "container": "CSS_SELECTOR for the wrapper of this specific option group",
                    "item": "CSS_SELECTOR for individual option buttons/swatches/selects in this group",
                    "value_attr": "ATTRIBUTE_NAME holding the option value (e.g. data-value, title, value, or '' if text)"
                },
                "<actual_group_name_2>": {
                    "container": "CSS_SELECTOR for the wrapper of this specific option group",
                    "item": "CSS_SELECTOR for individual option buttons/swatches/selects in this group",
                    "value_attr": "ATTRIBUTE_NAME holding the option value"
                }
            }
        }

        cms_hint = ""
        if engine_name and engine_name.lower() != "general":
            cms_hint = (
                f"TARGET CMS PLATFORM ENGINE: \"{engine_name}\". "
                f"Use typical {engine_name} e-commerce DOM structure patterns as guidance "
                f"(e.g., hidden inputs 'input[name=\"product_id\"]', buy buttons '#button-cart', price tags '.price-new', '.price-old').\n\n"
            )

        return f"""{cms_hint}### ROLE & PURPOSE:
You are a Senior Web Scraping Architect. Your goal is to analyze the provided cleaned HTML skeleton of a product page and generate a precise, highly resilient W3C CSS selector map in JSON format.

### CRITICAL RUNTIME CONTEXT:
The selector map you produce will be executed directly by an automated headless Playwright engine. The robot will use these selectors blindly across thousands of different product pages on this site.
- If you anchor a selector to a discount color (e.g. `.text-red-700`), the robot WILL FAIL to extract prices for regular non-discounted items.
- If you hardcode a brand slug into a link selector (e.g. `a[href*='/asus/']`), the robot WILL BREAK on products from any other brand.

YOUR OBJECTIVE: Identify semantic, universal DOM nodes (microdata `[itemprop='...']`, data attributes, IDs, HTML structures `h1`, `.product-title`, `.price-box`) that remain 100% stable across ALL product pages on this store. Return strictly ONE clean CSS selector string per field.

### STRICT WARNING — DO NOT EXTRACT TEXT OR DATA!
INCORRECT OUTPUT (DO NOT DO THIS):
{{
  "title": "Стеллаж для дров VOREL 100 см",
  "fact_price": 1890,
  "sku": "A100666"
}}

CORRECT OUTPUT (DO THIS ONLY):
{{
  "product_id_element": "input[name='product_id'], button[data-product-id]",
  "product_id_attr": "value, data-product-id",
  "title": ".product-title h1",
  "model": "",
  "sku": ".sku-code span",
  "price_container": ".price-box",
  "currency": ".currency-symbol",
  "available": ".stock-status",
  "brand_name": ".brand-link",
  "sales_notes": "",
  "main_image_element": ".main-image a",
  "main_image_attr": "href",
  "gallery_item_element": ".gallery-item a",
  "gallery_item_attr": "href",
  "description": "#product_description"
}}

### TARGET JSON SCHEMA KEYS TO USE (DO NOT CHANGE KEY NAMES!):
{json.dumps(example_schema, indent=2)}

### DETAILED SEMANTIC FIELD GUIDANCE:

1. PRODUCT ID & BASIC SCALARS:
- "product_id_element": Target ONLY primary elements inside the MAIN purchasing section or main product form (e.g. hidden inputs `input[name='product_id']`, main add-to-cart button `button[data-product-id]`, or primary product container attribute).
  * STRICT FORBIDDEN: NEVER include global page attributes, widget containers (`[data-widget]`, `[data-merch]`), comment sections (`[data-product-comments]`), recommendation carousels, or footer trackers.
  * Return maximum 1 to 3 tight primary selectors separated by commas.
- "product_id_attr": Attribute names corresponding strictly to those 1-3 primary product_id_element nodes (e.g., "value, data-product-id, data-id").
- "title": CSS selector for product main title heading (typically 'h1').
- "model": CSS selector for model name or product series.
- "sku": CSS selector for vendor article code, item SKU, or product code label.
- "currency": CSS selector targeting currency code or symbol element.
- "available": CSS selector targeting the GENERAL CONTAINER or WRAPPER that holds stock availability information (e.g., `.stock-wrapper`, `.product-availability`, `[data-availability-block]`, `[itemprop='offers']`). 
  * CRITICAL RULE: DO NOT target specific status elements like `.in-stock`, `.out-of-stock`, or `.stock-status` directly, because these classes may dynamically change or be hidden when the user switches variants. 
  * Instead, target the PARENT CONTAINER that wraps all availability states. The parser will extract visible text from this container and analyze it.
  * Example: If the HTML has `<div class="stock-wrapper"><span class="in-stock"
- "brand_name": CSS selector targeting manufacturer or brand link/text.

2. PRICES ("price_container"):
- BALANCED NARROW CONTAINER RULE: Target the NARROWEST possible HTML element/container that encloses ALL price components together: the **current/fact price**, the **old/original price** (if present), AND the **currency symbol/code**.
    * DO NOT make it so narrow that it excludes the old price or currency (e.g., DO NOT select ONLY `span.new-price` if `span.old-price` is right next to it inside `.price-box`).
    * DO NOT target top-level page wrappers, entire product cards, or cart sections (e.g. NEVER return `.product-cart__price-box`, `div.product`, `main`, `article`, `#app`, `.product-info`, or `.product-detail`).
- NEVER invent, guess, or substitute top-level page containers (e.g. NEVER return "div.product", "main", "article", "#app", ".product-info", or ".product-detail") just to fill this key.
- If a price IS present, target the narrowest possible HTML element (e.g. .product-price, [itemprop='price'], .price-box).
- FEW-SHOT EXAMPLES:
  * HTML without price: `<div class="product"><h1>Товар</h1><button>Заказать звонок</button></div>`
    -> "price_container": ""
  * HTML with price: `<div class="product"><h1>Товар</h1><div class="price-box"><span>100 грн</span></div></div>`
    -> "price_container": ".price-box" (or "div.price-box")

3. SPECIAL SALES NOTES ("sales_notes"):
- CONCEPT: Specific purchasing conditions, minimum quantity terms, or deposit requirements unique to THIS SPECIFIC ITEM.
- POSITIVE EXAMPLES: "20% prepayment required", "Minimum order: 5 pcs", "Sold in packs of 10", "Custom order (3-5 days delivery)", "Pickup only".
- NEGATIVE EXAMPLES (DO NOT TARGET): General site-wide shipping method lists (Nova Poshta, Ukrposhta), payment options (Monobank, PrivatBank, credit choices), standard store warranty blocks ("14 days return policy"), or store benefit banners.
- FALLBACK: If no specific sales note or order condition exists for the item, set strictly to "".

4. MEDIA & GALLERY:
- "main_image_element": CSS selector targeting the primary main product photo (`img` or parent `a`).
- "main_image_attr": Attribute holding the image URL (e.g. "href", "src", "data-src", "data-lazy", "data-zoom-image"). Look for high-resolution link/data attributes first.
- "gallery_item_element": CSS selector matching ALL product thumbnail or gallery image elements on the page.
- "gallery_item_attr": Attribute holding image URLs in gallery items (e.g. "href", "src", "data-src", "data-href").

5. DESCRIPTION:
- "description": CSS selector targeting the main product description text block or tab content panel.

6. VARIANT SELECTORS (CRITICAL FOR MODIFICATIONS):
- "variant_selectors": This MUST be a dictionary where the KEYS are the ACTUAL, DYNAMIC names of the option groups found on the page (e.g., "color", "size", "memory", "volume", "capacity", "ram", "storage"). 
- DO NOT hardcode "color" or "size" as keys if the product uses different terminology (like "RAM" and "Storage" for laptops). 
- For EACH detected group, provide:
  * "container": CSS selector for the wrapper containing this specific group's choices.
  * "item": CSS selector for the individual option elements (buttons, radio inputs, select options, swatches).
  * "value_attr": The attribute name holding the option's value (e.g., "data-value", "title", "value"). Use "" if the value is purely in the text content.
- If no variant selectors exist on the page, set "variant_selectors" to an empty object.


### OPERATIONAL RULES:
- MUST output valid W3C CSS selectors ONLY as JSON string values.
- DO NOT invent new keys. Use EXACTLY the schema keys above.
- STRICT ABSENCE POLICY: If any element (price, sku, model, brand, etc.) is missing or not explicitly present in the provided HTML skeleton, set its selector value strictly to "". DO NOT use wrapper fallback containers!
- NEVER put actual extracted text, titles, numbers, or prices in the values.
- PROHIBITED: Do NOT use jQuery selectors like ':contains()'.
- SINGLE SELECTOR RULE: For all fields EXCEPT `product_id_element` and `product_id_attr`, return strictly ONE clean W3C CSS selector (DO NOT use comma-separated fallback chains for title, price, sku, etc.).

### SELECTOR QUALITY RULES (STRICT):
1. For "product_id_element" and "product_id_attr", return ONLY 1 to 3 primary candidates anchored tightly to the main product purchase area/form. DO NOT collect global tracking or widget attributes from across the entire page.
2. Return strictly ONE clean W3C CSS selector string per field. Do NOT provide comma-separated fallback chains.
3. Target semantic tags, microdata, or component structure (`[itemprop='...']`, `[data-price]`, `h1`, `.product-title`, `.price-box`).
4. Never hardcode specific brand names or category slugs into selectors (e.g. NEVER use `a[href*='/asus/']`). Target generic structural elements like `.brand-link a`.

HTML SKELETON:
{cleaned_html}
""" 

    def _build_prompt_pass2(self, cleaned_html: str, engine_name: str) -> str:
        """Проход 2 для Gemini: 100% сфокусированный сбор таблицы характеристик с запретами и правилами разделения."""
        example_schema = {
            "attributes": {
                "container": "CSS_SELECTOR for full specs table/wrapper",
                "row": "CSS_SELECTOR for spec row inside container",
                "key": "CSS_SELECTOR for label element inside row",
                "value": "CSS_SELECTOR for value element inside row"
            }
        }

        cms_hint = ""
        if engine_name and engine_name.lower() != "general":
            cms_hint = (
                f"TARGET CMS PLATFORM ENGINE: \"{engine_name}\". "
                f"Use typical {engine_name} e-commerce DOM structure patterns as guidance.\n\n"
            )

        return f"""{cms_hint}### ROLE & PURPOSE:
You are a Structured Data Extraction Engineer. Your goal is to analyze the provided HTML skeleton and build a W3C CSS selector map exclusively for technical product specification tables.

### CRITICAL RUNTIME CONTEXT:
An automated scraper uses this map for sequential DOM iteration. It first selects the main wrapper (`container`), splits it into parameter rows (`row`), and extracts the attribute name (`key`) and value (`value`) inside each row.
- `key` and `value` MUST target DIFFERENT child elements inside each row.
- All nested selectors (`row`, `key`, `value`) MUST be relative to the container and rely purely on DOM hierarchy (`tr`, `td:first-child`, `td:last-child`, `dt`, `dd`), NEVER on presentation styles (`.font-bold`, `.border-b`, `.flex`).
- Strictly DO NOT capture specification containers inside general product description fields.

### STRICT WARNING — DO NOT EXTRACT TEXT OR DATA!
INCORRECT OUTPUT (DO NOT DO THIS):
{{
  "attributes": {{
    "container": "#product_attributes",
    "row": ".attr-item",
    "key": "Производитель",
    "value": "Bosch"
  }}
}}

CORRECT OUTPUT (DO THIS ONLY):
{{
  "attributes": {{
    "container": "#product_attributes .rm-product-tabs-attributtes-list",
    "row": ".rm-product-tabs-attributtes-list-item",
    "key": "div:first-child",
    "value": "div:last-child"
  }}
}}

### TARGET JSON SCHEMA:
{json.dumps(example_schema, indent=2)}

### DETAILED SPECIFICATION FIELD GUIDANCE:

1. CONCEPT OF ATTRIBUTES:
- Structured Key-Value pairs representing physical, technical, functional, or meta properties describing the product (e.g., "Manufacturer: Bosch", "Code: A100663", "Material: Steel", "Weight: 10kg", "Power: 220V").

2. HOW AND WHERE TO FIND:
- Scan the HTML for repeating structured key-value pairs.
- Target dedicated specification containers inside tabs, panels, tables, or definition lists (e.g., '#product_attributes', '#tab-specification', '.product-attributes', '.attributes-list', 'table.table-striped', 'dl.specs').

3. STRICT PROHIBITIONS:
- DO NOT target top header summary blocks (e.g., '.rm-product-center-info', '.product-meta') that only hold basic SKU, Brand, Rating/Reviews, or Stock badges.
- DO NOT target standard unstructured description text blocks (e.g., '#product_description') or generic unstructured list tags (`ul/li`) that lack distinct separate key and value child elements inside each item.

4. FIELD DEFINITIONS:
- "attributes.container": Common parent wrapper holding the complete list or table of specifications.
- "attributes.row": Selector matching each individual parameter row inside the container (e.g., "tr", ".attr-row", "dl > div").
- "attributes.key": Selector for the parameter name/label element inside the row (e.g., "td:first-child", ".attr-title", "dt").
- "attributes.value": Selector for the parameter value element inside the row (e.g., "td:last-child", ".attr-val", "dd"). Ensure "key" and "value" target DIFFERENT elements!

### OPERATIONAL RULES:
- MUST output valid W3C CSS selectors ONLY as JSON string values.
- PROHIBITED: Do NOT use jQuery selectors like ':contains()'.
- If no specification table exists, set all nested fields strictly to empty strings "".

### SELECTOR QUALITY RULES FOR PASS 2:
1. Target structural DOM relations relative to the container:
   - Tables: `tr` (row), `td:first-child` (key), `td:last-child` (value)
   - Definition lists: `dt` (key), `dd` (value)
   - Structured divs: `.attr-row` (row), `.attr-label` (key), `.attr-val` (value)

HTML SKELETON:
{cleaned_html}
"""


    async def clean_product_data(
        self, 
        sku: str, 
        parent_sku: str,
        brand_name: str, 
        model: str,
        manufacturer: str,
        country_of_origin: str,
        attributes: dict
    ) -> dict:
        """
        Микро-очистка скаляров, вычищение юридического мусора и нормализация ключей характеристик на английский snake_case.
        """
        input_payload = {
            "sku": sku or "",
            "parent_sku": parent_sku or "",
            "brand_name": brand_name or "",
            "model": model or "",
            "manufacturer": manufacturer or "",
            "country_of_origin": country_of_origin or "",
            "attributes": attributes or {}
        }

        prompt = f"""You are a strict e-commerce data cleaning and normalization assistant.
Clean and normalize the provided product JSON object.

INPUT JSON:
{json.dumps(input_payload, ensure_ascii=False, indent=2)}

### FEW-SHOT LEARNING EXAMPLE:
INPUT EXAMPLE:
{{
  "sku": "Код: 12345",
  "parent_sku": "",
  "brand_name": "Ноутбуки Asus",
  "model": "Модель: FA607",
  "manufacturer": "Asus Inc.",
  "country_of_origin": "Китай",
  "attributes": {{
    "Діагональ дисплея": "16\\"",
    "Країна-виробник": "Китай",
    "Обмін та повернення": "Обмін та повернення протягом 14 днів...",
    "Характеристики та комплектація": "Виробник залишає за собою право вносити зміни..."
  }}
}}

EXPECTED OUTPUT EXAMPLE:
{{
  "sku": "12345",
  "parent_sku": "",
  "brand_name": "Asus",
  "model": "FA607",
  "manufacturer": "Asus",
  "country_of_origin": "China",
  "attributes": {{
    "screen_size": "16\\""
  }}
}}

### STRICT CLEANING RULES:
1. "sku" & "parent_sku": Strip all text prefixes in ANY language ("Код:", "Code:", "Kod:", "Réf:"). Keep only raw alphanumeric code.
2. "brand_name" & "manufacturer": Strip category prefixes ("Ноутбуки Asus" -> "Asus").
3. "model": Strip label words ("Модель:", "Model:").
4. "attributes":
   - REMOVE DUPLICATES: If an attribute duplicates a top-level scalar ("country_of_origin", "brand_name", "sku", "manufacturer", "model"), DELETE IT from "attributes".
   - REMOVE GARBAGE & LEGAL NOTES: Strictly delete return policies, store terms, manufacturer modification disclaimers, warranty legal notes, and empty keys like "-".
   - NORMALIZE KEYS: Translate and standardize ALL attribute KEYS into short, concise English snake_case identifiers (e.g., "screen_size", "ram_capacity", "cpu", "gpu").

Return ONLY a valid JSON object matching the target schema."""

        try:
            res = await self._call_llm(prompt, step_name="MICRO CLEANER")
            if isinstance(res, dict):
                return res
        except Exception as e:
            logger.error(f"❌ [MICRO CLEANER ERR] Ошибка при микро-очистке: {e}")

        return input_payload

    async def clean_scalars_batch(self, scalars_payload: list[dict]) -> list[dict]:
        """
        Проход 1: Быстрая пакетная очистка легких скалярных полей через Ollama (пачками по 10 шт).
        """
        # Разбиваем большой батч на под-батчи по 10 штук для Ollama
        batch_size = 10
        all_results = []
        
        for i in range(0, len(scalars_payload), batch_size):
            chunk = scalars_payload[i:i + batch_size]
            prompt = f"""You are a strict e-commerce data cleaning assistant.
Clean and normalize the provided array of product scalar objects.

INPUT SCALARS BATCH:
{json.dumps({"products": chunk}, ensure_ascii=False, indent=2)}

### STRICT CLEANING RULES:
1. PROCESS ALL ITEMS: Maintain exact product_id values and return a "products" array with ALL input items.
2. "sku" & "parent_sku": Strip all label prefixes in ANY language ("Код:", "Code:", "Kod:", "Réf:"). Keep only raw alphanumeric code.
3. "brand_name" & "manufacturer": Strip category prefixes ("Ноутбуки Asus" -> "Asus").
4. "model": Strip label words ("Модель:", "Model:").
5. "currency": Strip interface noise and extra words (e.g., "₴КупитиПокупкачастинами" -> "₴", "грнКупить" -> "UAH"). Keep strictly a valid currency symbol or ISO code (e.g. "₴", "$", "€", "UAH", "USD", "EUR", "PLN").
6. "available": Convert availability status to strictly "yes" if the product is in stock / available for purchase, or "no" if out of stock / unavailable / discontinued.

### ⚠️ CRITICAL SEMANTIC CONSISTENCY CHECK (UNIVERSAL RULE):
Every field in your output MUST logically and semantically relate to the product described in the "title". 
If ANY extracted value (brand, model, country_of_origin, etc.) clearly belongs to a DIFFERENT product or context — for example, a laptop brand for a suitcase, a phone model for furniture, or an unrelated SKU — it is a scraping leak from a sidebar, footer, or "Recommended Products" block. 
In such cases, strictly set that inconsistent field to an empty string "".

### EXAMPLES OF SEMANTIC CHECK:
INPUT 1 (Brand leak):
{{
  "title": "ВАЛІЗА V&V TRAVEL FLASH LIGHT 2.0",
  "brand_name": "Asus",
  "country_of_origin": "China"
}}
EXPECTED OUTPUT 1:
{{
  "title": "ВАЛІЗА V&V TRAVEL FLASH LIGHT 2.0",
  "brand_name": "",
  "country_of_origin": "China"
}}

INPUT 2 (Model leak):
{{
  "title": "Дрель Bosch Professional",
  "model": "MacBook Pro 16"
}}
EXPECTED OUTPUT 2:
{{
  "title": "Дрель Bosch Professional",
  "model": ""
}}

Return ONLY a valid JSON object with key "products" containing the cleaned list."""

            res = await self._call_ollama(prompt, step_name=f"SCALARS BATCH {i//batch_size + 1}")
            if isinstance(res, dict) and "products" in res and isinstance(res["products"], list):
                all_results.extend(res["products"])
            else:
                logger.warning(f"⚠️ [OLLAMA SCALARS] Не удалось очистить батч {i//batch_size + 1}, возвращаем исходные данные.")
                all_results.extend(chunk)
        
        return all_results

    async def clean_attributes_mapping(self, raw_keys: list[str]) -> dict:
        """
        Проход 2: Очистка и нормализация списка ключей характеристик в словарь { "старый_ключ": "новый_ключ" }.
        """
        prompt = f"""You are a strict e-commerce data normalization assistant.
Normalize the provided array of raw attribute keys into a JSON dictionary mapping exact original keys to clean English snake_case identifiers.

INPUT ATTRIBUTE KEYS:
{json.dumps(raw_keys, ensure_ascii=False, indent=2)}

### FEW-SHOT LEARNING EXAMPLE:
INPUT EXAMPLE:
[
  "Діагональ дисплея",
  "Країна-виробник",
  "Обмін та повернення",
  "Характеристики та комплектація",
  "Бренд"
]

EXPECTED OUTPUT EXAMPLE:
{{
  "Діагональ дисплея": "screen_size",
  "Країна-виробник": "country_of_origin",
  "Обмін та повернення": "",
  "Характеристики та комплектація": "",
  "Бренд": "brand"
}}

### STRICT RULES:
1. Return ONLY a JSON object where each key is an EXACT raw input string from the input array, and the value is the normalized English snake_case key name.
2. REMOVE GARBAGE & LEGAL NOTES: If a key represents store terms, return policy, legal disclaimers, manufacturer modification disclaimers, or empty dashes ("-"), set its mapped value to an empty string "".
3. NORMALIZE: Translate and normalize all valid technical specification keys into short, concise English snake_case identifiers (e.g. "screen_size", "ram_capacity", "cpu", "gpu", "color", "weight", "material").

Return ONLY a valid JSON object dictionary mapping raw keys to normalized keys."""

        res = await self._call_ollama(prompt, step_name="ATTRIBUTES MAPPING CLEANER")
        if isinstance(res, dict):
            return res
        
        logger.error("❌ [ATTR MAPPING ERR] Не удалось получить словарь маппинга ключей от Ollama.")
        return {}

    def _build_prompt_scalars(self, cleaned_html: str, engine_name: str) -> str:
        """Промпт 1: Основные скалярные поля и системный Product ID."""
        example_schema = {
            "product_id_element": "CSS_SELECTOR (e.g. input[name='product_id'])",
            "product_id_attr": "ATTRIBUTE_NAME (e.g. value)",
            "title": "CSS_SELECTOR (e.g. h1.product-title)",
            "model": "CSS_SELECTOR or empty string",
            "sku": "CSS_SELECTOR (e.g. .sku-value)",
            "price_container": "CSS_SELECTOR for product price block/wrapper",
            "currency": "CSS_SELECTOR or empty string",
            "available": "CSS_SELECTOR (e.g. .stock-status)",
            "brand_name": "CSS_SELECTOR (e.g. .brand a)",
            "sales_notes": "CSS_SELECTOR or empty string"
        }

        cms_hint = ""
        if engine_name and engine_name.lower() != "general":
            cms_hint = (
                f"TARGET CMS PLATFORM: \"{engine_name}\". "
                f"Use typical {engine_name} e-commerce DOM patterns as structural guidance "
                f"(e.g., hidden inputs 'input[name=\"product_id\"]', buy buttons '#button-cart', price tags '.price-new', '.price-old').\n\n"
            )

        return f"""You are a senior front-end engineer specializing strictly in W3C CSS Selector extraction for web scrapers.

{cms_hint}CRITICAL TASK REQUIREMENT:
Analyze the provided cleaned HTML skeleton of a product page and return a valid JSON map where EVERY VALUE IS A W3C CSS SELECTOR STRING.

### STRICT WARNING — DO NOT EXTRACT TEXT OR DATA!
INCORRECT OUTPUT (DO NOT DO THIS):
{{
  "title": "Стеллаж для дров VOREL 100 см",
  "fact_price": 1890,
  "sku": "A100666"
}}

CORRECT OUTPUT (DO THIS ONLY):
{{
  "product_id_element": "input[name='product_id']",
  "product_id_attr": "value",
  "title": ".product-title h1",
  "model": "",
  "sku": ".sku-code span",
  "price_container": ".price-box",
  "currency": ".currency-symbol",
  "available": ".stock-status",
  "brand_name": ".brand-link",
  "sales_notes": ""
}}

### TARGET JSON SCHEMA KEYS TO USE (DO NOT CHANGE KEY NAMES!):
{json.dumps(example_schema, indent=2)}

### FIELD GUIDANCE:
1. "product_id_element": CSS selector targeting the exact element containing the numeric database Product ID (e.g. "input[name='product_id']", "button[data-product-id]", "#button-cart").
2. "product_id_attr": Attribute name storing the numeric ID on that element (e.g. "value", "data-product-id", "data-id"). Set to "text" if ID is inside tag text.
3. "title": CSS selector for product main title heading (typically 'h1').
4. "model": CSS selector for model name or series.
5. "sku": CSS selector for vendor article code or item SKU.
6. "price_container": CSS selector targeting the price block/wrapper containing price(s).
7. "currency": CSS selector targeting currency code element.
8. "available": CSS selector targeting the GENERAL CONTAINER or WRAPPER that holds stock availability information (e.g., `.stock-wrapper`, `.product-availability`, `[data-availability-block]`). DO NOT target specific status classes like `.in-stock` or `.out-of-stock` directly, as they may change dynamically. Target the parent container that wraps all availability states.
9. "brand_name": CSS selector targeting manufacturer or brand link/text.
10. "sales_notes": CSS selector targeting delivery notes or minimum order terms.

### OPERATIONAL RULES:
- MUST output valid W3C CSS selectors ONLY as JSON string values.
- DO NOT invent new keys like "item_name", "item_price", "trust_reasons", or "content". Use EXACTLY the schema keys above.
- NEVER put actual extracted text, titles, numbers, or prices in the values.
- PROHIBITED: Do NOT use jQuery selectors like ':contains()'.
- If a field is missing in the HTML, set its value strictly to "".

HTML SKELETON:
{cleaned_html}
"""

    def _build_prompt_media(self, cleaned_html: str, engine_name: str) -> str:
        """Промпт 2: Медиафайлы, главное фото и галерея."""
        template = {
            "main_image_element": "",
            "main_image_attr": "",
            "gallery_item_element": "",
            "gallery_item_attr": ""
        }
        return f"""You are an expert CSS Selector extractor for e-commerce product media.
CMS PLATFORM ENGINE: "{engine_name}"

FIELD DEFINITIONS:
- "main_image_element": CSS selector targeting the primary main product photo (`img` or parent `a`).
- "main_image_attr": Attribute holding the image URL (e.g. "src", "data-src", "data-lazy", "data-zoom-image", "href").
- "gallery_item_element": CSS selector matching ALL product thumbnail or gallery image elements.
- "gallery_item_attr": Attribute holding image URLs in gallery items (e.g. "src", "data-src", "href").

RULES:
1. Look for high-res image attributes (`data-src`, `data-lazy`, `href`) in addition to standard `src`.
2. STRICTLY PROHIBITED to use jQuery pseudo-classes like ':contains()'.
3. Return ONLY a valid JSON object matching the schema below.

TARGET SCHEMA:
{json.dumps(template, indent=2)}

HTML SKELETON:
{cleaned_html}
"""

    def _build_prompt_attributes(self, cleaned_html: str, engine_name: str) -> str:
        """Промпт 3: Таблица характеристик и спецификаций."""
        template = {
            "attributes": {
                "container": "",
                "row": "",
                "key": "",
                "value": ""
            },
            "description": "",
        }
        return f"""You are an expert CSS Selector extractor for product specifications and technical attribute tables.
CMS PLATFORM ENGINE: "{engine_name}"

FIELD DEFINITIONS:
- "attributes.container": Table, wrapper, or list holding technical specifications.
- "attributes.row": Single specification row inside container (e.g., "tr", ".feature-row", "dl > div").
- "attributes.key": Name/label element of specification inside row (e.g., "td:first-child", ".label", "dt").
- "attributes.value": Value element of specification inside row (e.g., "td:last-child", ".val", "dd").
- "description": Main product description text container/tab.

RULES:
1. TARGET ONLY full technical specification tables or specification list panels (typically inside tab panels like '#product_attributes', '#tab-specification', or dedicated spec wrappers).
2. STRICTLY PROHIBITED: DO NOT use top header summary blocks (e.g., '.rm-product-center-info', '.product-meta') that only hold basic SKU, Brand, Rating/Reviews, or Stock badges.
3. EXCLUDE tab navigation buttons/links (e.g., 'a.nav-link', '.nav-tabs', '.nav-item'). Target the content container inside the tab panel.
4. "attributes.key" & "attributes.value": Ensure selectors strictly separate the label (key) and value element inside each specification row.
5. Return ONLY a valid JSON object matching the schema below.

TARGET SCHEMA:
{json.dumps(template, indent=2)}

HTML SKELETON:
{cleaned_html}
"""

    def _build_prompt_variants(self, cleaned_html: str, engine_name: str) -> str:
        """Промпт 4: Варианты и интерактивные модификации товаров (Универсальный)."""
        template = {
            "variant_selectors": {
                "<actual_group_name>": {
                    "container": "CSS_SELECTOR for the wrapper of this option group",
                    "item": "CSS_SELECTOR for individual option elements",
                    "value_attr": "ATTRIBUTE_NAME holding the option value (or '' if text)"
                }
            }
        }
        return f"""You are an expert CSS Selector extractor for product options and variants.
CMS PLATFORM ENGINE: "{engine_name}"

RULES:
1. Dynamically detect the ACTUAL names of the option groups on the page (e.g., "color", "size", "memory", "volume", "capacity"). Use these exact names as the KEYS in the "variant_selectors" dictionary.
2. DO NOT hardcode "color" or "size" if the product uses different terms.
3. For each group, provide "container", "item", and "value_attr".
4. If no variant options exist on the page, return {{"variant_selectors": {{}}}}.
5. Return ONLY a valid JSON object matching the schema below.

TARGET SCHEMA:
{json.dumps(template, indent=2)}

HTML SKELETON:
{cleaned_html}
"""

    async def generate_selectors(self, cleaned_html: str, engine_name: str = "general") -> Dict[str, Any]:
        """Генерирует карту селекторов: 1 единый запрос для Gemini или 4 пошаговых для Ollama."""
        
        if not cleaned_html or len(cleaned_html) < 200:
            logger.error(
                f"💥 [SELECTOR ENGINE ABORT] Отмена вызова LLM ({self.provider.upper()})! "
                f"Передан пустой или непригодно малый HTML ({len(cleaned_html) if cleaned_html else 0} симв.).\n"
                f"   📄 [DUMP]: '{cleaned_html}'"
            )
            return {}

        logger.info(f"🤖 [SELECTOR ENGINE] Генерация карты селекторов (Engine: '{engine_name}', Provider: '{self.provider.upper()}')")

        # 2-ПРОХОДНАЯ СХЕМА ДЛЯ GEMINI
        if self.provider == "gemini":
            merged_map = {}
            
            # Страховочное усечение, если HTML все еще аномально большой (> 80КБ)
            # if len(cleaned_html) > 80000:
            #     logger.warning(f"⚠️ [GEMINI] HTML-скелет слишком большой ({len(cleaned_html)} симв.). Усекаем до 80KB...")
            #     cleaned_html = cleaned_html[:80000]

            logger.info(f"  🧠 [GEMINI 2-PASS] [1/2] Запрос скаляров и медиа (Объем HTML: {len(cleaned_html)} симв.)...")
            p1 = self._build_prompt_pass1(cleaned_html, engine_name)
            res1 = await self._call_gemini(p1, step_name="PASS 1 (SCALARS & MEDIA & VARIANTS)")
            # res1 = await self._call_ollama(p1, step_name="PASS 1 (SCALARS & MEDIA & VARIANTS)")
            if isinstance(res1, dict):
                merged_map.update(res1)

            logger.info("  📊 [GEMINI 2-PASS] [2/2] Фокусированный запрос таблицы характеристик...")
            p2 = self._build_prompt_pass2(cleaned_html, engine_name)
            res2 = await self._call_gemini(p2, step_name="PASS 2 (ATTRIBUTES & SPECS)")
            # res2 = await self._call_ollama(p2, step_name="PASS 2 (ATTRIBUTES & SPECS)")
            if isinstance(res2, dict):
                merged_map.update(res2)

            # Карта считается валидной ТОЛЬКО если успешно прошёл Pass 1 (есть хотя бы заголовок или цена)
            has_scalars = bool(merged_map.get("title") or merged_map.get("price_container") or merged_map.get("sku"))

            if has_scalars:
                # logger.info("✅ [GEMINI SELECTOR ENGINE] Карта CSS-селекторов успешно создана за 2 прохода!")
                return merged_map

            logger.error("❌ [GEMINI SELECTOR ENGINE] Pass 1 не принес базовые скаляры (title/price). Карта отклонена!")
            return {}

        merged_map = {}

        # 1. Промпт 1: Скаляры и Product ID
        logger.info("  🧠 [1/4] Поиск базовых полей и системного Product ID...")
        p1 = self._build_prompt_scalars(cleaned_html, engine_name)
        res1 = await self._call_llm(p1, step_name="1/4 SCALARS & PID")
        merged_map.update(res1)

        # 2. Промпт 2: Медиа и Галерея
        logger.info("  🖼️ [2/4] Поиск селекторов изображений и галереи...")
        p2 = self._build_prompt_media(cleaned_html, engine_name)
        res2 = await self._call_llm(p2, step_name="2/4 MEDIA & GALLERY")
        merged_map.update(res2)

        # 3. Промпт 3: Характеристики
        logger.info("  📊 [3/4] Поиск таблицы спецификаций и описания...")
        p3 = self._build_prompt_attributes(cleaned_html, engine_name)
        res3 = await self._call_llm(p3, step_name="3/4 ATTRIBUTES & DESC")
        merged_map.update(res3)

        # 4. Промпт 4: Варианты и Модификации
        logger.info("  🔹 [4/4] Поиск блоков вариантов и опций...")
        p4 = self._build_prompt_variants(cleaned_html, engine_name)
        res4 = await self._call_llm(p4, step_name="4/4 VARIANTS")
        merged_map.update(res4)

        has_any_selectors = any(v for k, v in merged_map.items() if v and v != {})

        if has_any_selectors:
            logger.info("✅ [OLLAMA SELECTOR ENGINE] Карта CSS-селекторов из 4 промптов успешно создана!")
            return merged_map

        logger.error("❌ [OLLAMA SELECTOR ENGINE] Не удалось получить валидные селекторы от Ollama.")
        return {}