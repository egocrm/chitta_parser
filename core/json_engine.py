import json
import logging
import re
from typing import Dict, Any, List, Optional
from core.models import ProductParent, ProductVariant
from core.state_machine import CatalogStateMachine
from playwright.async_api import Page
from core.engines.base import BaseEngine

logger = logging.getLogger("ChittaParser")

class JSONProductExtractor:
    """Извлечение товаров и вариантов из сырых JSON-объектов без привязки к HTML."""

    @staticmethod
    def _safe_float_price(val: Any) -> float:
        """Безопасно преобразует любые строковые и числовые форматы цен во float."""
        if val is None:
            return 0.0
        if isinstance(val, (int, float)):
            return float(val)
        
        clean_str = str(val).replace('\xa0', ' ').strip()
        match = re.search(r'(\d+[\d\s]*[\.,]?\d*)', clean_str)
        if not match:
            return 0.0
        num_str = match.group(1).replace(' ', '')
        if ',' in num_str and '.' in num_str:
            num_str = num_str.replace('.', '').replace(',', '.')
        elif ',' in num_str:
            num_str = num_str.replace(',', '.')
        try:
            return float(num_str)
        except (ValueError, TypeError):
            return 0.0

    # Расширенный список возможных ключей ID товара
    ID_KEYS = {
        "product_id", "productid", "product-id",
        "id",
        "item_id", "itemid", "item-id",
        "goods_id", "goodsid", "goods-id",
        "offer_id", "offerid", "offer-id",
        "entity_id", "entityid",
        "variant_id", "variantid", "variant-id",
        "g_id", "gid"
    }

    VARIANT_ID_KEYS = [
        "variant_id", "variantid", "variant-id",
        "product_id", "productid", "product-id",
        "id",
        "offer_id", "offerid", "offer-id",
        "option_id", "optionid", "option-id",
        "mod_id", "modification_id", "modificationid", "modification-id",
        "item_id", "itemid", "item-id"
    ]

    @classmethod
    def _extract_id_from_dict(cls, data: Dict[str, Any], keys: List[str]) -> str:
        """Вспомогательный метод для гибкого поиска ID по списку возможных ключей с учетом регистра."""
        if not isinstance(data, dict):
            return ""
        
        # Подготавливаем нормализованный список искомых ключей
        target_keys = {k.lower().replace("_", "").replace("-", "") for k in keys}

        # Ищем совпадение напрямую по ключам словаря без создания промежуточных копий
        for k, v in data.items():
            clean_k = str(k).lower().replace("_", "").replace("-", "")
            if clean_k in target_keys and v is not None:
                val_str = str(v).strip()
                if val_str and val_str != "0":
                    return val_str
        return ""

    @staticmethod
    def _find_product_objects(data: Any) -> List[Dict[str, Any]]:
        """Рекурсивно ищет словари, содержащие признаки товара."""
        products = []

        if isinstance(data, dict):
            has_id = bool(JSONProductExtractor._extract_id_from_dict(data, JSONProductExtractor.ID_KEYS))
            
            keys_lower = [str(k).lower() for k in data.keys()]
            has_title = any(k in keys_lower for k in ["name", "title", "caption"])
            has_price = any(k in keys_lower for k in ["price", "raw_price", "discount_price"])

            if has_id and has_title and (has_price or "variants" in keys_lower or "modifications" in keys_lower or "options" in keys_lower):
                products.append(data)
            else:
                for value in data.values():
                    products.extend(JSONProductExtractor._find_product_objects(value))

        elif isinstance(data, list):
            for item in data:
                products.extend(JSONProductExtractor._find_product_objects(item))

        return products

    def parse_json_payload(self, json_data: Dict[str, Any], url: str, state_machine: CatalogStateMachine) -> bool:
        """Разбирает JSON-структуру и находит в ней родительские товары и варианты."""
        raw_products = self._find_product_objects(json_data)
        if not raw_products:
            return False

        parsed_any = False
        for prod in raw_products:
            # Универсальное извлечение обязательного системного product_id родителя
            product_id = self._extract_id_from_dict(prod, self.ID_KEYS)
            if not product_id:
                logger.warning(f"⚠️ [SKIP] Товар пропущен [{url}]: не найден системный product_id в JSON.")
                continue

            sku = str(prod.get("sku") or prod.get("article") or prod.get("code") or product_id).strip()
            title = str(prod.get("name") or prod.get("title") or prod.get("caption") or "").strip()
            description = str(prod.get("description") or prod.get("body_html") or "").strip()
            
            price = self._safe_float_price(prod.get("price") or prod.get("raw_price"))
            fact_price = self._safe_float_price(prod.get("discount_price") or prod.get("special"))
            if fact_price == 0.0:
                fact_price = price
            if price == 0.0:
                price = fact_price

            # Главное изображение и Галерея
            raw_images = prod.get("images") or prod.get("gallery") or prod.get("main_image") or prod.get("image") or prod.get("image_url") or []
            clean_images = []
            if isinstance(raw_images, list):
                for img_item in raw_images:
                    if isinstance(img_item, str) and img_item:
                        if img_item not in clean_images: 
                            clean_images.append(img_item)
                    elif isinstance(img_item, dict):
                        u = img_item.get("url") or img_item.get("src") or ""
                        if u and u not in clean_images: 
                            clean_images.append(u)
            elif isinstance(raw_images, str) and raw_images:
                clean_images.append(raw_images)
            elif isinstance(raw_images, dict):
                u = raw_images.get("url") or raw_images.get("src") or ""
                if u: 
                    clean_images.append(u)

            main_img = ", ".join(clean_images)

            # Категории
            cat_info = prod.get("category", {})
            cat_id = "100"
            cat_name = "Каталог"
            if isinstance(cat_info, dict):
                cat_id = str(cat_info.get("id", "100"))
                cat_name = str(cat_info.get("caption") or cat_info.get("name") or "Каталог")
                state_machine.add_category(cat_id=cat_id, name=cat_name)

            # Динамические характеристики
            attributes = {}
            brand_val = prod.get("brand")
            if brand_val:
                if isinstance(brand_val, str):
                    attributes["brand"] = brand_val.strip()
                elif isinstance(brand_val, dict):
                    attributes["brand"] = str(brand_val.get("name") or brand_val.get("caption") or "").strip()

            for attr in prod.get("attributes", []) or prod.get("group_attrs", []):
                if isinstance(attr, dict) and "name" in attr and "value" in attr:
                    attr_key = f"attr_{str(attr['name']).lower().replace(' ', '_')}"
                    attributes[attr_key] = str(attr["value"])

            parent = ProductParent(
                product_id=product_id,
                sku=sku,
                title=title,
                description=description,
                price=price,
                fact_price=fact_price,
                currency=str(prod.get("currency", "UAH")),
                category_id=cat_id,
                category_name=cat_name,
                image=str(main_img),
                product_link=url,
                attributes=attributes
            )

            if not state_machine.add_parent(parent):
                continue

            parsed_any = True

            # Массив модификаций (размеры, цвета)
            variants_data = prod.get("modifications", []) or prod.get("variants", []) or prod.get("options", [])
            for idx, mod in enumerate(variants_data, 1):
                if not isinstance(mod, dict):
                    continue

                # Универсальное извлечение системного product_id варианта
                v_product_id = self._extract_id_from_dict(mod, self.VARIANT_ID_KEYS)
                if not v_product_id:
                    logger.warning(f"⚠️ [SKIP] Вариант #{idx} пропущен [{url}]: не найден системный product_id варианта в JSON.")
                    continue

                v_sku = str(mod.get("sku") or mod.get("code") or v_product_id).strip()
                v_title = str(mod.get("name") or mod.get("title") or f"{title} (Вариант {idx})")
                v_price = self._safe_float_price(mod.get("price")) or price
                v_fact_price = self._safe_float_price(mod.get("discount_price")) or v_price

                v_img = mod.get("image_url") or mod.get("image") or parent.image
                if isinstance(v_img, dict):
                    v_img = v_img.get("url", parent.image)

                v_mod_attrs = {}
                if "size" in mod and mod["size"]: 
                    v_mod_attrs["size"] = str(mod["size"]).strip()
                if "color" in mod and mod["color"]: 
                    v_mod_attrs["color"] = str(mod["color"]).strip()

                for mod_k in ["option", "variant", "modification", "mod_name"]:
                    if mod_k in mod and mod[mod_k]:
                        v_mod_attrs[mod_k] = str(mod[mod_k]).strip()

                for k in v_mod_attrs.keys():
                    parent.modification_attributes[k] = ""

                v_attrs = {}

                variant = ProductVariant(
                    product_id=v_product_id,
                    parent_product_id=product_id,
                    sku=v_sku,
                    parent_sku=sku,
                    title=f"{title} ({v_title})" if title not in v_title else v_title,
                    price=v_price,
                    fact_price=v_fact_price,
                    category_id=cat_id,
                    category_name=cat_name,
                    image=str(v_img),
                    product_link=url,
                    attributes=v_attrs,
                    modification_attributes=v_mod_attrs
                )
                state_machine.add_variant(product_id, variant)

        return parsed_any
        
