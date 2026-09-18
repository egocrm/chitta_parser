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
    def _sanitize_selector(sel: str) -> str:
        """
        Очищает селекторы от jQuery-специфичных псевдоклассов (:contains, :text-is), 
        которые вызывают краш нативного document.querySelectorAll в браузере.
        Превращает их в валидный стандартный CSS, сохраняя область поиска.
        """
        if not sel:
            return ""
        
        # 1. Полностью удаляем :contains("..."), :text-is("..."), :text("...")
        # Это превращает 'div:has(span:contains("foo"))' в валидный CSS 'div:has(span)'
        sel = re.sub(r':contains\s*\([^)]*\)', '', sel, flags=re.IGNORECASE)
        sel = re.sub(r':text-is\s*\([^)]*\)', '', sel, flags=re.IGNORECASE)
        sel = re.sub(r':text\s*\([^)]*\)', '', sel, flags=re.IGNORECASE)
        
        # 2. Убираем лишние пробелы перед закрывающими скобками, если они образовались после вырезки
        sel = re.sub(r'\s+\)', ')', sel)
        
        return sel.strip()       

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
    def _normalize_availability(raw_text: str) -> Optional[str]:
        """
        Универсально нормализует статусы наличия на 44+ языках. 
        Возвращает 'yes', 'no' или None (если сигнал неочевиден и нужна проверка LLM).
        """
        if not raw_text:
            return None

        clean = str(raw_text).lower().strip()
        
        # 1. Явный ноль с единицей измерения (универсально)
        # Мы требуем обязательную единицу измерения, чтобы не спутать "0" в "0,00 грн" с наличием
        if re.search(r'\b0\s+(шт|pcs|st|items|unit|ед|szt|бр|adet|個|個数)\b', clean):
            return "no"

        # 2. Расширенный мультиязычный список НЕГАТИВНЫХ маркеров (Out of Stock)
        negative_signals = [
            # UA / RU
            "нет в наличии", "немає в наявності", "нет", "немає", "нету", "отсутствует", 
            "відсутній", "відсутня", "закончился", "закінчився", "закончились", "закінчилися", 
            "снят с производства", "знято з виробництва", "снято с продажи", "знято з продажу", 
            "недоступен", "недоступно", "не доступно", "распродано", "розпродано", 
            "очікується", "ожидается", "под заказ", "під замовлення", "уточняйте наличие", "уточнюйте наявність",
            # EN
            "out of stock", "sold out", "unavailable", "backorder", "backordered", "discontinued", 
            "out-of-stock", "temporarily unavailable", "coming soon", "pre-order", "preorder", 
            "no stock", "not in stock", "zero stock",
            # DE / NL
            "nicht auf lager", "ausverkauft", "nicht lieferbar", "derzeit nicht verfügbar", 
            "vergriffen", "nicht vorrätig", "bestellt", "niet op voorraad", "uitverkocht",
            # PL / CS / SK / SL / HR / SR
            "brak w magazynie", "niedostępny", "niedostępne", "wyprzedane", "brak towaru", 
            "oczekiwanie na dostawę", "chwilowo brak", "nie ma na stanie",
            "není skladem", "vyprodáno", "nedostupné", "nie je na sklade", "vypredané",
            "ni na zalogi", "razprodano", "nema na zalihi", "rasprodano", "nema na stanju", "rasprodato",
            # FR / ES / PT / IT / RO
            "épuisé", "non disponible", "rupture de stock", "indisponible", "hors stock",
            "agotado", "fuera de stock", "sin stock", "no hay stock", "no disponible",
            "esgotado", "fora de estoque", "indisponível", "sem estoque",
            "esaurito", "non disponibile", "non in magazzino", "fuori stock",
            "nu este in stoc", "epuizat", "indisponibil", "fara stoc", "stoc epuizat",
            # Nordic: DA / SV / NO / FI / IS
            "ikke på lager", "udsolgt", "inte i lager", "slutsåld", "ikke på lager", "utsolgt",
            "ei varastossa", "loppuunmyyty", "ekki til á lager", "uppselt",
            # Baltic: LV / LT / ET
            "nav noliktavā", "izpārdots", "nėra sandėlyje", "išparduota", "pole laos", "läbimüüdud",
            # Eastern EU: HU / BG / EL / MN / KK
            "nincs raktáron", "elfogyott", "няма в наличност", "изчерпан", "μη διαθέσιμο", "εξαντλήθηκε",
            "байхгүй", "дууссан", "қойымда жоқ", "сатылып бітті",
            # Asian: ZH / JA / KO / TH / VI / ID / TL / BN / HI
            "缺货", "无货", "售罄", "暫無現貨",
            "在庫切れ", "売り切れ", "入荷未定", "品切れ",
            "재고 없음", "품절", "품절예정", "재고 소진",
            "สินค้าหมด", "หมดสต็อก", "สินค้าหมดชั่วคราว",
            "hết hàng", "tạm hết hàng", "không có sẵn",
            "stok habis", "tidak tersedia", "kehabisan stok",
            "walang stock", "ubos na", "hindi available",
            "স্টকে নেই", "শেষ হয়েছে",
            "स्टॉक में नहीं है", "समाप्त", "अनुपलब्ध",
            # Other: TR / AF / SW
            "stokta yok", "tükendi", "temin edilemiyor", "mevcut değil",
            "nie op voorraad nie", "uitverkoop",
            "haipo stokini", "imeisha"
        ]
        
        if any(sig in clean for sig in negative_signals):
            return "no"

        # 3. Числовое наличие (универсально). 
        # ИСПРАВЛЕНО: \s+ и отсутствие '?' делают единицу измерения ОБЯЗАТЕЛЬНОЙ.
        # Это предотвращает ложное срабатывание на "5 " в тексте "5 590,00 грн".
        if re.search(r'\b[1-9]\d*\s+(шт|pcs|st|items|unit|ед|szt|бр|adet|個|個数)\b', clean):
            return "yes"

        # 4. БАЗОВЫЕ позитивные маркеры (In Stock). 
        # Только самые однозначные фразы, чтобы избежать ложных срабатываний на описаниях товаров.
        core_positive_signals = [
            # UA / RU
            "в наявності", "в наличии", "є в наявності", "есть в наличии",
            # EN
            "in stock", "instock", "available", "ready to ship",
            # DE / NL
            "auf lager", "verfügbar", "op voorraad", "beschikbaar",
            # PL / CS / SK
            "w magazynie", "dostępny", "na stanie", "skladem",
            # FR / ES / PT / IT
            "en stock", "disponible", "em estoque", "disponível", "in magazzino",
            # Nordic / Baltic
            "på lager", "i lager", "varastossa", "noliktavā", "sandėlyje", "laos",
            # Asian / TR
            "有货", "現貨", "在庫あり", "재고 있음", "มีสินค้า", "còn hàng", "có sẵn", 
            "tersedia", "may stock", "stokta var", "mevcut"
        ]
        
        if any(sig in clean for sig in core_positive_signals):
            return "yes"

        # 5. FALLBACK: Если нет ни явных "нет", ни явных "да", ни чисел с единицами измерения.
        # Скорее всего, селектор захватил название товара, цену, код (SKU) или мусор.
        # Мы НЕ можем уверенно сказать "yes". Возвращаем None, чтобы сработал LLM-батчинг.
        return None    

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

    async def _capture_active_modifications(self, page: Page, variant_selectors: Dict[str, Any], url: str) -> Dict[str, str]:
        """
        Захватывает текущие активные значения переключателей (модификаций) на странице.
        Возвращает словарь {group_name: active_value}.
        """
        active_values = {}
        
        for group_name, group_cfg in variant_selectors.items():
            container = str(group_cfg.get("container", "")).strip()
            item = str(group_cfg.get("item", "")).strip()
            value_attr = str(group_cfg.get("value_attr", "title")).strip()
            
            if not container or not item:
                continue
            
            # Определяем тип переключателя по селектору
            try:
                # Проверяем, является ли это select
                if "select" in container.lower() or "select" in item.lower():
                    select_locator = page.locator(container).first
                    if await select_locator.count() > 0:
                        # Для select берем selected option
                        selected_option = select_locator.locator("option[selected]").first
                        if await selected_option.count() == 0:
                            selected_option = select_locator.locator("option").first
                        
                        if await selected_option.count() > 0:
                            val_text = await selected_option.get_attribute(value_attr) or \
                                       await selected_option.inner_text()
                            if val_text:
                                active_values[group_name] = self._clean_text(val_text)
                
                # Проверяем radio кнопки
                elif "radio" in item.lower() or 'type="radio"' in item:
                    checked_radio = page.locator(f"{container} input[type='radio']:checked").first
                    if await checked_radio.count() > 0:
                        # Пытаемся найти связанный label для получения текста
                        val_text = await checked_radio.get_attribute(value_attr)
                        if not val_text:
                            # Ищем текст в родительском label
                            parent_label = checked_radio.locator("..").locator("label").first
                            if await parent_label.count() > 0:
                                val_text = await parent_label.inner_text()
                        
                        if val_text:
                            active_values[group_name] = self._clean_text(val_text)
                
                # Проверяем кнопки/ссылки с классом active
                else:
                    active_item = page.locator(f"{container} {item}.active").first
                    if await active_item.count() == 0:
                        # Альтернативно ищем элемент с aria-selected или data-active
                        active_item = page.locator(f"{container} {item}[aria-selected='true']").first
                    if await active_item.count() == 0:
                        active_item = page.locator(f"{container} {item}[data-active='true']").first
                    
                    if await active_item.count() > 0:
                        val_text = await active_item.get_attribute(value_attr) or \
                                   await active_item.inner_text()
                        if val_text:
                            active_values[group_name] = self._clean_text(val_text)
                            
            except Exception as e:
                logger.debug(f"⚠️ [CAPTURE MODIFICATIONS] Не удалось захватить значение для '{group_name}': {e}")
        
        return active_values

    async def _build_variant_combinations(self, page: Page, url: str, variant_selectors: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Универсально строит декартово произведение всех вариантов из multiple групп.
        Поддерживает: <select>, <input type="radio">, кликабельные <a> и <button>.
        Возвращает список комбинаций: {"combination": {"color": "red", "size": "L"}, "href": "...", "dom_values": {...}}
        """
        from itertools import product
        
        groups_data = {}
        
        for group_name, group_cfg in variant_selectors.items():
            container = self._sanitize_selector(str(group_cfg.get("container", "")).strip())
            item = self._sanitize_selector(str(group_cfg.get("item", "")).strip())
            value_attr = str(group_cfg.get("value_attr", "title")).strip()
            
            if not container or not item:
                continue
            
            values_list = []
            
            try:
                # Формируем полный селектор элемента
                full_item_sel = item if item.startswith(container) else f"{container} {item}"
                items_locator = page.locator(full_item_sel)
                item_count = await items_locator.count()
                
                for i in range(item_count):
                    item_el = items_locator.nth(i)
                    
                    # 1. Извлекаем текстовое значение
                    val_text = await item_el.get_attribute(value_attr)
                    if not val_text:
                        val_text = await item_el.inner_text()
                    val_text = self._clean_text(val_text) if val_text else f"option_{i+1}"
                    
                    # 2. Извлекаем URL (href или value в случае <option>)
                    href = await item_el.get_attribute("href")
                    
                    # Если href нет у самого элемента, ищем вложенный <a>
                    if not href:
                        nested_link = item_el.locator("a[href]").first
                        if await nested_link.count() > 0:
                            href = await nested_link.get_attribute("href")
                    
                    # Если всё ещё нет — ищем соседний <a> (для PrestaShop: <input> рядом с <a>)
                    if not href:
                        try:
                            sibling_link = item_el.locator("xpath=following-sibling::a[1] | xpath=preceding-sibling::a[1] | xpath=../a[1]").first
                            if await sibling_link.count() > 0:
                                href = await sibling_link.get_attribute("href")
                        except Exception:
                            pass
                    
                    # Fallback: для <option> в <select> URL часто в атрибуте value
                    if not href:
                        href = await item_el.get_attribute("value")
                    
                    # 3. Извлекаем системный ID варианта из DOM (data-id, value и т.д.)
                    variant_id = await item_el.get_attribute("data-value") or \
                                 await item_el.get_attribute("data-id") or \
                                 await item_el.get_attribute("value") or \
                                 f"{group_name}_{i}"
                    
                    values_list.append({
                        "value": val_text,
                        "href": href or "",
                        "variant_id": str(variant_id).strip()
                    })
                
                if values_list:
                    groups_data[group_name] = values_list
                    
            except Exception as e:
                logger.warning(f"⚠️ [BUILD COMBINATIONS] Ошибка сбора данных для группы '{group_name}': {e}")
        
        if not groups_data:
            return []
        
        # Строим декартово произведение
        combinations = []
        group_names = list(groups_data.keys())
        
        for combo_values in product(*[groups_data[g] for g in group_names]):
            combination_dict = {}
            all_hrefs = []
            all_ids = []
            
            for idx, group_name in enumerate(group_names):
                value_data = combo_values[idx]
                combination_dict[group_name] = value_data["value"]
                if value_data["href"]:
                    all_hrefs.append(value_data["href"])
                all_ids.append(value_data["variant_id"])
            
            # Приоритет URL: берем первый найденный непустой
            final_href = next((h for h in all_hrefs if h), "")
            
            combinations.append({
                "combination": combination_dict,
                "href": final_href,
                "variant_ids": all_ids
            })
        
        return combinations

    async def _click_variant_option(self, page: Page, container_sel: str, item_sel: str, target_value: str) -> bool:
        """
        Универсальный метод взаимодействия с опциями (Waterfall Fallback).
        1. Прямой переход, если value - это URL.
        2. Нативный select_option для обычных ID.
        3. Агрессивная JS-эмуляция для упрямых кастомных тем.
        """
        try:
            if container_sel and item_sel.startswith(container_sel):
                items_locator = page.locator(item_sel)
            else:
                items_locator = page.locator(container_sel).locator(item_sel) if container_sel else page.locator(item_sel)
            
            count = await items_locator.count()
            if count == 0:
                return False
            
            for i in range(count):
                item = items_locator.nth(i)
                text = await item.get_attribute("title") or await item.inner_text() or await item.get_attribute("value")
                if not text:
                    continue
                
                if target_value.lower().strip() in text.lower().strip():
                    tag = await item.evaluate("el => el.tagName.toLowerCase()")
                    
                    if tag == "option":
                        parent_select = item.locator("xpath=ancestor::select[1]")
                        if await parent_select.count() > 0:
                            option_value = await item.get_attribute("value")
                            
                            # УРОВЕНЬ 1: Прямой переход, если value является ссылкой
                            if option_value and (option_value.startswith("http") or option_value.startswith("/")):
                                # logger.info(f"🚀 [CLICK] Прямой переход по URL из <option>: {option_value}")
                                await page.goto(option_value, wait_until="domcontentloaded", timeout=10000)
                                return True
                            
                            # УРОВЕНЬ 2: Нативный выбор
                            await parent_select.first.select_option(label=target_value, timeout=3000)
                            
                            # УРОВЕНЬ 3: Агрессивная JS-эмуляция (на случай, если нативный выбор не триггернул AJAX)
                            await page.evaluate("""
                                (sel) => {
                                    const el = document.querySelector(sel);
                                    if (el) {
                                        el.dispatchEvent(new Event('mousedown', { bubbles: true }));
                                        el.dispatchEvent(new Event('mouseup', { bubbles: true }));
                                        el.dispatchEvent(new Event('click', { bubbles: true }));
                                        el.dispatchEvent(new Event('change', { bubbles: true }));
                                        el.dispatchEvent(new Event('input', { bubbles: true }));
                                    }
                                }
                            """, await parent_select.first.evaluate("el => el.tagName + (el.id ? '#' + el.id : '') + (el.className ? '.' + el.className.replace(/\\s+/g, '.') : '')")
                            )
                            return True
                            
                    elif tag == "input":
                        parent_label = item.locator("xpath=ancestor::label[1]")
                        if await parent_label.count() > 0:
                            await parent_label.first.click(timeout=3000)
                            return True
                        else:
                            await item.click(force=True, timeout=3000)
                            return True
                    else:
                        await item.click(force=True, timeout=3000)
                        return True
                        
        except Exception as e:
            logger.warning(f"⚠️ [CLICK HELPER] Ошибка обработки '{target_value}': {e}")
        
        return False

    def _create_and_add_variant_from_combo(
        self, page: Page, parent: ProductParent, state_machine: CatalogStateMachine, 
        extracted_id: str, current_url: str, current_combo: Dict[str, str], leaf_data: Dict[str, Any]
    ):
        """Вспомогательный метод для создания и добавления варианта из leaf-узла рекурсии."""
        mod_parts = [f"{k}: {v}" for k, v in current_combo.items()]
        variant_title = f"{leaf_data.get('title', '')} ({', '.join(mod_parts)})" if mod_parts else leaf_data.get('title', '')
        variant_sku = f"{leaf_data.get('sku', '')}_{'-'.join(str(v) for v in current_combo.values())}" if leaf_data.get('sku') else extracted_id
        
        variant = ProductVariant(
            product_id=str(extracted_id),
            parent_product_id=leaf_data['product_id'],
            sku=str(variant_sku).strip(),
            parent_sku=leaf_data.get('sku', ''),
            title=variant_title,
            description=parent.description,
            price=leaf_data.get('old_price', 0),
            fact_price=leaf_data.get('price', 0),
            bonus=leaf_data.get('bonus', ''),
            currency=leaf_data.get('currency', 'UAH'),
            available=leaf_data.get('available', 'yes'),
            image=leaf_data.get('image', ''),
            product_link=current_url,
            category_id=parent.category_id,
            category_name=parent.category_name,
            category_link=parent.category_link,
            modification_attributes=current_combo.copy(),
            raw_availability_html=leaf_data.get('raw_availability_html', '')
        )
        
        for alt_idx, alt_id in enumerate(leaf_data.get('extra_product_ids', []), 1):
            setattr(variant, f"parent_product_id_{alt_idx}", alt_id)
        
        state_machine.add_variant(leaf_data['product_id'], variant)
        # logger.info(f"🔹 [VARIANT ADDED] ID: {extracted_id}")

    async def _resolve_variants_recursive(
        self,
        page: Page,
        parent_url: str,
        remaining_groups: List[str],
        current_combo: Dict[str, str],
        variant_selectors: Dict[str, Any],
        selectors_map: Dict[str, Any],
        parent_product: ProductParent,
        state_machine: CatalogStateMachine,
        base_data: Dict[str, Any],
        depth: int = 0
    ) -> None:
        """Рекурсивный обход дерева вариантов (DFS). Создание варианта только на 'листе' дерева."""
        MAX_DEPTH = 3
        
        # 1. БАЗОВЫЙ СЛУЧАЙ: Мы перебрали все группы опций. Только здесь мы создаем вариант/модификацию.
        if not remaining_groups or depth >= MAX_DEPTH:
            logger.info(f"🛑 [DFS DIAG] ЛИСТ ДЕРЕВА (глубина {depth}). Финальное комбо: {current_combo}")
            
            current_url = page.url
            
            # === НАДЕЖНЫЙ ПОДХОД: Копируем базу для гарантии всех ключей, но ЯВНО сбрасываем динамические поля ===
            leaf_data = base_data.copy()
            
            # Явный сброс динамических полей перед попыткой извлечения со страницы варианта
            leaf_data['price'] = 0.0
            leaf_data['old_price'] = 0.0
            leaf_data['bonus'] = ''
            leaf_data['image'] = ''
            leaf_data['sales_notes'] = ''
            leaf_data['available'] = None        
            leaf_data['raw_availability_html'] = ''           
            try:
                # 1. Цены (пытаемся извлечь со страницы варианта)
                price_sel = selectors_map.get("price_container") or selectors_map.get("fact_price", "")
                if price_sel:
                    raw_price_text = await self._safe_get_text(page, price_sel)
                    if raw_price_text:
                        fact_p, old_p = self._extract_all_prices(raw_price_text, sku=leaf_data.get('sku', ''), product_id="")
                        if fact_p > 0:
                            leaf_data['price'] = fact_p
                        if old_p > 0:
                            leaf_data['old_price'] = old_p
                
                # 2. Валюта
                currency_sel = selectors_map.get("currency", "")
                if currency_sel:
                    raw_currency = await self._safe_get_text(page, currency_sel)
                    if raw_currency:
                        leaf_data['currency'] = self._extract_currency(raw_currency)
                
                # Фолбэк: если валюта не найдена по селектору, ищем в тексте цены
                if leaf_data.get('currency') == 'UAH' and price_sel:
                    raw_price_text_retry = await self._safe_get_text(page, price_sel)
                    if raw_price_text_retry:
                        extracted_curr = self._extract_currency(raw_price_text_retry)
                        if extracted_curr:
                            leaf_data['currency'] = extracted_curr

                # 3. Бонус/Скидка (ЖЕСТКАЯ ЛОГИКА)
                bonus_sel = selectors_map.get("bonus", "")
                if bonus_sel:
                    bonus_text = await self._safe_get_text(page, bonus_sel)
                    # Сохраняем только если текст явно содержит признаки скидки
                    if bonus_text:
                        # Универсальная валидация: текст содержит '%' или любую распознаваемую валюту (€, zł, CZK и т.д.)
                        if '%' in bonus_text or self._extract_currency(bonus_text):
                            leaf_data['bonus'] = bonus_text
                
                # Авто-расчет ТОЛЬКО если бонус все еще пуст и цены корректно извлечены
                if not leaf_data['bonus']:
                    old_p = leaf_data.get('old_price', 0)
                    fact_p = leaf_data.get('price', 0)
                    if old_p > fact_p > 0:
                        discount_percent = int((1 - (fact_p / old_p)) * 100)
                        leaf_data['bonus'] = f"-{discount_percent}%"
                        # logger.info(f"🏷️ [DFS DIAG] Бонус рассчитан: {leaf_data['bonus']} (из {old_p} -> {fact_p})")
                    # else:
                        # logger.info(f"🏷️ [DFS DIAG] Бонус не рассчитан: old={old_p}, fact={fact_p}")

                # 4. Наличие: FAST-PATH (быстрый текст) -> SLOW-PATH (HTML для LLM)
                avail_sel = selectors_map.get("available", "")
                found_clear_status = False
                
                # ШАГ 1: Быстрая проверка основного селектора по тексту
                if avail_sel:
                    try:
                        avail_locator = page.locator(avail_sel).first
                        if await avail_locator.count() > 0:
                            visible_text = await avail_locator.inner_text(timeout=1500)
                            clean_text_for_log = visible_text.replace('\n', ' ').strip()[:100] # Для логов, чтобы не спамить
                            fast_result = self._normalize_availability(visible_text)
                            
                            # logger.info(f"🔍 [AVAIL STEP 1] Селектор: '{avail_sel}' | Текст: '{clean_text_for_log}' | Вердикт: {fast_result}")
                            
                            if fast_result in ["yes", "no"]:
                                leaf_data['available'] = fast_result
                                leaf_data['raw_availability_html'] = "" # LLM не нужен, мы уже знаем ответ
                                found_clear_status = True
                    except Exception as e:
                        logger.debug(f"⚠️ [AVAIL STEP 1] Ошибка: {e}")
                
                # ШАГ 2: Если не нашли, поднимаемся по DOM и ПРОВЕРЯЕМ ТЕКСТ (Fast Path Fallback)
                if not found_clear_status:
                    fallback_sel = selectors_map.get("price_container") or selectors_map.get("title", "")
                    if fallback_sel:
                        try:
                            # Одним запросом JS получаем innerText самого элемента, родителя и прародителя
                            texts = await page.evaluate("""
                                (sel) => {
                                    const el = document.querySelector(sel);
                                    if (!el) return [];
                                    const results = [];
                                    let current = el;
                                    for (let i = 0; i <= 2; i++) { // 0 = сам элемент, 1 = родитель, 2 = прародитель
                                        if (current && current !== document.body) {
                                            results.push(current.innerText);
                                            current = current.parentElement;
                                        }
                                    }
                                    return results;
                                }
                            """, fallback_sel)

                            # Проверяем каждый уровень нашим быстрым Python-методом
                            for level, txt in enumerate(texts):
                                clean_txt_for_log = txt.replace('\n', ' ').strip()[:100]
                                fast_result = self._normalize_availability(txt)
                                
                                # logger.info(f"🔍 [AVAIL STEP 2] Уровень DOM +{level} | Текст: '{clean_txt_for_log}' | Вердикт: {fast_result}")
                                
                                if fast_result in ["yes", "no"]:
                                    leaf_data['available'] = fast_result
                                    leaf_data['raw_availability_html'] = "" # LLM не нужен
                                    found_clear_status = True
                                    break
                            
                            # ШАГ 3: SLOW-PATH. Только если текст на всех уровнях не дал четкого "yes" или "no", сохраняем HTML для LLM
                            if not found_clear_status and len(texts) > 0:
                                # logger.info(f"🔍 [AVAIL STEP 3] Четкий сигнал не найден. Готовим HTML для LLM-батчинга.")
                                parent_html = await page.evaluate("""
                                    (sel) => {
                                        const el = document.querySelector(sel);
                                        if (!el) return '';
                                        let current = el;
                                        for (let i = 0; i < 2 && current && current !== document.body; i++) {
                                            current = current.parentElement;
                                        }
                                        return current ? current.innerHTML : '';
                                    }
                                """, fallback_sel)
                                
                                if parent_html:
                                    leaf_data['raw_availability_html'] = parent_html
                                    leaf_data['available'] = None # Принудительно в None, чтобы main.py отправил это в LLM
                        except Exception as e:
                            logger.debug(f"⚠️ [AVAIL FALLBACK] Ошибка: {e}")
                # else:
                #     logger.info(f"✅ [AVAIL SKIP] Наличие уже определено на Шаге 1, пропускаем Fallback и LLM.")


                # 5. Sales Notes
                sales_sel = selectors_map.get("sales_notes", "")
                if sales_sel:
                    sales_text = await self._safe_get_text(page, sales_sel)
                    if sales_text:
                        leaf_data['sales_notes'] = sales_text

                # 6. ГАЛЕРЕЯ КАРТИНОК (Собираем строго со страницы варианта)
                img_elem_sel = selectors_map.get("main_image_element") or selectors_map.get("main_image", "")
                img_attr = selectors_map.get("main_image_attr", "src")
                
                all_images = []
                
                if img_elem_sel:
                    main_img_raw = await self._safe_get_media_url(page, img_elem_sel, img_attr)
                    if main_img_raw:
                        main_img_abs = self._make_absolute_url(main_img_raw, current_url)
                        if main_img_abs:
                            all_images.append(main_img_abs)
                
                gallery_elem = selectors_map.get("gallery_item_element") or selectors_map.get("gallery_images", "")
                gallery_attr = selectors_map.get("gallery_item_attr", "src")
                
                if gallery_elem:
                    try:
                        gallery_loc = page.locator(gallery_elem)
                        g_count = await gallery_loc.count()
                        for idx in range(g_count):
                            el = gallery_loc.nth(idx)
                            raw_g_url = (await el.get_attribute(gallery_attr) or 
                                        await el.get_attribute("href") or 
                                        await el.get_attribute("src") or 
                                        await el.get_attribute("data-href") or "")
                            if raw_g_url and not str(raw_g_url).startswith("data:image"):
                                abs_g_url = self._make_absolute_url(raw_g_url, current_url)
                                if abs_g_url and abs_g_url not in all_images:
                                    all_images.append(abs_g_url)
                    except Exception as e:
                        logger.warning(f"⚠️ [DFS GALLERY] Ошибка чтения галереи: {e}")
                
                if all_images:
                    leaf_data['image'] = ", ".join(all_images)
                    # logger.info(f"🖼️ [DFS DIAG] Собрано {len(all_images)} картинок для варианта")
                    
            except Exception as e:
                logger.warning(f"⚠️ [DFS DIAG] Не удалось извлечь актуальные данные, остаются сброшенные (0 или пусто): {e}")
            # ==========================================================

            # Извлекаем ID
            extracted_id = ""
            llm_sel = selectors_map.get("product_id_element", "")
            llm_attr = selectors_map.get("product_id_attr", "value")
            if llm_sel:
                try:
                    locator = page.locator(llm_sel).first
                    if await locator.count() > 0:
                        val = await locator.get_attribute(llm_attr, timeout=2000)
                        if val and str(val).strip() and str(val).strip() != "0":
                            extracted_id = str(val).strip()
                except Exception:
                    pass

            # Проверяем уникальность ID
            is_duplicate = False
            if extracted_id:
                existing_real_ids = {str(v.product_id) for v in parent_product.variants if not getattr(v, 'is_synthetic', False)}
                if extracted_id in existing_real_ids or extracted_id == parent_product.product_id:
                    is_duplicate = True

            # === ТОЧКА 1: ДИАГНОСТИКА ПЕРЕД СОЗДАНИЕМ ===
            # logger.info(f"🔍 [DIAG 1] Комбо: {current_combo} | extracted_id: '{extracted_id}' | is_duplicate: {is_duplicate}")
            # logger.info(f"🔍 [DIAG 1] leaf_data['price']: {leaf_data.get('price')} | leaf_data['old_price']: {leaf_data.get('old_price')}")
            # logger.info(f"🔍 [DIAG 1] leaf_data['bonus']: '{leaf_data.get('bonus')}'")
            # ==========================================

            # Создаем запись
            # === ЛОГИКА СОЗДАНИЯ ЗАПИСИ ===
            if is_duplicate or not extracted_id:
                # 1. Инициализируем список modifications в родителе, если его нет
                if not hasattr(parent_product, 'modifications') or parent_product.modifications is None:
                    parent_product.modifications = []
                
                # 2. КЛЮЧЕВОЕ ДОБАВЛЕНИЕ: Если ID совпадает с родителем, это наш "дефолтный" вариант
                if extracted_id == parent_product.product_id:
                    # logger.info(f"🏷️ [DFS DIAG] Найден ДЕФОЛТНЫЙ вариант (ID={extracted_id} == ID родителя). Сохраняем его модификации в parent.default_variant_mods.")
                    # Явно сохраняем комбинацию как дефолтную для родителя
                    parent_product.default_variant_mods = current_combo.copy()
                    # 2. Обновляем данные самого родителя, если они валидны
                    if leaf_data.get('price', 0) > 0:
                        parent_product.fact_price = leaf_data['price']
                    if leaf_data.get('old_price', 0) > 0:
                        parent_product.price = leaf_data['old_price']
                    if leaf_data.get('bonus'):
                        parent_product.bonus = leaf_data['bonus']
                    if leaf_data.get('image'):
                        parent_product.image = leaf_data['image']                    
                else:
                    # logger.info(f"🏷️ [DFS DIAG] Модификация (ID={extracted_id} не уникален). Сохраняем в parent.modifications.")
                    if not hasattr(parent_product, 'modifications') or parent_product.modifications is None:
                        parent_product.modifications = []
                    
                    parent_product.modifications.append({
                        "combo": current_combo.copy(),
                        "price": leaf_data.get('price', parent_product.fact_price),
                        "old_price": leaf_data.get('old_price', parent_product.price),
                        "bonus": leaf_data.get('bonus', ''),
                        "image": leaf_data.get('image', parent_product.image),
                        "currency": leaf_data.get('currency', parent_product.currency),
                        "available": leaf_data.get('available', parent_product.available),
                        "raw_availability_html": leaf_data.get('raw_availability_html', '')
                    })
                
                # ВАЖНО: Мы делаем return здесь. Вариант НЕ создается и НЕ попадает в parent.variants.
                return                
            else:
                # logger.info(f"✅ [DFS DIAG] Вариант (уникальный ID: {extracted_id})")
                self._create_and_add_variant_from_combo(page, parent_product, state_machine, extracted_id, current_url, current_combo, leaf_data)
            
            return # Возвращаемся ТОЛЬКО здесь, на листе дерева

        # 2. РЕКУРСИВНЫЙ ШАГ: Перебираем опции текущей группы
        current_group = remaining_groups[0]
        next_groups = remaining_groups[1:]
        
        cfg = variant_selectors.get(current_group, {})
        container_sel = self._sanitize_selector(str(cfg.get("container", "")).strip())
        item_sel = self._sanitize_selector(str(cfg.get("item", "")).strip())

        if not container_sel or not item_sel:
            await self._resolve_variants_recursive(page, parent_url, next_groups, current_combo, variant_selectors, selectors_map, parent_product, state_machine, base_data, depth + 1)
            return

        if container_sel and item_sel.startswith(container_sel):
            items_locator = page.locator(item_sel)
        else:
            items_locator = page.locator(container_sel).locator(item_sel) if container_sel else page.locator(item_sel)

        try:
            count = await items_locator.count()
        except Exception as e:
            logger.error(f"❌ [DFS DIAG] Ошибка поиска: {e}")
            return

        for i in range(count):
            item = items_locator.nth(i)
            text = await item.get_attribute("title") or await item.inner_text() or await item.get_attribute("value")
            if not text: continue
            
            target_value = text.strip()
            # _check
            previous_url = page.url
            
            clicked = await self._click_variant_option(page, container_sel, item_sel, target_value)
            if not clicked: continue

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                await page.wait_for_timeout(1000)
            
            # ВАЖНО: Мы НЕ проверяем is_variant здесь для прерывания. 
            # Мы просто передаем накопленный new_combo дальше в рекурсию.
            # Рекурсивный вызов для следующих групп
            new_combo = current_combo.copy()
            new_combo[current_group] = target_value

            await self._resolve_variants_recursive(
                page=page, parent_url=parent_url, remaining_groups=next_groups,
                current_combo=new_combo, variant_selectors=variant_selectors, selectors_map=selectors_map,
                parent_product=parent_product, state_machine=state_machine, base_data=base_data, depth=depth + 1
            )

            # Возврат на предыдущую страницу
            if page.url != previous_url:
                try:
                    await page.goto(previous_url, wait_until="domcontentloaded", timeout=10000)
                    await page.wait_for_timeout(500)
                except Exception as e:
                    logger.error(f"❌ [DFS DIAG] Ошибка возврата: {e}")
                    raise

    async def _parse_multi_group_variants(
        self,
        page: Page,
        url: str,
        parent: ProductParent,
        state_machine: CatalogStateMachine,
        variant_selectors: Dict[str, Any],
        base_title: str,
        base_sku: str,
        base_price: float,
        base_old_price: float,
        base_currency: str,
        base_available: str,
        base_image: str,
        product_id: str,
        extra_product_ids: List[str]
    ):
        """Точка входа в рекурсивный обход вариантов."""
        groups = list(variant_selectors.keys())
        if not groups:
            # logger.warning(f"⚠️ [MULTI-GROUP] Не найдено групп вариантов для товара {product_id}")
            return
        
        base_data = {
            "title": base_title, "sku": base_sku, "price": base_price, 
            "old_price": base_old_price, "currency": base_currency, 
            "available": base_available, "image": base_image, 
            "product_id": product_id, "extra_product_ids": extra_product_ids
        }
        
        logger.info(f"🚀 [DFS START] Запуск рекурсивного обхода. Группы: {groups}")
        
        await self._resolve_variants_recursive(
            page=page,
            parent_url=url,
            remaining_groups=groups,
            current_combo={},
            variant_selectors=variant_selectors,
            selectors_map=self.selectors_map,
            parent_product=parent,
            state_machine=state_machine,
            base_data=base_data,
            depth=0
        )
        
        logger.info(f"🏁 [DFS END] Рекурсивный обход завершен. Собрано вариантов: {len(parent.variants)}, модификаций: {len(parent.modifications)}")
    
    async def _parse_flat_variants(
        self,
        page: Page,
        url: str,
        parent: ProductParent,
        state_machine: CatalogStateMachine,
        var_cfg: Dict[str, Any],
        base_title: str,
        base_sku: str,
        base_price: float,
        base_old_price: float,
        base_currency: str,
        base_available: str,
        base_image: str,
        product_id: str,
        extra_product_ids: List[str]
    ):
        """
        Обрабатывает варианты с одной группой (плоский формат).
        Также применяет строгую проверку на наличие реального ID перед созданием ProductVariant.
        """
        v_container = str(var_cfg.get("container", "")).strip()
        v_group_name = str(var_cfg.get("group_name", "")).strip() or "option_1"
        v_item = str(var_cfg.get("item", "")).strip()
        v_val = str(var_cfg.get("value", "")).strip()
        
        if not v_container or not v_item:
            return
        
        try:
            full_item_sel = v_item if v_item.startswith(v_container) else f"{v_container} {v_item}"
            items_locator = page.locator(full_item_sel)
            item_count = await items_locator.count()
            
            rel_val_sel = v_val.replace(v_item, "").strip() if v_item in v_val else v_val
            if not rel_val_sel:
                rel_val_sel = v_val
            
            for i in range(item_count):
                item_el = items_locator.nth(i)
                val_text = ""
                
                if rel_val_sel:
                    val_sub = item_el.locator(rel_val_sel).first
                    if await val_sub.count() > 0:
                        val_text = self._clean_text(await val_sub.inner_text())
                
                if not val_text:
                    val_text = self._clean_text(await item_el.inner_text())
                
                v_href = await item_el.get_attribute("href") or await item_el.get_attribute("value") or ""
                v_id_raw = await item_el.get_attribute("data-value") or await item_el.get_attribute("data-id") or ""
                v_link = self._make_absolute_url(v_href, url) if v_href else url
                
                # СТРОГАЯ ПРОВЕРКА: Если нет ни URL, ни реального ID, это не вариант, а модификация
                is_real_variant = bool(v_id_raw and str(v_id_raw).strip() and str(v_id_raw).strip() != product_id) or \
                                  (v_link and v_link != url)
                
                if is_real_variant:
                    # Это ВАРИАНТ
                    variant = ProductVariant(
                        product_id=str(v_id_raw).strip() or f"link_{hash(v_link) % 100000}",
                        parent_product_id=product_id,
                        sku=f"{base_sku}_{i+1}" if base_sku else str(v_id_raw),
                        parent_sku=base_sku,
                        title=f"{base_title} ({val_text})" if val_text else base_title,
                        description=parent.description,
                        price=base_old_price,
                        fact_price=base_price,
                        currency=base_currency,
                        available=base_available,
                        image=base_image,
                        product_link=v_link,
                        category_id=parent.category_id,
                        category_name=parent.category_name,
                        category_link=parent.category_link,
                        modification_attributes={v_group_name: val_text}
                    )
                    
                    variant.parent_id = product_id
                    for alt_idx, alt_id in enumerate(extra_product_ids, 1):
                        setattr(variant, f"parent_product_id_{alt_idx}", alt_id)
                    
                    state_machine.add_variant(product_id, variant)
                    logger.info(f"🔹 [FLAT VARIANT] Добавлен: {variant.title} (ID: {variant.product_id})")
                else:
                    # Это МОДИФИКАЦИЯ
                    parent.modification_attributes[v_group_name] = val_text
                    logger.info(f"🏷️ [FLAT MODIFICATION] ID не найден. '{v_group_name}: {val_text}' сохранен как модификация родителя.")
                    
        except Exception as e:
            logger.warning(f"⚠️ [FLAT VARIANTS] Ошибка сбора вариантов: {e}")

    async def _parse_single_group_variants(
        self,
        page: Page,
        url: str,
        parent: ProductParent,
        state_machine: CatalogStateMachine,
        var_cfg: Dict[str, Any],
        base_title: str,
        base_sku: str,
        base_price: float,
        base_old_price: float,
        base_currency: str,
        base_available: str,
        base_image: str,
        product_id: str,
        extra_product_ids: List[str]
    ):
        """
        Обрабатывает варианты с одной группой (старый плоский формат).
        """
        v_container = str(var_cfg.get("container", "")).strip()
        v_group_name = str(var_cfg.get("group_name", "")).strip() or "option_1"
        v_item = str(var_cfg.get("item", "")).strip()
        v_val = str(var_cfg.get("value", "")).strip()
        
        if not v_container or not v_item:
            return
        
        try:
            # Нормализуем селектор
            if v_item.startswith(v_container):
                full_item_sel = v_item
            else:
                full_item_sel = f"{v_container} {v_item}"
            
            items_locator = page.locator(full_item_sel)
            item_count = await items_locator.count()
            
            rel_val_sel = v_val.replace(v_item, "").strip() if v_item in v_val else v_val
            if not rel_val_sel:
                rel_val_sel = v_val
            
            # Захват активной модификации перед циклом
            active_value = ""
            try:
                # Пробуем найти активный элемент
                active_item = page.locator(f"{full_item_sel}.active").first
                if await active_item.count() == 0:
                    active_item = page.locator(f"{full_item_sel}[aria-selected='true']").first
                if await active_item.count() == 0:
                    active_item = page.locator(f"{full_item_sel}:has(input:checked)").first
                
                if await active_item.count() > 0:
                    active_value = await active_item.inner_text()
            except:
                pass
            
            if active_value:
                parent.modification_attributes[v_group_name] = self._clean_text(active_value)
            
            for i in range(item_count):
                item_el = items_locator.nth(i)
                val_text = ""
                
                if rel_val_sel:
                    val_sub = item_el.locator(rel_val_sel).first
                    if await val_sub.count() > 0:
                        val_text = self._clean_text(await val_sub.inner_text())
                
                if not val_text:
                    val_text = self._clean_text(await item_el.inner_text())
                
                v_href = await item_el.get_attribute("href") or ""
                v_id = await item_el.get_attribute("data-value") or \
                       await item_el.get_attribute("data-id") or \
                       f"{product_id}_{i+1}"
                v_link = self._make_absolute_url(v_href, url) if v_href else url
                
                v_mod_attrs = {v_group_name: val_text}
                if v_group_name not in parent.modification_attributes:
                    parent.modification_attributes[v_group_name] = ""
                
                variant = ProductVariant(
                    product_id=str(v_id).strip(),
                    parent_product_id=product_id,
                    sku=f"{base_sku}_{i+1}" if base_sku else str(v_id),
                    parent_sku=base_sku,
                    title=f"{base_title} ({val_text})" if val_text else base_title,
                    description=parent.description,
                    price=base_old_price,
                    fact_price=base_price,
                    currency=base_currency,
                    available=base_available,
                    image=base_image,
                    product_link=v_link,
                    category_id=parent.category_id,
                    category_name=parent.category_name,
                    category_link=parent.category_link,
                    modification_attributes=v_mod_attrs
                )
                
                variant.parent_id = product_id
                for alt_idx, alt_id in enumerate(extra_product_ids, 1):
                    setattr(variant, f"parent_product_id_{alt_idx}", alt_id)
                
                state_machine.add_variant(product_id, variant)
                
        except Exception as e:
            logger.warning(f"⚠️ [SINGLE-GROUP VARIANTS] Ошибка сбора вариантов: {e}")

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
        # Сохраняем карту селекторов в экземпляре для использования в других методах
        self.selectors_map = selectors_map
        
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

        # Регистрируем родителя в state_machine перед обработкой вариантов
        if product_id:
            # === ПРОВЕРКА: ЭТО УЖЕ СОБРАННЫЙ ВАРИАНТ? ===
            if hasattr(state_machine, 'registered_variant_ids') and product_id in state_machine.registered_variant_ids:
                logger.info(f"⏩ [FAST PARSER SKIP] Ссылка {url} ведет на вариант (ID: {product_id}), который уже был собран ранее. Пропускаем.")
                return None  # Возвращаем None, чтобы main.py знал, что делать нечего
            
            is_added = state_machine.add_parent(parent)
            if not is_added:
                logger.info(f"⏩ [FAST PARSER SKIP] Товар ID {product_id} уже зарегистрирован в системе. Пропускаем.")
                return None

            logger.debug(f"✅ [FAST PARSER v2] Зарегистрирован родительский товар ID: {product_id}")
        else:
            logger.warning(f"⚠️ [FAST PARSER v2] Не найден product_id для товара {url}, используем заглушку")
            # Создаем временный ID если product_id не найден
            temp_id = f"temp_{hash(url) % 100000}"
            parent.parent_id = temp_id
            state_machine.add_parent(parent, override_id=temp_id)
            product_id = temp_id

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
                                # ИЗМЕНЕНИЕ: Сохраняем найденные атрибуты в raw_attributes вместо attributes
                                parent.raw_attributes.append({k_text: v_text})
            except Exception as e:
                logger.warning(f"⚠️ [FAST PARSER v2] Ошибка сбора характеристик: {e}")

        # 8. Варианты (с пробросом parent_id, parent_product_id и alt ID)
        # Поддержка формата Gemini: variant_selectors = {color: {...}, size: {...}}
        var_cfg = selectors_map.get("variant_selectors", selectors_map.get("variants", {}))
        
        if var_cfg and isinstance(var_cfg, dict):
            # Проверяем формат: если это словарь групп {color: {...}, size: {...}}
            is_multi_group = any(isinstance(v, dict) for v in var_cfg.values())
            
            if is_multi_group:
                # Формат Gemini: multiple groups
                await self._parse_multi_group_variants(
                    page=page,
                    url=url,
                    parent=parent,
                    state_machine=state_machine,  # <-- ПЕРЕДАЕМ ЯВНО
                    variant_selectors=var_cfg,
                    base_title=title,
                    base_sku=sku,
                    base_price=fact_price,
                    base_old_price=old_price,
                    base_currency=currency,
                    base_available=available,
                    base_image=main_img,
                    product_id=product_id,
                    extra_product_ids=extra_product_ids
                )
            else:
                # Старый плоский формат Ollama
                await self._parse_flat_variants(
                    page=page,
                    url=url,
                    parent=parent,
                    state_machine=state_machine,  # <-- ПЕРЕДАЕМ ЯВНО
                    var_cfg=var_cfg,
                    base_title=title,
                    base_sku=sku,
                    base_price=fact_price,
                    base_old_price=old_price,
                    base_currency=currency,
                    base_available=available,
                    base_image=main_img,
                    product_id=product_id,
                    extra_product_ids=extra_product_ids
                )

        # === ФИНАЛЬНАЯ САНИТИЗАЦИЯ (ОДИН РАЗ ПО ПОЛНОМУ СПИСКУ) ===
        if parent.variants:
            parent.sanitize_modification_attributes()
        # =========================================================

        logger.info(f"⚡ [FAST PARSER v2 DONE] Собраны данные РОДИТЕЛЯ для '{parent.title}' (ID: '{parent.product_id}')")
        return parent

    async def _capture_active_modifications(self, page: Page, var_cfg: dict, parent: ProductParent) -> dict:
        """
        Захватывает активные значения переключателей (radio:checked, select option[selected], button.active)
        до перехода на страницы вариантов. Возвращает dict {group_name: active_value}.
        """
        active_values = {}
        
        for group_name, cfg in var_cfg.items():
            container = str(cfg.get("container", "")).strip()
            item_sel = str(cfg.get("item", "")).strip()
            value_attr = str(cfg.get("value_attr", "title")).strip()
            
            if not container or not item_sel:
                continue
            
            try:
                # Определяем тип переключателя по селектору
                if "select" in item_sel.lower() or "option" in item_sel.lower():
                    # SELECT: читаем selected option
                    select_locator = page.locator(container).first
                    if await select_locator.count() > 0:
                        selected_option = select_locator.locator("option[selected], option:checked").first
                        if await selected_option.count() > 0:
                            val = await selected_option.get_attribute(value_attr) or await selected_option.inner_text()
                            active_values[group_name] = self._clean_text(val)
                elif "radio" in item_sel or "input" in item_sel:
                    # RADIO: читаем checked input
                    radio_locator = page.locator(f"{container} input[type='radio']:checked").first
                    if await radio_locator.count() > 0:
                        val = await radio_locator.get_attribute(value_attr) or await radio_locator.get_attribute("value")
                        active_values[group_name] = self._clean_text(val) if val else ""
                else:
                    # BUTTON/LINK: читаем активный элемент (.active, .selected)
                    active_item = page.locator(f"{container} .active, {container} .selected").first
                    if await active_item.count() > 0:
                        val = await active_item.get_attribute(value_attr) or await active_item.inner_text()
                        active_values[group_name] = self._clean_text(val)
            except Exception as e:
                logger.debug(f"⚠️ Не удалось захватить активное значение для {group_name}: {e}")
        
        return active_values
