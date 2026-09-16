# core/fast_parser.py — Быстрый исполнитель извлечения данных по карте CSS-селекторов (v2)

import logging
import re
from typing import Dict, Any, List, Optional
from urllib.parse import urljoin
from playwright.async_api import Page

from core.models import ProductParent, ProductVariant
from core.state_machine import CatalogStateMachine
from core.category_extractor import CategoryExtractor

logger = logging.getLogger("ChittaParser")


class FastSelectorParser:
    """Быстро извлекает данные товара со страницы Playwright по сгенерированной карте CSS-селекторов."""

    def __init__(self, universal_parser=None):
        self.universal_parser = universal_parser
        self.category_extractor = CategoryExtractor()

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        clean = re.sub(r'<[^>]+>', ' ', str(text))
        return re.sub(r'\s+', ' ', clean).strip()

    @staticmethod
    def _parse_price(raw_text: str) -> float:
        if not raw_text:
            return 0.0
        clean_str = str(raw_text).replace('\xa0', ' ').strip()
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

    @classmethod
    def _extract_all_prices(cls, raw_text: str, sku: str = "", product_id: str = "") -> tuple[float, float]:
        if not raw_text:
            return 0.0, 0.0

        clean_str = str(raw_text)

        # 1. Удаляем проценты скидок, включая дробные (-10%, -12.5%, -15,5%)
        clean_str = re.sub(r'-?\s*\d+(?:[\.,]\d+)?\s*%', '', clean_str)

        # 2. Удаляем фразы экономии/выгоды ("економія 408 грн", "выгода 100$", "save 50")
        clean_str = re.sub(
            r'(?:економія|экономия|вигода|выгода|save|saving|экономия|скидка|знижка)\s*:?\s*-?\s*\d+[\d\s]*[\.,]?\d*\s*(?:грн|₴|\$|€|eur|usd)?',
            '',
            clean_str,
            flags=re.IGNORECASE
        )

        clean_str = clean_str.replace('\xa0', ' ').strip()
        
        matches = re.findall(r'(\d+[\d\s]*[\.,]?\d*)', clean_str)
        
        clean_sku = re.sub(r'\D', '', str(sku)).strip()
        clean_pid = re.sub(r'\D', '', str(product_id)).strip()

        raw_numbers = []
        for match in matches:
            num_str = match.replace(' ', '')
            if not num_str or len(num_str) < 1:
                continue
            if ',' in num_str and '.' in num_str:
                num_str = num_str.replace('.', '').replace(',', '.')
            elif ',' in num_str:
                num_str = num_str.replace(',', '.')
            try:
                val = float(num_str)
                if val > 0:
                    raw_numbers.append(val)
            except (ValueError, TypeError):
                continue

        if not raw_numbers:
            return 0.0, 0.0

        # Фильтруем совпадения с SKU/PID только в случае, когда найдены несколько чисел-кандидатов
        parsed_numbers = []
        for val in raw_numbers:
            val_int_str = str(int(val))
            is_sku_match = clean_sku and len(clean_sku) >= 3 and val_int_str == clean_sku
            is_pid_match = clean_pid and len(clean_pid) >= 3 and val_int_str == clean_pid
            
            if len(raw_numbers) > 1 and (is_sku_match or is_pid_match):
                continue
            parsed_numbers.append(val)

        if not parsed_numbers:
            parsed_numbers = raw_numbers

        if not parsed_numbers:
            return 0.0, 0.0

        if len(parsed_numbers) >= 2:
            # Отфильтровываем числа, которые не похожи на цену (например, малые значения количества или аномальные выбросы)
            valid_prices = [p for p in parsed_numbers if p >= 1.0]
            if not valid_prices:
                return 0.0, 0.0
            
            if len(valid_prices) == 1:
                return valid_prices[0], valid_prices[0]

            fact_p = min(valid_prices)
            old_p = max(valid_prices)
            
            # Если разница цен превышает 4 раза, скорее всего max() зацепил артикул или код
            if fact_p > 0 and (old_p / fact_p) > 4.0:
                old_p = fact_p
            return fact_p, old_p

        val = parsed_numbers[0]
        return val, val

    @staticmethod
    def _extract_currency(raw_text: str) -> str:
        """
        Универсально извлекает знаки или код валюты из любой строки мира, 
        с фильтрацией мусорных глаголов действий на основных европейских и мировых языках.
        """
        if not raw_text:
            return ""

        # 1. Поиск точного знака или ISO-кода валюты (расширенный список)
        match = re.search(r'(₴|\$|€|£|zł|¥|₸|грн|UAH|USD|EUR|PLN|RUB|RON|LEI|LEU|TRY|MDL|KZT|CZK|HUF|BGN|RSD|HRK|SEK|NOK|DKK|CHF|GBP|ILS|AED|CAD|AUD)', str(raw_text), re.IGNORECASE)
        if match:
            return match.group(1).strip()

        # 2. Очистка от цифр, спецсимволов и мусорных UI-глаголов (UA, RU, EN, DE, FR, ES, IT, PL, RO, CS/SK, TR и др.)
        clean_str = str(raw_text)
        verb_patterns = [
            r'купити', r'купить', r'замовити', r'заказать', r'покупка', r'покупкачастинами', r'оформити', r'в\s*кошик', r'в\s*корзину',
            r'buy', r'order', r'add\s*to\s*cart', r'cart', r'checkout', r'purchase',
            r'kaufen', r'bestellen', r'warenkorb', r'in\s*den\s*warenkorb',
            r'acheter', r'commander', r'panier', r'ajouter\s*au\s*panier',
            r'comprar', r'pedir', r'carrito', r'añadir\s*al\s*carrito',
            r'compra', r'acquista', r'ordina', r'preme', r'carrello',
            r'kup', r'zamów', r'koszyk', r'do\s*koszyka',
            r'cumpără', r'comandă', r'coș', r'in\s*coș',
            r'koupit', r'kúpiť', r'objednat', r'do\s*košíku',
            r'satın\s*al', r'sepet', r'sepete\s*ekle'
        ]
        for pattern in verb_patterns:
            clean_str = re.sub(pattern, '', clean_str, flags=re.IGNORECASE)

        # Удаляем оставшиеся цифры и спецсимволы
        clean_symbol = re.sub(r'[\d\s\xa0\.,\'-]+', '', clean_str).strip()
        
        # Если после вырезки мусора остался компактный знак/код валюты
        if 0 < len(clean_symbol) <= 10:
            return clean_symbol

        return ""

    @staticmethod
    def _normalize_availability(raw_text: str) -> str:
        """
        Универсально нормализует текстовые и числовые статусы наличия со всех языков к 'yes' или 'no'.
        """
        if not raw_text:
            return "yes"

        clean = str(raw_text).lower().strip()
        
        # Если статус совпадает с явным нулем "0", "0 шт", "qty: 0"
        if re.search(r'\b0\s*(шт|pcs|st|items|unit|ед)?\b', clean):
            return "no"

        # Расширенный мультиязычный список негативных маркеров
        negative_signals = [
            # UA / RU (Украинский и Русский)
            "нет в наличии", "немає в наявності", "нет", "немає", "нету", 
            "отсутствует", "відсутній", "відсутня", "закончился", "закінчився", 
            "закончились", "закінчилися", "снят с производства", "знято з виробництва", 
            "снято с продажи", "знято з продажу", "недоступен", "недоступно", "не доступно",
            "распродано", "розпродано", "очікується", "ожидается", "под заказ", "під замовлення",
            "уточняйте наличие", "уточнюйте наявність",

            # EN (Английский)
            "out of stock", "sold out", "unavailable", "backorder", "backordered",
            "discontinued", "out-of-stock", "temporarily unavailable", "coming soon",
            "pre-order", "preorder", "no stock", "not in stock", "zero stock",

            # DE (Немецкий)
            "nicht auf lager", "ausverkauft", "nicht lieferbar", "derzeit nicht verfügbar",
            "vergriffen", "nicht vorrätig", "bestellt", "nicht verfügbar",

            # PL (Польский)
            "brak w magazynie", "niedostępny", "niedostępne", "wyprzedane", 
            "brak towaru", "oczekiwanie na dostawę", "chwilowo brak",

            # FR (Французский)
            "épuisé", "non disponible", "rupture de stock", "indisponible", "hors stock",

            # ES (Испанский)
            "agotado", "no disponible", "fuera de stock", "sin stock", "no hay stock",

            # IT (Итальянский)
            "esaurito", "non disponibile", "non in magazzino", "fuori stock",

            # RO (Румынский)
            "stoc epuizat", "nu este in stoc", "indisponibil", "fara stoc",

            # CS / SK (Чешский и Словацкий)
            "vyprodáno", "není skladem", "nedostupné", "vypredané", "nie je na sklade",

            # TR (Турецкий)
            "stokta yok", "tükendi", "temin edilemiyor",

            # Системные и универсальные флаги
            "false", "no", "none", "null", "disabled", "off"
        ]

        if any(sig in clean for sig in negative_signals):
            return "no"

        return "yes"        

    @staticmethod
    def _make_absolute_url(raw_url: str, base_url: str) -> str:
        if not raw_url:
            return ""
        clean_url = str(raw_url).strip()
        if clean_url.startswith("//"):
            return "https:" + clean_url
        return urljoin(base_url, clean_url)

    async def _safe_get_text(self, page: Page, selector: str) -> str:
        if not selector or not isinstance(selector, str):
            return ""
        
        clean_sel = selector.strip()
        
        # Если Ollama вернула прямую строку текста/валюты без символов CSS-селектора
        common_html_tags = {
            "h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "div", "b", "strong", 
            "i", "em", "small", "label", "td", "th", "dt", "dd", "li", "title", "meta", "a"
        }
        if not any(char in clean_sel for char in ['.', '#', '[', ']', '<', '>', ':', ' ']):
            if clean_sel.lower() not in common_html_tags:
                return clean_sel

        # Автоматическая очистка невалидного jQuery-синтаксиса :contains()
        if ":contains(" in clean_sel:
          clean_sel = clean_sel.replace(":contains(", ":has-text(")

        try:
            locator = page.locator(clean_sel).first
            if await locator.count() > 0:
                # Если селектор попал на <meta> тег (Microdata), читаем его атрибут content
                tag_name = await locator.evaluate("el => el.tagName.toLowerCase()")
                if tag_name == "meta":
                    val = await locator.get_attribute("content", timeout=1500)
                    if val:
                        return self._clean_text(val)

                text = await locator.inner_text(timeout=1500)
                return self._clean_text(text)
        except Exception as e:
            logger.debug(f"⚠️ [FAST PARSER] Не удалось прочитать текст для селектора '{selector}': {e}")
            
        return ""

    async def _safe_get_attr(self, page: Page, selector: str, attr_name: str) -> str:
        if not selector or not attr_name:
            return ""

        clean_sel = selector.strip()
        attrs_list = [a.strip() for a in str(attr_name).split(',') if a.strip()]

        common_html_tags = {
            "h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "div", "b", "strong", 
            "i", "em", "small", "label", "td", "th", "dt", "dd", "li", "title", "meta", "a"
        }
        if not any(char in clean_sel for char in ['.', '#', '[', ']', '<', '>', ':', ' ']):
            if clean_sel.lower() not in common_html_tags:
                return clean_sel

        if ":contains(" in clean_sel:
            clean_sel = clean_sel.replace(":contains(", ":has-text(")

        try:
            locator = page.locator(clean_sel).first
            if await locator.count() > 0:
                for attr in attrs_list:
                    val = await locator.get_attribute(attr, timeout=1500)
                    if val and str(val).strip():
                        return str(val).strip()
        except Exception as e:
            logger.debug(f"⚠️ [FAST PARSER] Не удалось прочитать атрибуты '{attr_name}' для '{selector}': {e}")

        return ""

    async def _safe_get_media_url(self, page: Page, selector: str, preferred_attr: str = "") -> str:
        """Перебирает популярные lazy-loading атрибуты картинок, если указанный атрибут пуст."""
        if not selector:
            return ""

        attrs_to_check = [preferred_attr, "src", "data-src", "data-lazy", "data-zoom-image", "href"]
        attrs_to_check = [a for a in attrs_to_check if a]

        for attr in attrs_to_check:
            val = await self._safe_get_attr(page, selector, attr)
            if val and not val.startswith("data:image"):
                return val
        return ""

    async def _extract_all_product_ids(self, page: Page, selectors_str: str, attrs_str: str, max_ids: int = 3) -> list[str]:
        """
        Извлекает ID СТРОГО текущего товара из главного блока страницы (где лежит h1),
        игнорируя рекомендации, слайдеры и подвал. Лимит кандидатов: max_ids (по умолчанию 3).
        """
        if not selectors_str:
            return []

        selectors = [s.strip() for s in str(selectors_str).split(',') if s.strip()]
        attrs_list = [a.strip() for a in str(attrs_str).split(',') if a.strip()] if attrs_str else []
        found_ids = []

        # Селекторы блоков рекомендаций и мусора, которые нужно пропустить
        ignored_containers = [".recommend", ".related", ".similar", ".carousel", ".slider", ".seen", "footer", "aside", "#comments"]

        # Ищем главный контейнер карточки товара
        main_scope = page
        try:
            h1_loc = page.locator("h1").first
            if await h1_loc.count() > 0:
                container = page.locator("main, article, form:has(h1), div:has(h1)").first
                if await container.count() > 0:
                    main_scope = container
        except Exception:
            main_scope = page

        for sel in selectors:
            if len(found_ids) >= max_ids:
                break

            clean_sel = sel.replace(":contains(", ":has-text(")
            try:
                locator = main_scope.locator(clean_sel)
                count = await locator.count()
                
                for i in range(count):
                    if len(found_ids) >= max_ids:
                        break

                    el = locator.nth(i)

                    # Пропускаем элементы, лежащие внутри рекомендаций/подвала
                    try:
                        is_ignored = await el.evaluate(
                            "(node, selectors) => selectors.some(s => node.closest(s) !== null)", 
                            ignored_containers
                        )
                        if is_ignored:
                            continue
                    except Exception:
                        pass

                    # 1. Чтение из текста
                    if not attrs_list or any(a in ["text", "inner_text"] for a in attrs_list):
                        text_val = self._clean_text(await el.inner_text(timeout=1000))
                        if text_val:
                            clean_id = str(text_val).strip()
                            if clean_id and clean_id not in found_ids:
                                if not (clean_id.startswith('{') or clean_id.startswith('[')) and len(clean_id) < 60:
                                    found_ids.append(clean_id)

                    # 2. Чтение из атрибутов
                    for attr in attrs_list:
                        if len(found_ids) >= max_ids:
                            break
                        if attr in ["text", "inner_text"]:
                            continue
                        val = await el.get_attribute(attr, timeout=1000)
                        if val:
                            clean_id = str(val).strip()
                            if clean_id and clean_id not in found_ids:
                                if not (clean_id.startswith('{') or clean_id.startswith('[')) and len(clean_id) < 60:
                                    found_ids.append(clean_id)
            except Exception as e:
                logger.debug(f"⚠️ [FAST PARSER PID] Ошибка сбора ID по селектору '{sel}': {e}")

        return found_ids        

    async def extract_product(
        self, 
        page: Page, 
        url: str, 
        selectors_map: Dict[str, Any], 
        state_machine: CatalogStateMachine
    ) -> Optional[ProductParent]:
        """
        Извлекает данные карточки товара по карте селекторов v2.
        """
        # logger.info(f"⚡ [FAST PARSER v2] Старт мгновенного сбора по селекторам для: {url}")

        # 0. Защита от 404 и несуществующих страниц
        try:
            page_title = (await page.title()).lower()
            error_signals = [
                "404", "сторінку не знайдено", "страницу не найдено", 
                "page not found", "не знайдено", "не найдено", "not found"
            ]
            if any(sig in page_title for sig in error_signals):
                logger.warning(f"⚠️ [FAST PARSER] Страница 404 / не найдена, пропуск: {url}")
                return None
        except Exception as e:
            logger.debug(f"⚠️ [FAST PARSER] Ошибка проверки заголовка 404: {e}")

        # 1. Извлечение категорий
        cat_id, cat_name, cat_link = await self.category_extractor.extract_and_register_categories(
            page, url, state_machine
        )

        # 2. Скалярные текстовые поля
        title = await self._safe_get_text(page, selectors_map.get("title", ""))
        if not title:
            title = self._clean_text(await page.title())

        model = await self._safe_get_text(page, selectors_map.get("model", ""))
        sku = await self._safe_get_text(page, selectors_map.get("sku", ""))
        tags = await self._safe_get_text(page, selectors_map.get("tags", ""))

        # 3. Цены и валюта с защитой от глобальных контейнеров
        price_sel = selectors_map.get("price_container") or selectors_map.get("fact_price", "")
        
        # Точные токены верхнеуровневых оберток
        blacklisted_tokens = {"div.product", "main", "article", "body", "#app", ".product-detail", ".product-info", ".product-page", ".product"}
        
        valid_price_sel = ""
        if price_sel:
            candidates = [s.strip() for s in price_sel.split(",") if s.strip()]
            for cand in candidates:
                target_node = cand.split(">")[-1].split()[-1].lower()
                
                # Точное сравнение вместо подстрочного 'in'
                if target_node not in blacklisted_tokens:
                    valid_price_sel = cand
                    break
            
            if not valid_price_sel and candidates:
                logger.warning(f"⛔ [FAST PARSER] Отклонен верхнеуровневый оберточный селектор price_container: '{price_sel}'")

        raw_price_text = await self._safe_get_text(page, valid_price_sel)
        fact_price, old_price = self._extract_all_prices(raw_price_text, sku=sku, product_id="")

        currency = await self._safe_get_text(page, selectors_map.get("currency", ""))
        if currency:
            currency = self._extract_currency(currency)
        if not currency and raw_price_text:
            currency = self._extract_currency(raw_price_text)
        if not currency:
            currency = "USD"

        bonus = await self._safe_get_text(page, selectors_map.get("bonus", ""))
        raw_available = await self._safe_get_text(page, selectors_map.get("available", ""))
        available = self._normalize_availability(raw_available)
        sales_notes = await self._safe_get_text(page, selectors_map.get("sales_notes", ""))

        # 4. Изображения (Главная картинка + Галерея)
        img_elem = selectors_map.get("main_image_element") or selectors_map.get("main_image", "")
        img_attr = selectors_map.get("main_image_attr", "src")
        
        main_img_raw = await self._safe_get_media_url(page, img_elem, img_attr)
        main_img = self._make_absolute_url(main_img_raw, url)

        # Добор галереи
        gallery_elem = selectors_map.get("gallery_item_element") or selectors_map.get("gallery_images", "")
        gallery_attr = selectors_map.get("gallery_item_attr", "src")

        all_images = []
        if main_img:
            all_images.append(main_img)

        if gallery_elem:
            try:
                gallery_loc = page.locator(gallery_elem)
                g_count = await gallery_loc.count()
                # logger.info(f"🖼️ [FAST PARSER GALLERY] Найдено элементов галереи: {g_count} (по селектору: '{gallery_elem}')")

                for idx in range(g_count):
                    el = gallery_loc.nth(idx)
                    # Пробуем указанный атрибут, а также запасы href, src, data-href
                    raw_g_url = await el.get_attribute(gallery_attr) or await el.get_attribute("href") or await el.get_attribute("src") or await el.get_attribute("data-href") or ""
                    if raw_g_url and not raw_g_url.startswith("data:image"):
                        abs_g_url = self._make_absolute_url(raw_g_url, url)
                        if abs_g_url and abs_g_url not in all_images:
                            all_images.append(abs_g_url)
                            # logger.info(f"   📸 [GALLERY {idx+1}/{g_count}]: {abs_g_url}")
            except Exception as e:
                logger.warning(f"⚠️ [FAST PARSER GALLERY] Ошибка чтения галереи: {e}")

        # Формируем итоговую строку всех уникальных изображений через запятую
        final_images_str = ", ".join(all_images) if all_images else main_img
        # logger.info(f"🖼️ [FAST PARSER MEDIA RESULT] Итого собрано изображений ({len(all_images)} шт): {final_images_str}")

        # 5. Мета-поля и Описание
        brand_name = await self._safe_get_text(page, selectors_map.get("brand_name", ""))
        manufacturer = await self._safe_get_text(page, selectors_map.get("manufacturer", ""))
        country_of_origin = await self._safe_get_text(page, selectors_map.get("country_of_origin", ""))
        description = await self._safe_get_text(page, selectors_map.get("description", ""))

        # 6. Определение системных product_id (основной + альтернативные)
        product_id = ""
        extra_product_ids = []
        pid_elem_sel = selectors_map.get("product_id_element", "")
        pid_attr_name = selectors_map.get("product_id_attr", "")

        if pid_elem_sel:
            all_extracted_ids = await self._extract_all_product_ids(page, pid_elem_sel, pid_attr_name)
            if all_extracted_ids:
                product_id = all_extracted_ids[0]
                extra_product_ids = all_extracted_ids[1:]

        if not product_id and self.universal_parser:
            product_id = await self.universal_parser.resolve_product_id(page, url, fallback_sku="")
            if product_id:
                product_id = re.sub(r'\D', '', str(product_id)).strip()

        # Формирование объекта родителя
        parent = ProductParent(
            product_id=product_id,
            sku=sku,
            title=title,
            model=model,
            description=description,
            sales_notes=sales_notes,
            price=old_price,
            fact_price=fact_price,
            bonus=bonus,
            currency=currency,
            category_id=cat_id,
            category_name=cat_name,
            image=final_images_str,
            product_link=url,
            category_link=cat_link,
            tags=tags,
            brand_name=brand_name,
            manufacturer=manufacturer,
            country_of_origin=country_of_origin,
            available=available
        )
        parent.update_bonus()

        # Запись единого parent_id на родителя и альтернативных product_id_N
        parent.parent_id = product_id
        for alt_idx, alt_id in enumerate(extra_product_ids, 1):
            setattr(parent, f"product_id_{alt_idx}", alt_id)
            setattr(parent, f"parent_product_id_{alt_idx}", alt_id)

        # 7. Характеристики (attributes)
        attr_cfg = selectors_map.get("attributes", {})
        container_sel = str(attr_cfg.get("container", "")).strip()
        row_sel = str(attr_cfg.get("row", "")).strip()
        key_sel = str(attr_cfg.get("key", "")).strip()
        val_sel = str(attr_cfg.get("value", "")).strip()

        if container_sel and row_sel and key_sel and val_sel:
            try:
                container_loc = page.locator(container_sel).first
                if await container_loc.count() > 0:
                    rows_locator = container_loc.locator(row_sel)
                    row_count = await rows_locator.count()
                    for i in range(row_count):
                        row = rows_locator.nth(i)
                        k_el = row.locator(key_sel).first
                        v_el = row.locator(val_sel).first
                        if await k_el.count() > 0 and await v_el.count() > 0:
                            k_text = self._clean_text(await k_el.inner_text(timeout=1500))
                            v_text = self._clean_text(await v_el.inner_text(timeout=1500))
                            if k_text and v_text:
                                parent.attributes[k_text] = v_text
            except Exception as e:
                logger.warning(f"⚠️ [FAST PARSER v2] Ошибка сбора характеристик: {e}")

        # 8. Варианты (с пробросом parent_id, parent_product_id и alt ID)
        var_cfg = selectors_map.get("variants", {})
        v_container = str(var_cfg.get("container", "")).strip()
        v_group_name = str(var_cfg.get("group_name", "")).strip()
        v_item = str(var_cfg.get("item", "")).strip()
        v_val = str(var_cfg.get("value", "")).strip()

        if v_container and v_item:
            try:
                # Нормализуем селектор: если v_item не начинается с v_container, объединяем их
                v_item_clean = v_item.strip()
                v_container_clean = v_container.strip()
                if v_item_clean.startswith(v_container_clean):
                    full_item_sel = v_item_clean
                else:
                    full_item_sel = f"{v_container_clean} {v_item_clean}"
                
                items_locator = page.locator(full_item_sel)
                item_count = await items_locator.count()

                rel_val_sel = v_val.replace(v_item, "").strip() if v_item in v_val else v_val
                if not rel_val_sel: rel_val_sel = v_val

                for i in range(item_count):
                    item_el = items_locator.nth(i)
                    val_text = ""
                    if rel_val_sel:
                        val_sub = item_el.locator(rel_val_sel).first
                        if await val_sub.count() > 0:
                            val_text = self._clean_text(await val_sub.inner_text())
                    if not val_text:
                        val_text = self._clean_text(await item_el.inner_text())

                    group_name_text = "option_1"
                    if v_group_name:
                        gn_el = page.locator(f"{v_container} {v_group_name}").first
                        if await gn_el.count() > 0:
                            raw_gn = self._clean_text(await gn_el.inner_text())
                            # Валидация: имя группы не должно быть длинным заголовком товара или содержать переносы
                            if raw_gn and len(raw_gn) <= 30 and "\n" not in raw_gn:
                                group_name_text = raw_gn
                            else:
                                group_name_text = "option_1"
                        else:
                            group_name_text = "option_1"

                    v_href = await item_el.get_attribute("href") or ""
                    v_id = await item_el.get_attribute("data-value") or await item_el.get_attribute("data-id") or f"{product_id}_{i+1}"
                    v_link = self._make_absolute_url(v_href, url) if v_href else url

                    v_mod_attrs = {group_name_text: val_text}
                    parent.modification_attributes[group_name_text] = ""

                    variant = ProductVariant(
                        product_id=str(v_id).strip(),
                        parent_product_id=product_id,
                        sku=f"{sku}_{i+1}" if sku else str(v_id),
                        parent_sku=sku,
                        title=f"{title} ({val_text})" if val_text else title,
                        description=description,
                        price=old_price,
                        fact_price=fact_price,
                        currency=currency,
                        available=available,
                        image=main_img,
                        product_link=v_link,
                        category_id=cat_id,
                        category_name=cat_name,
                        category_link=cat_link,
                        modification_attributes=v_mod_attrs
                    )
                    # Проброс parent_id и всех parent_id_1, parent_id_2 на вариант
                    variant.parent_id = product_id
                    for alt_idx, alt_id in enumerate(extra_product_ids, 1):
                        setattr(variant, f"parent_product_id_{alt_idx}", alt_id)

                    state_machine.add_variant(product_id, variant)
            except Exception as e:
                logger.warning(f"⚠️ [FAST PARSER v2] Ошибка сбора вариантов: {e}")

        logger.info(f"⚡ [FAST PARSER v2 DONE] Собраны данные для '{parent.title}' (ID: '{parent.product_id}')")
        return parent