class JSONEngine(BaseEngine):
    """Изолированный движок для сайтов на JSON API, Next.js, Nuxt.js и Headless CMS."""

    def __init__(self, parser_instance):
        super().__init__(parser_instance)
        self.extractor = JSONProductExtractor()

    async def can_handle(self, page: Page, url: str) -> bool:
        """Проверяет, является ли страница JSON-ответом или содержит ли встроенные JSON-данные Next.js/Nuxt."""
        if url.endswith(".json") or "/api/" in url or "/graphql" in url:
            return True

        content = await page.content()
        json_signals = [
            "__NEXT_DATA__",
            "__NUXT__",
            "window.__INITIAL_STATE__",
            "application/json"
        ]
        return any(sig in content for sig in json_signals)

    async def extract_catalog_links(self, page: Page) -> List[str]:
        """Фолбэк на универсальное извлечение ссылок через UniversalSemanticParser."""
        return await self.parser.extract_product_links(page)

    async def parse_product_page(
        self, 
        page: Page, 
        url: str, 
        state_machine: Optional[CatalogStateMachine] = None
    ) -> Optional[ProductParent]:
        if not state_machine:
            state_machine = CatalogStateMachine(max_parents=100)

        # 1. Сбор JSON из встроенных тегов скриптов (Next.js / Nuxt / Microdata)
        try:
            embedded_jsons = await page.evaluate("""
                () => {
                    const results = [];
                    // Next.js
                    const nextData = document.querySelector('script#__NEXT_DATA__');
                    if (nextData) {
                        try { results.push(JSON.parse(nextData.textContent)); } catch(e){}
                    }
                    // Nuxt.js
                    const nuxtData = document.querySelector('script#window.__NUXT__');
                    if (nuxtData) {
                        try { results.push(JSON.parse(nuxtData.textContent)); } catch(e){}
                    }
                    // Произвольные JSON скрипты
                    document.querySelectorAll('script[type="application/json"]').forEach(s => {
                        try { results.push(JSON.parse(s.textContent)); } catch(e){}
                    });
                    return results;
                }
            """)

            for raw_json in embedded_jsons:
                if isinstance(raw_json, (dict, list)):
                    success = self.extractor.parse_json_payload(raw_json, url, state_machine)
                    if success:
                        parents = state_machine.get_all_parents()
                        if parents:
                            logger.info(f"⚡ [JSON ENGINE] Товар успешно распарсен из встроенного JSON Script!")
                            return parents[-1]
        except Exception as e:
            logger.debug(f"⚠️ [JSON ENGINE] Ошибка чтения embedded JSON: {e}")

        # 2. Фолбэк: Прямой запрос .json эндпоинта (для Shopify / Custom REST)
        try:
            json_url = url.split("?")[0].rstrip("/") + ".json"
            response = await page.request.get(json_url)
            if response.status == 200:
                raw_json = await response.json()
                if isinstance(raw_json, (dict, list)):
                    success = self.extractor.parse_json_payload(raw_json, url, state_machine)
                    if success:
                        parents = state_machine.get_all_parents()
                        if parents:
                            logger.info(f"⚡ [JSON ENGINE] Товар успешно распарсен через прямой API .json запрос!")
                            return parents[-1]
        except Exception as e:
            logger.debug(f"⚠️ [JSON ENGINE] Ошибка прямого API запроса .json: {e}")

        return None        