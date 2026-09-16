import json
import logging
import re
from typing import Any, List, Dict, Optional
from bs4 import BeautifulSoup, Comment, Tag
import httpx

from core.models import ProductParent, ProductVariant

logger = logging.getLogger("ChittaParser")


class OllamaEnricher:
    """Универсальное AI-обогащение данных через Markdown-каскад без HTML-мусора."""

    INVALID_VALUES = {
        "не указано", "невідомо", "нет данных", "відсутній", "відсутня",
        "unknown", "n/a", "none", "null", "-", "—", "undefined"
    }

    def __init__(
        self,
        model_name: str = "qwen2.5-coder:3b",
        host: str = "http://localhost:11434",
        timeout: int = 180,
    ):
        self.model_name = model_name
        self.host = host
        self.timeout = timeout

    @classmethod
    def _element_to_markdown(cls, element: Tag) -> str:
        """Преобразует DOM-элемент в чистый Markdown без потери текста."""
        if not element:
            return ""

        soup_copy = BeautifulSoup(str(element), "html.parser")

        for h in soup_copy.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
            level = int(h.name[1])
            htext = h.get_text(" ", strip=True)
            if htext:
                h.replace_with(f"\n\n{'#' * level} {htext}\n\n")

        for tr in soup_copy.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["th", "td"]) if td.get_text(strip=True)]
            if len(cells) >= 2:
                tr.replace_with(f"\n* {cells[0]}: {': '.join(cells[1:])}\n")
            elif len(cells) == 1:
                tr.replace_with(f"\n* {cells[0]}\n")

        for li in soup_copy.find_all("li"):
            litext = li.get_text(" ", strip=True)
            if litext:
                li.replace_with(f"\n- {litext}\n")

        raw_text = soup_copy.get_text(separator="\n", strip=True)
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        return "\n".join(lines)

    @classmethod
    def extract_markdown_from_html(cls, html: str, parent: Optional[ProductParent] = None) -> str:
        """Формирует Markdown из HTML-страницы с очисткой от шума."""
        if not html:
            return ""

        soup = BeautifulSoup(html, "html.parser")
        total_page_len = len(soup.get_text(strip=True))

        noise_selectors = [
            "script", "style", "svg", "noscript", "iframe", "header", "footer", "nav", "aside", "head",
            '[class*="mobile-menu" i]', '[class*="main-menu" i]', '[class*="navbar" i]',
            '[class*="cookie" i]', '.swiper', '.slick-slider', '.owl-carousel',
            '[class*="carousel" i]', '[class*="slider" i]',
            '[class*="recommend" i]', '[class*="similar" i]', '[class*="related" i]',
            '[class*="minicart" i]', '[class*="cart-popup" i]',
            'div.modal-dialog', 'div.popup-content', '.modal-wrapper',
            '[class*="size-guide" i]', '[class*="size-chart" i]', '[id*="size-modal" i]',
            '[class*="review" i]', '[class*="delivery" i]', '[class*="comments" i]',
            'select[class*="variant" i]', '.swatches', '[data-qaid="product_options"]'
        ]

        for sel in noise_selectors:
            for el in soup.select(sel):
                if el.find("h1"):
                    continue
                el_len = len(el.get_text(strip=True))
                if total_page_len > 0 and (el_len / total_page_len) > 0.4:
                    continue
                el.decompose()

        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        target = None

        if parent and parent.product_id:
            pid = str(parent.product_id).strip()
            # Ищем строго по продуктовым атрибутам или целевым input
            queries = [
                f'[data-product-id="{pid}"]', 
                f'[data-id="{pid}"]', 
                f'input[name*="product"][value="{pid}"]',
                f'input[name="id"][value="{pid}"]'
            ]
            body_text_len = len(soup.body.get_text(strip=True)) if soup.body else 0

            for query in queries:
                found_el = soup.select_one(query)
                if found_el:
                    # Разрешаем любой родительский контейнер (включая div)
                    container = found_el.find_parent(["main", "article", "section", "form", "div"])
                    if container:
                        container_len = len(container.get_text(strip=True))
                        # Контейнер валиден, только если содержит не менее 25% контента страницы
                        if body_text_len > 0 and (container_len / body_text_len) >= 0.25:
                            target = container
                            break

        if not target:
            h1 = soup.find("h1")
            if h1:
                # 1. Сначала ищем крупный семантический контейнер (без мелких div)
                container = h1.find_parent(["main", "article", "section"])
                if container and len(container.get_text(strip=True)) > 300:
                    target = container
                else:
                    # 2. Если вокруг h1 только div, поднимаемся по родителям, пока не найдем контейнер с богатым контентом (> 500 симв)
                    curr = h1.parent
                    while curr and curr.name != "body":
                        if len(curr.get_text(strip=True)) > 500:
                            target = curr
                            break
                        curr = curr.parent

        if not target:
            target = soup.find("main") or soup.find("article") or soup.body or soup        
            
        markdown_text = cls._element_to_markdown(target)

        if len(markdown_text) < 150 and soup.body and target != soup.body:
            logger.warning("⚠️ [OLLAMA CONTEXT] Выбранный контейнер мал. Откат на <body>")
            markdown_text = cls._element_to_markdown(soup.body)

        markdown_text = markdown_text.replace('\xa0', ' ')
        markdown_text = re.sub(r'(\d+)\s+(\d{3})(?=[,\.]|\s*(?:грн|₴|\$|€|eur|usd))', r'\1\2', markdown_text)
        markdown_text = re.sub(r'(\d+),(\d{2})(?=\s*(?:грн|₴|\$|€|eur|usd))', r'\1.\2', markdown_text)

        return markdown_text

    def _clean_field_value(self, val: Any) -> str:
        if not val:
            return ""
        clean_str = str(val).strip()
        if clean_str.lower() in self.INVALID_VALUES:
            return ""
        return clean_str

    async def _call_ollama_raw(self, prompt: str) -> str:
        """Вызов Ollama API с возвратом сырой строки JSON."""
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "num_ctx": 32768,
                "num_predict": 1024,
                "temperature": 0.0,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=float(self.timeout)) as client:
                response = await client.post(f"{self.host}/api/generate", json=payload)
                if response.status_code == 200:
                    res_text = response.json().get("response", "{}")
                    return res_text.replace("```json", "").replace("```", "").strip()
                else:
                    logger.warning(f"⚠️ [OLLAMA ERROR] API вернул HTTP код {response.status_code}")
        except Exception as e:
            logger.warning(f"⚠️ [OLLAMA CALL FAILED]: {e}")
        return "{}"

    async def generate(self, prompt: str) -> Dict[str, Any]:
        """Публичный метод для вызова LLM с возвратом распарсенного JSON."""
        raw_json = await self._call_ollama_raw(prompt)
        if not raw_json:
            return {}
        try:
            return json.loads(raw_json)
        except json.JSONDecodeError:
            logger.error(f"❌ [OLLAMA GENERATE] Ошибка парсинга JSON ответа!")
            return {}

    def _normalize_extracted_attributes(self, data: dict) -> Dict[str, str]:
        """Распаковывает ответ Ollama в плоский словарь атрибутов."""
        if not isinstance(data, dict):
            return {}

        if "delta_attributes" in data and isinstance(data["delta_attributes"], dict):
            return {str(k): str(v) for k, v in data["delta_attributes"].items()}

        if "attributes" in data and isinstance(data["attributes"], dict):
            return {str(k): str(v) for k, v in data["attributes"].items()}

        reserved_meta_keys = {
            "old_price", "brand_name", "manufacturer", "country_of_origin",
            "sales_notes", "add_description", "instructs", "bonus", "title"
        }
        return {
            str(k): str(v)
            for k, v in data.items()
            if k not in reserved_meta_keys and not isinstance(v, (dict, list))
        }

    def _build_variant_delta_prompt(
        self, variant_title: str, base_attributes: dict, variant_md: str
    ) -> str:
        """Формирует промпт поиска изменений (Delta Extraction)."""
        base_json = json.dumps(base_attributes, ensure_ascii=False, indent=2)

        return f"""You are a strict product specification delta analyzer for e-commerce.

BASE PRODUCT ATTRIBUTES:
{base_json}

TARGET VARIANT TITLE: "{variant_title}"

TARGET VARIANT MARKDOWN SNIPPET:
{variant_md}

TASK:
Analyze TARGET VARIANT MARKDOWN SNIPPET specifically for the product variant named "{variant_title}".
Compare it against BASE PRODUCT ATTRIBUTES and extract ONLY the attribute values that CHANGE or are DIFFERENT for this target variant (e.g. size, specific length, specific color, volume).

STRICT RULES:
1. Return ONLY a valid JSON object with a single root key "delta_attributes".
2. Include in "delta_attributes" ONLY the key-value pairs that change for "{variant_title}".
3. Do NOT include unchanged static specs (e.g., material, fabric composition, care instructions).
4. Extract single atomic values matching "{variant_title}". Never extract option lists.
5. If no attributes differ from BASE PRODUCT ATTRIBUTES, return {{"delta_attributes": {{}}}}.

EXAMPLE OUTPUT:
{{
  "delta_attributes": {{
    "Розмір": "L (50-52)",
    "Довжина": "120 см"
  }}
}}
"""

    async def process_variants_attributes(
        self, 
        parent: ProductParent, 
        parent_md: str, 
        variant_mds: Optional[Dict[str, str]] = None
    ) -> None:
        """Синхронизирует атрибуты вариантов через поиск дельты на изолированных Markdown-страницах.
        
        ИЗМЕНЕНИЕ: Теперь метод сначала агрегирует все raw_attributes из родителя и вариантов,
        затем отправляет их в LLM для нормализации и классификации на статические/модификации.
        """
        if not parent.variants:
            return

        variant_mds = variant_mds or {}

        # Агрегация всех сырых атрибутов (raw_attributes) из родителя и всех вариантов
        all_raw_attrs = []
        if parent.raw_attributes:
            all_raw_attrs.extend(parent.raw_attributes)
        for v in parent.variants:
            if v.raw_attributes:
                all_raw_attrs.extend(v.raw_attributes)

        # Если есть сырые данные, используем их для создания base_attributes через эвристику или LLM
        # Пока используем старый метод как фолбэк, но в будущем здесь будет вызов LLM для нормализации
        if all_raw_attrs:
            # Простая эвристика: объединяем все найденные пары name:value
            # В будущем здесь будет более сложная логика с LLM
            base_attributes = {}
            for item in all_raw_attrs:
                if isinstance(item, dict):
                    for k, v in item.items():
                        if k and v and k.lower().strip() != "option":
                            base_attributes[str(k).strip()] = str(v).strip()
        else:
            # Фолбэк на старые attributes, если raw_attributes пусты
            base_attributes = {
                k: v for k, v in parent.attributes.items() 
                if k.lower().strip() != "option"
            }

        logger.info(
            f"🔄 [VARIANT DELTA EXTRACTION] Старт обработки {len(parent.variants)} вариантов. "
            f"Агрегировано {len(all_raw_attrs)} сырых атрибутов. Базовая матрица ({len(base_attributes)} ключей)."
        )

        for idx, variant in enumerate(parent.variants, 1):
            # Извлекаем изолированный Markdown этого конкретного варианта (или фолбэк на основной)
            v_md = variant_mds.get(str(variant.product_id), parent_md)

            prompt = self._build_variant_delta_prompt(
                variant_title=variant.title,
                base_attributes=base_attributes,
                variant_md=v_md
            )

            logger.info(
                f"\n==================== [OLLAMA DELTA PROMPT #{idx}] ====================\n"
                f"Target Title: {variant.title}\n"
                f"Markdown Len: {len(v_md)} chars\n"
                f"=========================================================================="
            )

            raw_res = await self._call_ollama_raw(prompt)
            logger.info(
                f"\n==================== [OLLAMA RAW DELTA RESPONSE #{idx}] ====================\n"
                f"{raw_res}\n"
                f"=========================================================================="
            )

            data = {}
            if raw_res:
                try:
                    data = json.loads(raw_res)
                except json.JSONDecodeError:
                    logger.error(f"❌ [OLLAMA ERROR] Ошибка парсинга JSON ответа дельты на варианте #{idx}!")

            delta_attrs = self._normalize_extracted_attributes(data)

            # Статические атрибуты сохраняем в attributes варианта
            variant.attributes = dict(base_attributes)

            # Накладываем дельту модификаций строго в modification_attributes
            for d_key, d_val in delta_attrs.items():
                clean_k = self._clean_field_value(d_key).strip()
                clean_v = self._clean_field_value(d_val).strip()

                if not clean_k or not clean_v or clean_k.lower() == "option":
                    continue

                variant.modification_attributes[clean_k] = clean_v
                
                # Регистрируем ключ модификации у родителя (беря значение строго из родительских attributes, если оно там есть)
                if clean_k not in parent.modification_attributes or not parent.modification_attributes[clean_k]:
                    parent.modification_attributes[clean_k] = parent.attributes.get(clean_k, "")

            # СХЛОПЫВАНИЕ: Если Ollama успешно нашла именованные модификации (например, 'Колір'), 
            # отбрасываем фолбэк-ключ 'option', чтобы избежать дублирования
            named_keys = [k for k in variant.modification_attributes.keys() if k.lower() != "option"]
            if named_keys and "option" in variant.modification_attributes:
                opt_val = variant.modification_attributes["option"]
                # Если значение option содержится в любом из именованных атрибутов, удаляем option
                if any(opt_val.lower() in str(variant.modification_attributes[nk]).lower() for nk in named_keys):
                    variant.modification_attributes.pop("option", None)
                    if "option" in parent.modification_attributes and not any("option" in v.modification_attributes for v in parent.variants):
                        parent.modification_attributes.pop("option", None)

            logger.info(f"✅ [VARIANT DELTA RESULT #{idx}] '{variant.title}' (ID: {variant.product_id}): modification_attributes = {variant.modification_attributes}")

    async def enrich_product(
        self, 
        html_content: str, 
        parent: ProductParent, 
        variant_html_map: Optional[Dict[str, str]] = None
    ) -> ProductParent:
        """Главный метод обогащения родителя и синхронизации вариантов."""
        full_md = self.extract_markdown_from_html(html_content, parent=parent)

        if not full_md:
            logger.warning("⚠️ [OLLAMA] Не удалось сформировать Markdown-контекст товара.")
            return parent

        # Преобразуем HTML изолированных страниц вариантов в Markdown (если передана карта variant_html_map)
        variant_mds: Dict[str, str] = {}
        if variant_html_map:
            for v_pid, v_html in variant_html_map.items():
                v_md = self.extract_markdown_from_html(v_html, parent=None)
                if v_md:
                    variant_mds[str(v_pid)] = v_md

        CHUNK_SIZE = 12000
        OVERLAP = 500
        chunks: List[str] = []
        start = 0

        while start < len(full_md):
            end = start + CHUNK_SIZE
            chunks.append(full_md[start:end])
            if end >= len(full_md):
                break
            start = end - OVERLAP

        logger.info(f"🤖 [OLLAMA CALL] Анализ товара: '{parent.title}' | Всего Markdown: {len(full_md)} симв. | Чанков: {len(chunks)}")

        for idx, chunk_text in enumerate(chunks, 1):
            # logger.info(f"\n==================== [OLLAMA MARKDOWN CHUNK {idx}/{len(chunks)}] ====================\n{chunk_text}\n=================================================================")

            existing_desc_snippet = self._clean_field_value(parent.description[:500]) if parent.description else ""

            prompt = f"""You are a strict e-commerce data extraction engine for Vector Search (RAG).
Extract ONLY dense, high-signal factual information belonging STRICTLY to the target product.

Target Product Title: "{parent.title}"
Target Current Retail Price: {parent.fact_price}
Existing Main Description: "{existing_desc_snippet}"

UNIVERSAL VARIANT MATCHING RULE:
- Target Product Title identifies the exact variant.
- If a specification line lists multiple option choices (e.g. colors, sizes), extract ONLY the single value matching Target Product Title into "attributes".
- NEVER extract comma-separated option lists into "attributes".

STRICT EXTRACTION RULES:
1. Extract values into a JSON object with exact keys specified below in STRICT ORIGINAL LANGUAGE.
2. NO HALLUCINATIONS: Do NOT invent data. Do NOT extract seller, shop, or marketplace names as "brand_name". Return "" or 0.0 or {{}} if missing.
3. "old_price": Explicitly written original price belonging ONLY to "{parent.title}" that is HIGHER than {parent.fact_price}. Return float or 0.0.
4. "brand_name": Product manufacturing brand or trademark. Return "" if missing or if it is a store/seller name.
5. "manufacturer": Producing company name. Return "" if missing.
6. "country_of_origin": Country of origin/manufacture. Return "" if missing.
7. "sales_notes": Wholesale conditions, bulk discounts, or payment notes. Return "" if missing.
8. "add_description": Extract ONLY NEW additional technical/material details or general features NOT mentioned in "Existing Main Description".
   - CRITICAL: STRICTLY FORBIDDEN to include sizes (e.g. S, M, L, XL), specific colors, article numbers, or variant names here!
   - Must contain ONLY generic, universal product features valid for all variants (e.g. material properties, care, general usage).
   - Return "" if no new generic information is available or if it repeats Existing Main Description.
9. "instructs": Application steps, usage instructions, care/washing rules, or dosage guidelines. Max 80 words. Return "" if missing.
10. "bonus": Promotional badge or discount text (e.g., "-30%"). Return "" if missing.
11. "attributes": JSON object of key-value pairs for ALL static product specifications found in the text.
    - Key MUST be a short label in original language (1-4 words max).
    - VALUE MUST BE ATOMIC: Extract ONLY the single option matching "{parent.title}".

EXAMPLE (Pattern to follow):
Title: "Халат жіночий L, Сірий"
Existing Main Description: "М’який і теплий халат з мікрофібри Велсофт."
Snippet: "Код: 123. Ціна: 809 грн. Стара ціна: 1155 грн. Розміри: M, L, XL. Колір: Сірий, Синій. Тканина: 100% поліестер. Догляд: машинне прання 40°C. Продавець: HUGO."
JSON Output:
{{
  "old_price": 1155.0,
  "brand_name": "",
  "manufacturer": "",
  "country_of_origin": "",
  "sales_notes": "",
  "add_description": "...",
  "instructs": "",
  "bonus": "",
  "attributes": {{
    "Розмір": "L",
    "Колір": "Сірий",
    "Матеріал": "100% поліестер"
  }}
}}

PRODUCT MARKDOWN SNIPPET:
{chunk_text}
"""
            raw_res = await self._call_ollama_raw(prompt)
            logger.info(f"\n==================== [OLLAMA RAW RESPONSE CHUNK {idx}] ====================\n{raw_res}\n==========================================================================")

            data = {}
            if raw_res:
                try:
                    data = json.loads(raw_res)
                except json.JSONDecodeError:
                    logger.error(f"❌ [OLLAMA ERROR] Ошибка парсинга JSON ответа на чанке {idx}!")

            if data and isinstance(data, dict):
                fields_status = []

                # 1. Старая цена
                try:
                    raw_old = float(data.get("old_price") or 0.0)
                except (ValueError, TypeError):
                    raw_old = 0.0

                if raw_old > parent.fact_price:
                    if parent.price <= parent.fact_price:
                        parent.price = raw_old
                        fields_status.append(f"Старая цена: {parent.price}")
                    else:
                        fields_status.append(f"Старая цена уже есть ({parent.price})")
                elif raw_old > 0:
                    logger.info(f"⚠️ [OLLAMA IGNORED] Отклонена невалидная цена {raw_old}: она меньше или равна розничной ({parent.fact_price})")
                    fields_status.append("Старая цена: отклонена")
                else:
                    fields_status.append("Старая цена: —")

                # 2. Мета-поля
                v_brand = self._clean_field_value(data.get("brand_name"))
                if v_brand and not parent.brand_name: parent.brand_name = v_brand
                fields_status.append(f"Бренд: '{parent.brand_name or '—'}'")

                v_mfg = self._clean_field_value(data.get("manufacturer"))
                if v_mfg and not parent.manufacturer: parent.manufacturer = v_mfg
                fields_status.append(f"Производитель: '{parent.manufacturer or '—'}'")

                v_country = self._clean_field_value(data.get("country_of_origin"))
                if v_country and not parent.country_of_origin: parent.country_of_origin = v_country
                fields_status.append(f"Страна: '{parent.country_of_origin or '—'}'")

                v_sales = self._clean_field_value(data.get("sales_notes"))
                if v_sales and not parent.sales_notes: parent.sales_notes = v_sales
                fields_status.append(f"Sales notes: '{parent.sales_notes or '—'}'")

                v_add_desc = self._clean_field_value(data.get("add_description"))
                if v_add_desc and not parent.add_description: parent.add_description = v_add_desc
                fields_status.append(f"AddDesc: {len(parent.add_description)} симв.")

                v_instructs = self._clean_field_value(data.get("instructs"))
                if v_instructs and not parent.instructs: parent.instructs = v_instructs
                fields_status.append(f"Инструкция: {len(parent.instructs)} симв.")

                v_bonus = self._clean_field_value(data.get("bonus"))
                if v_bonus and not parent.bonus: parent.bonus = v_bonus
                fields_status.append(f"Бонус: '{parent.bonus or '—'}'")

                # 3. Базовые характеристики родителя
                attrs_val = data.get("attributes")
                if isinstance(attrs_val, dict):
                    added_count = 0
                    for attr_k, attr_v in attrs_val.items():
                        clean_k = self._clean_field_value(attr_k).strip()
                        clean_v = self._clean_field_value(attr_v).strip()

                        if (
                            clean_k 
                            and clean_v 
                            and clean_k.lower() != "option"
                            and len(clean_k) <= 50 
                            and "\n" not in clean_k 
                            and clean_k not in parent.attributes
                        ):
                            parent.attributes[clean_k] = clean_v
                            added_count += 1

                    if added_count > 0:
                        fields_status.append(f"Характеристики: +{added_count}")

                logger.info(f"📊 [OLLAMA RESULT CHUNK {idx}] " + " | ".join(fields_status))

            needs_more = (
                parent.price <= parent.fact_price or
                not parent.brand_name or
                not parent.manufacturer or
                not parent.country_of_origin or
                not parent.sales_notes or
                not parent.bonus or
                not parent.add_description
            )

            if not needs_more:
                logger.info(f"✅ [OLLAMA CASCADE] Все обязательные поля успешно собраны на чанке {idx}/{len(chunks)}.")
                break
            elif idx < len(chunks):
                logger.info(f"🔄 [OLLAMA CASCADE] Не все поля найдены. Запуск следующего чанка {idx+1}/{len(chunks)}...")

        # Вызов синхронизации вариантов через Delta Extraction
        # if parent.variants:
        #     await self.process_variants_attributes(parent, parent_md=full_md, variant_mds=variant_mds)
        # Вызов синхронизации вариантов через Delta Extraction
        if parent.variants:
            await self.process_variants_attributes(parent, parent_md=full_md, variant_mds=variant_mds)
            # Очищаем родительские attributes от ключей, ставших модификациями
            parent.sanitize_modification_attributes()
        
        # Наследование общих полей
        for variant in parent.variants:
            if variant.price <= variant.fact_price and parent.price > parent.fact_price:
                variant.price = parent.price
            if not variant.brand_name: variant.brand_name = parent.brand_name
            if not variant.manufacturer: variant.manufacturer = parent.manufacturer
            if not variant.country_of_origin: variant.country_of_origin = parent.country_of_origin
            if not variant.sales_notes: variant.sales_notes = parent.sales_notes
            if not variant.add_description: variant.add_description = parent.add_description
            if not variant.instructs: variant.instructs = parent.instructs
            if not variant.bonus: variant.bonus = parent.bonus

        return parent

    async def normalize_availability_batch(self, raw_statuses: set[str]) -> dict[str, str]:
        """Принимает набор уникальных сырых строк наличия и возвращает JSON-карту маппинга в 'yes'/'no'."""
        if not raw_statuses:
            return {}

        statuses_list = [s for s in raw_statuses if s and str(s).strip()]
        if not statuses_list:
            return {}

        prompt = f"""You are a data cleaning assistant for an e-commerce catalog.
Analyze the following availability status strings and classify EACH of them strictly as "yes" (in stock, available, pre-order) or "no" (out of stock, unavailable, discontinued, sold out).

Input statuses:
{json.dumps(statuses_list, ensure_ascii=False, indent=2)}

Return ONLY a valid JSON object mapping each exact input string to either "yes" or "no".
Do not add any explanations or markdown wrappers outside the JSON.
Example format:
{{
  "InStock": "yes",
  "Out of stock": "no",
  "Є в наявності": "yes"
}}"""

        try:
            resp_text = await self._call_ollama_raw(prompt)
            
            # Очистка от возможных markdown-тегов ```json ... ```
            clean_json = re.sub(r'^```(?:json)?\s*', '', resp_text, flags=re.MULTILINE)
            clean_json = re.sub(r'\s*```$', '', clean_json, flags=re.MULTILINE).strip()
            
            mapping = json.loads(clean_json)
            if isinstance(mapping, dict):
                return {k: ("yes" if str(v).lower().strip() in ["yes", "true", "1", "instock"] else "no") for k, v in mapping.items()}
        except Exception as e:
            logger.warning(f"⚠️ [OLLAMA AVAILABILITY WARN] Ошибка батч-нормализации наличия через Ollama: {e}")

        # Резервный фолбэк, если Ollama недоступна
        fallback_map = {}
        for st in statuses_list:
            st_lower = st.lower()
            if any(kw in st_lower for kw in ["instock", "yes", "true", "наявності", "наличии", "в наличии"]):
                fallback_map[st] = "yes"
            else:
                fallback_map[st] = "no"
        return fallback_map        

    async def rename_anonymous_options_batch(self, groups: Dict[str, List[str]]) -> Dict[str, str]:
        """Принимает словарь {group_id: [список_значений]} и возвращает словарь {group_id: новое_понятное_имя_опции}."""
        if not groups:
            return {}

        prompt = f"""You are an e-commerce taxonomy expert.
Analyze the following groups of product option values and infer a short, accurate attribute label for EACH group in the relevant language (e.g., "Розмір", "Колір", "Об'єм пам'яті", "Матеріал", "Volume", "Color", "Size").

Groups:
{json.dumps(groups, ensure_ascii=False, indent=2)}

STRICT RULES:
1. Return ONLY a valid JSON object mapping each group_id to a single short attribute label.
2. Label length must be 1-3 words max.
3. Match the language of the input values if obvious, or use primary catalog language.
4. Example output format:
{{
  "group_1": "Розмір",
  "group_2": "Колір"
}}"""

        try:
            resp_text = await self._call_ollama_raw(prompt)
            clean_json = re.sub(r'^```(?:json)?\s*', '', resp_text, flags=re.MULTILINE)
            clean_json = re.sub(r'\s*```$', '', clean_json, flags=re.MULTILINE).strip()

            mapping = json.loads(clean_json)
            if isinstance(mapping, dict):
                return {
                    str(k): self._clean_field_value(v).strip()
                    for k, v in mapping.items()
                    if self._clean_field_value(v).strip()
                }
        except Exception as e:
            logger.warning(f"⚠️ [OLLAMA OPTION RENAME WARN] Ошибка переименования опций через Ollama: {e}")

        return {}        