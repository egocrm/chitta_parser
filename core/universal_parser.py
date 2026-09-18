import json
import logging
import re
import hashlib
import zlib
from typing import List, Dict, Any, Optional
from urllib.parse import urljoin
from playwright.async_api import Page
from core.models import ProductParent, ProductVariant
from core.state_machine import CatalogStateMachine
from core.category_extractor import CategoryExtractor

logger = logging.getLogger("ChittaParser")


class UniversalSemanticParser:
    """Детерминированное извлечение данных через Schema.org (JSON-LD) и interactive-обход."""

    def __init__(self, ollama_enricher=None):
        self.ollama_enricher = ollama_enricher
        self.category_extractor = CategoryExtractor()

    @staticmethod
    def clean_text(text: str) -> str:
        if not text:
            return ""
        clean = re.sub(r'<[^>]+>', ' ', str(text))
        return re.sub(r'\s+', ' ', clean).strip()

    @staticmethod
    def extract_clean_sku(raw_sku: str, fallback_url: str) -> str:
        clean = re.sub(r'[^\w\-]', '', str(raw_sku))
        if clean and len(clean) >= 3:
            return clean
        url_match = re.search(r'/(\d+)|-(\d+)', fallback_url)
        if url_match:
            return url_match.group(1) or url_match.group(2)
        return clean or str(abs(hash(fallback_url)))

    @staticmethod
    def _make_absolute_url(raw_url: str, base_url: str) -> str:
        if not raw_url:
            return ""
        clean_url = str(raw_url).strip()
        if clean_url.startswith("//"):
            return "https:" + clean_url
        return urljoin(base_url, clean_url)

    @staticmethod
    def _generate_cat_id(name: str, link: str = "") -> str:
        """Делегирует генерацию ID единому модулю CategoryExtractor."""
        return CategoryExtractor._generate_cat_id(name, link)      

    @staticmethod
    def _extract_url_candidate(url: str) -> str:
        """Извлекает потенциальный ID из URL по паттернам CMS/маркетплейсов или числовым хвостам (>= 3 цифр)."""
        if not url:
            return ""

        clean_url = url.split("#")[0]

        # 1. Query-параметры
        query_match = re.search(
            r'[?&](?:product_id|id_product|ELEMENT_ID|element_id|goods_id|item_id|offer_id|variant_id|variant|pid|product|prod|item|p|v|sku)=(\w+)',
            clean_url,
            re.IGNORECASE
        )
        if query_match:
            return query_match.group(1)

        # 2. Известные паттерны CMS и маркетплейсов
        prom_match = re.search(r'/(?:p|g)(\d+)(?:-|\.html|$)', clean_url, re.IGNORECASE)
        if prom_match:
            return prom_match.group(1)

        presta_match = re.search(r'/(\d+)-[a-zA-Z0-9-]+\.html', clean_url)
        if presta_match:
            return presta_match.group(1)

        horoshop_match = re.search(r'/(?:products/[^/]+-p|p)(\d+)/?$', clean_url, re.IGNORECASE)
        if horoshop_match:
            return horoshop_match.group(1)

        # 3. Эвристический цифровой хвост (строго от 3 цифр и больше, отсекает -1, -2)
        tail_match = re.search(r'(?:-(\d{3,})|/(\d{3,}))/?$', clean_url)
        if tail_match:
            return tail_match.group(1) or tail_match.group(2)

        return ""

    async def _get_all_page_ids(self, page: Page) -> set:
        """Глобально собирает ВСЕ возможные ID со страницы (из Schema.org и всех DOM-атрибутов)."""
        try:
            ids = await page.evaluate("""
                () => {
                    const found = new Set();
                    const attrNames = [
                        'data-product-id', 'data-product_id', 'data-productid', 'data-product-code',
                        'data-id', 'data-goods-id', 'data-goods_id', 'data-offer-id', 'data-offer_id',
                        'data-item-id', 'data-item_id'
                    ];

                    for (let attr of attrNames) {
                        document.querySelectorAll(`[${attr}]`).forEach(el => {
                            const val = el.getAttribute(attr);
                            if (val && val.trim() && val.trim() !== '0') found.add(val.trim());
                        });
                    }

                    const inputNames = ['product_id', 'product-id', 'product_id[]', 'add-to-cart', 'id', 'goods_id', 'variant_id'];
                    for (let name of inputNames) {
                        document.querySelectorAll(`form input[name="${name}"], input[name="${name}"]`).forEach(input => {
                            if (input.value && input.value.trim() && input.value.trim() !== '0') found.add(input.value.trim());
                        });
                    }
                    return Array.from(found);
                }
            """)
            page_ids = set(ids) if ids else set()

            # Добавляем ID из Schema.org JSON-LD
            scripts = await page.locator('script[type="application/ld+json"]').all_inner_texts()
            for script_text in scripts:
                try:
                    data = json.loads(script_text)
                    items = self._get_json_ld_items(data)
                    for item in items:
                        if isinstance(item, dict) and item.get("@type") in ["Product", "IndividualProduct"]:
                            spid = str(item.get("productID") or item.get("g:id") or "").strip()
                            if spid:
                                page_ids.add(spid)
                except Exception:
                    continue

            return page_ids
        except Exception:
            return set()

    async def resolve_product_id(self, page: Page, url: str, fallback_sku: str = "") -> str:
        """Кросс-валидация: URL + Глобальные ID -> Локальная Зона Покупки -> Fallback на SKU."""
        url_candidate = self._extract_url_candidate(url)
        all_page_ids = await self._get_all_page_ids(page)

        # 1. 100% Совпадение URL-кандидата с одним из системных ID страницы
        if url_candidate and url_candidate in all_page_ids:
            logger.info(f"🎯 [PID MATCH] ID '{url_candidate}' подтвержден 100% (найден в URL и сопоставлен с ID страницы).")
            return url_candidate

        if url_candidate:
            logger.info(f"🔍 [PID MISMATCH] Кандидат из URL '{url_candidate}' отсутствуют в списках ID страницы. Переход к локальной зоне покупки.")

        # 2. Локальный поиск в Главной Зоне Покупки DOM
        try:
            zone_id = await page.evaluate("""
                () => {
                    const junkSelectors = [
                        '[class*="recommend" i]', '[class*="similar" i]', '[class*="carousel" i]',
                        '[class*="slider" i]', '[class*="related" i]', '[data-qaid*="recommend" i]',
                        '[data-qaid="qa_product_tile"]', 'header', 'footer', 'aside', 'nav'
                    ];
                    const isJunk = (el) => junkSelectors.some(sel => !!el.closest(sel));

                    const mainContainer = document.querySelector('main, article, [data-qaid="product_info"], form[action*="cart"], form[action*="buy"]') || document.body;

                    const attrNames = ['data-product-id', 'data-product_id', 'data-productid', 'data-id', 'data-goods-id'];
                    for (let attr of attrNames) {
                        for (let el of mainContainer.querySelectorAll(`[${attr}]`)) {
                            if (isJunk(el)) continue;
                            const val = el.getAttribute(attr);
                            if (val && val.trim() && val.trim() !== '0') return val.trim();
                        }
                    }

                    const inputNames = ['product_id', 'product-id', 'add-to-cart', 'id', 'goods_id'];
                    for (let name of inputNames) {
                        const input = mainContainer.querySelector(`form input[name="${name}"], input[name="${name}"]`);
                        if (input && !isJunk(input) && input.value && input.value.trim() && input.value.trim() !== '0') {
                            return input.value.trim();
                        }
                    }
                    return null;
                }
            """)
            if zone_id:
                # logger.info(f"🎯 [PID ZONE] Извлечен системный ID из зоны покупки: '{zone_id}'")
                return str(zone_id).strip()
        except Exception:
            pass

        # 3. Фолбэк на SKU
        if fallback_sku:
            logger.info(f"📌 [PID FALLBACK] Используется SKU товара как product_id: '{fallback_sku}'")
            return fallback_sku

        return ""

    async def _extract_active_dom_image(self, page: Page) -> str:
        selectors = [
            '[data-qaid="main_image"] img',
            '[data-qaid="main_image"]',
            '[data-qaid="image_preview"] img',
            '.b-product-picture__image img',
            '.product-main-image img',
            '.gallery-image.active',
            'main [itemprop="image"]',
            'article [itemprop="image"]',
        ]
        junk_keywords = ["logo", "banner", "icon", "avatar", "header", "footer", "sprite"]

        for sel in selectors:
            try:
                locator = page.locator(sel)
                count = await locator.count()
                if count > 0:
                    for i in range(count):
                        el = locator.nth(i)
                        is_valid_context = await el.evaluate(
                            "node => !!node.closest('main, article, [data-qaid=\"product_info\"], .b-product') && !node.closest('header, footer, nav, .header, .footer')"
                        )
                        if not is_valid_context:
                            continue

                        src = (
                            await el.get_attribute("src") 
                            or await el.get_attribute("data-src") 
                            or await el.get_attribute("href")
                        )
                        if not src:
                            continue

                        src = src.strip()
                        if src.startswith("//"):
                            src = "https:" + src

                        if any(kw in src.lower() for kw in junk_keywords):
                            continue

                        if CatalogStateMachine.is_valid_image_url(src):
                            return src
            except Exception:
                continue
        return ""

    async def extract_product_links(self, page: Page) -> List[str]:
        clean_links = []
        current_page_url = page.url.split("?")[0].split("#")[0].rstrip('/')

        # 1. Schema.org
        try:
            scripts = await page.locator('script[type="application/ld+json"]').all_inner_texts()
            for script_text in scripts:
                data = json.loads(script_text)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict) and item.get("@type") in ["ItemList", "OfferCatalog"]:
                        for list_item in item.get("itemListElement", []):
                            url = list_item.get("url") or list_item.get("item", {}).get("url")
                            if url and url not in clean_links:
                                clean_links.append(url.split("?")[0])
        except Exception:
            pass

        if clean_links:
            logger.info(f"🔗 [UNIVERSAL LINKS] Найдено {len(clean_links)} ссылок через Schema.org")
            return clean_links

        # 2. Microdata
        try:
            microdata_links = await page.eval_on_selector_all(
                '[itemtype*="Product"] a[itemprop="url"], [itemscope] a[itemprop="url"]',
                'els => els.map(el => el.href)'
            )
            for href in microdata_links:
                clean_url = href.split("?")[0].split("#")[0].rstrip('/')
                if clean_url and clean_url != current_page_url and clean_url not in clean_links:
                    clean_links.append(clean_url)
        except Exception:
            pass

        if clean_links:
            logger.info(f"🔗 [UNIVERSAL LINKS] Найдено {len(clean_links)} ссылок через Microdata")
            return clean_links

        # 3. URL patterns (Prom / Shopify / WooCommerce)
        raw_hrefs = await page.eval_on_selector_all('a[href]', 'els => els.map(el => el.href)')
        product_patterns = re.compile(
            r'/(?:products?|items?|goods|product-item)/|/(?:[a-z]{2}/)?products?/|/(?:[a-z]{2}/)?p\d+|-p\d+|_p\d+|\.html$', 
            re.IGNORECASE
        )
        system_exclude = re.compile(
            r'/(?:cart|checkout|search|account|blogs|pages|contact|privacy|terms|about|delivery|payment|shipping|login|register|wishlist|compare)/', 
            re.IGNORECASE
        )

        for href in raw_hrefs:
            clean_url = href.split("?")[0].split("#")[0].rstrip('/')
            if product_patterns.search(clean_url) and not system_exclude.search(clean_url):
                if clean_url != current_page_url and clean_url not in clean_links:
                    clean_links.append(clean_url)

        if clean_links:
            logger.info(f"🔗 [UNIVERSAL LINKS] Найдено {len(clean_links)} ссылок по паттернам URL")
            return clean_links

        # 4. Эврика карточек
        heuristic_links = await page.evaluate(r"""
            (currentPageUrl) => {
                const links = new Set();
                const systemExclude = /(cart|checkout|search|account|blogs|pages|contact|privacy|terms|about|delivery|payment|shipping|login|register|wishlist|compare|instagram|facebook|viber|telegram|youtube|tel:|mailto:)/i;
                const allLinks = Array.from(document.querySelectorAll('main a[href], article a[href], .catalog a[href], .products a[href], .grid a[href], body a[href]'));

                for (const a of allLinks) {
                    const href = a.href ? a.href.split('?')[0].split('#')[0].replace(/\/+$/, '') : '';
                    if (!href || href === currentPageUrl || systemExclude.test(href)) continue;
                    
                    const card = a.closest('li, article, [class*="product" i], [class*="item" i], [class*="card" i], [class*="grid" i] > div');
                    if (card) {
                        const hasImg = !!card.querySelector('img');
                        const hasPrice = /[0-9]+\s*(₴|грн|\$|€|руб|eur|usd)/i.test(card.textContent || '');
                        const hasBuyBtn = !!card.querySelector('button, input[type="submit"], [class*="buy" i], [class*="cart" i]');

                        if (hasImg && (hasPrice || hasBuyBtn)) {
                            links.add(href);
                        }
                    }
                }
                return Array.from(links);
            }
        """, current_page_url)        

        for href in heuristic_links:
            if href not in clean_links:
                clean_links.append(href)

        logger.info(f"🔗 [UNIVERSAL LINKS] Найдено {len(clean_links)} ссылок через эвристику карточек товаров")
        return clean_links

    def _extract_all_prices_from_json(self, obj: Any) -> list[float]:
        found_prices = []
        ignored_subkeys = ["currency", "valid", "date", "until", "time"]

        if isinstance(obj, dict):
            for key, val in obj.items():
                key_lower = str(key).lower()
                if "price" in key_lower and not any(sub in key_lower for sub in ignored_subkeys):
                    if isinstance(val, (int, float)) and val > 0:
                        found_prices.append(float(val))
                    elif isinstance(val, str):
                        clean_str = val.replace(',', '.').strip()
                        if re.match(r'^\d+(?:\.\d+)?$', clean_str):
                            num = float(clean_str)
                            if num > 0:
                                found_prices.append(num)

                if isinstance(val, (dict, list)):
                    found_prices.extend(self._extract_all_prices_from_json(val))

        elif isinstance(obj, list):
            for item in obj:
                found_prices.extend(self._extract_all_prices_from_json(item))

        return found_prices

    def _parse_json_ld_offers(self, offers: Any) -> tuple:
        price = 0.0
        fact_price = 0.0
        availability_raw = ""

        if not offers:
            return price, fact_price, availability_raw

        offers_list = offers if isinstance(offers, list) else [offers]

        for offer in offers_list:
            if isinstance(offer, dict) and offer.get("availability"):
                availability_raw = str(offer.get("availability")).split("/")[-1].strip()
                break

        all_prices = self._extract_all_prices_from_json(offers_list)

        if all_prices:
            min_price = min(all_prices)
            max_price = max(all_prices)

            fact_price = min_price
            price = max_price if max_price > min_price else min_price

        return price, fact_price, availability_raw

    async def _get_universal_product_container(self, page: Page, parent: ProductParent):
        try:
            title_text = parent.title[:25].strip() if parent.title else ""
            price_digits = re.sub(r'[^\d]', '', str(int(parent.fact_price))) if parent.fact_price > 0 else ""

            await page.evaluate(
                """
                ([titleText, priceDigits]) => {
                    if (!titleText) return;
                    const clean = (str) => (str || '').replace(/[\\s\\u00a0]+/g, ' ').trim();
                    
                    const isOverlayOrDialog = (el) => {
                        if (!el) return false;
                        const role = el.getAttribute('role');
                        const ariaHidden = el.getAttribute('aria-hidden');
                        if (role === 'dialog' || role === 'alertdialog') return true;
                        if (ariaHidden === 'true') return true;
                        if (['HEADER', 'FOOTER', 'NAV'].includes(el.tagName)) return true;
                        return false;
                    };

                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    let titleNode = null;
                    while (walker.nextNode()) {
                        if (clean(walker.currentNode.nodeValue).includes(titleText)) {
                            titleNode = walker.currentNode.parentElement;
                            break;
                        }
                    }
                    if (!titleNode) titleNode = document.querySelector('h1') || document.body;
                    
                    let current = titleNode;
                    while (current && current !== document.body && current !== document.documentElement) {
                        const txt = clean(current.textContent);
                        const txtDigits = txt.replace(/[^0-9]/g, '');
                        const hasPrice = priceDigits ? txtDigits.includes(priceDigits) : true;
                        const hasBuyForm = !!current.querySelector('button, input[type="submit"], form, [class*="cart" i], [class*="buy" i], [data-qaid*="buy" i]');
                        
                        if (hasPrice && hasBuyForm && !isOverlayOrDialog(current)) {
                            window.__universal_product_container = current;
                            return;
                        }
                        current = current.parentElement;
                    }
                    
                    current = titleNode.parentElement || document.body;
                    while (current && current !== document.body && clean(current.textContent).length < 300) {
                        if (isOverlayOrDialog(current)) {
                            current = current.parentElement;
                            continue;
                        }
                        current = current.parentElement;
                    }
                    window.__universal_product_container = current || document.body;
                }
                """,
                [title_text, price_digits]
            )
            return await page.evaluate_handle("() => window.__universal_product_container || document.body")
        except Exception:
            return await page.evaluate_handle("() => document.body")

    async def _extract_dom_prices_fallback(self, page: Page, parent: ProductParent) -> None:
        try:
            container_handle = await self._get_universal_product_container(page, parent)
            candidates_log = await container_handle.evaluate("""
                (container, currentPrice) => {
                    if (!container) return { candidates: [], accepted: null };
                    const selectors = [
                        'del', 's', '[style*="line-through"]',
                        '[class*="old" i]', '[class*="crossed" i]', '[class*="original" i]', '[class*="compare" i]', '[class*="strike" i]',
                        '[data-qaid*="old" i]', '[data-qaid*="price" i]', '[data-qaprice]'
                    ];
                    const elements = Array.from(container.querySelectorAll(selectors.join(',')));
                    const logList = [];
                    let accepted = null;

                    for (let i = 0; i < elements.length; i++) {
                        const el = elements[i];
                        const isInsideCarousel = !!el.closest('ul, ol, .swiper-slide, .slick-slide, [class*="carousel" i], [class*="slider" i]');
                        const style = window.getComputedStyle(el);
                        const isLineThrough = style.textDecorationLine.includes('line-through') || style.textDecoration.includes('line-through') || el.tagName === 'DEL' || el.tagName === 'S';
                        const rawText = (el.textContent || "").trim();
                        const cleanText = rawText.replace(',', '.').replace(/[^0-9.]/g, '');
                        const val = parseFloat(cleanText);

                        const itemLog = { index: i + 1, tag: el.tagName.toLowerCase(), className: el.className, rawText: rawText, parsedValue: isNaN(val) ? 0.0 : val, isLineThrough: isLineThrough, isCarousel: isInsideCarousel };
                        logList.push(itemLog);

                        if (!accepted && !isNaN(val) && val > currentPrice && !isInsideCarousel && isLineThrough) {
                            accepted = itemLog;
                        }
                    }
                    return { candidates: logList, accepted: accepted };
                }
            """, parent.fact_price)

            accepted = candidates_log.get("accepted")
            if accepted:
                parent.price = float(accepted["parsedValue"])
                # logger.info(f"⚡ [FAST-PATH UNIVERSAL] Установлена старая цена: {parent.price}")

            # Бонус
            bonus_data = await container_handle.evaluate("""
                (container) => {
                    if (!container) return "";
                    const candidates = container.querySelectorAll('[class*="discount" i], [class*="badge" i], [class*="save" i], [class*="percent" i], [class*="sale" i], [data-qaid*="discount" i]');
                    for (let el of candidates) {
                        const txt = (el.textContent || "").trim();
                        if (txt && (txt.includes('%') || txt.includes('-'))) return txt;
                    }
                    return "";
                }
            """)
            if bonus_data:
                parent.bonus = self.clean_text(bonus_data)

            if not parent.bonus and parent.price > parent.fact_price:
                discount_pct = round((1.0 - (parent.fact_price / parent.price)) * 100)
                if discount_pct > 0:
                    parent.bonus = f"-{discount_pct}%"

            # Опт
            wholesale_data = await container_handle.evaluate("""
                (container) => {
                    if (!container) return "";
                    const candidates = container.querySelectorAll(
                        '[class*="wholesale" i], [class*="bulk" i], [class*="tier" i], [class*="quantity" i], [class*="hurt" i], [data-qaid*="wholesale" i]'
                    );

                    for (let el of candidates) {
                        const txt = (el.textContent || "").trim();
                        if (!txt || txt.length > 150) continue;

                        const isTrigger = el.tagName === 'BUTTON' ||
                                          el.hasAttribute('onclick') ||
                                          el.hasAttribute('data-toggle') ||
                                          el.hasAttribute('aria-expanded') ||
                                          el.hasAttribute('aria-controls') ||
                                          (el.tagName === 'A' && (el.getAttribute('href') || '').startsWith('javascript:'));

                        const hasDigits = /\\d/.test(txt);
                        if (isTrigger || !hasDigits) continue;

                        return txt;
                    }
                    return "";
                }
            """)
            if wholesale_data:
                parent.sales_notes = self.clean_text(wholesale_data)
                # logger.info(f"⚡ [FAST-PATH UNIVERSAL] Оптовые условия: '{parent.sales_notes}'")

            # Наличие
            presence_data = await container_handle.evaluate("""
                (container) => {
                    if (!container) return "";
                    const candidates = container.querySelectorAll('[itemprop="availability"], [class*="stock" i], [class*="presence" i], [class*="availability" i], [data-qaid*="presence" i]');
                    for (let el of candidates) {
                        const txt = (el.textContent || "").trim();
                        if (txt && txt.length < 50) return txt;
                    }
                    return "";
                }
            """)
            if presence_data and not parent.available:
                parent.available = self.clean_text(presence_data)

        except Exception as e:
            logger.debug(f"⚠️ Ошибка DOM Fast-Path: {e}")

    # def _parse_json_ld_breadcrumbs(self, scripts: List[str], state_machine: CatalogStateMachine, page_url: str = "") -> tuple:
    #     cat_id, cat_name, cat_link = "", "", ""
    #     try:
    #         for script_text in scripts:
    #             data = json.loads(script_text)
    #             items = self._get_json_ld_items(data)
    #             for item in items:
    #                 if isinstance(item, dict) and item.get("@type") == "BreadcrumbList":
    #                     elements = item.get("itemListElement", [])
    #                     if not elements:
    #                         continue

    #                     sorted_els = sorted(
    #                         elements, 
    #                         key=lambda x: int(x.get("position", 0)) if str(x.get("position", "0")).isdigit() else 0
    #                     )                        
    #                     crumbs = []
    #                     for el in sorted_els:
    #                         c_name = self.clean_text(el.get("name") or el.get("item", {}).get("name", ""))
    #                         raw_c_link = el.get("item") if isinstance(el.get("item"), str) else el.get("item", {}).get("@id", "") or el.get("url", "")
    #                         c_link = self._make_absolute_url(raw_c_link, page_url) if page_url else raw_c_link
    #                         if c_name:
    #                             crumbs.append({"name": c_name, "link": c_link})

    #                     if crumbs and crumbs[0]["name"].lower() in ["головна", "главная", "home", "main"]:
    #                         crumbs.pop(0)

    #                     if not crumbs:
    #                         continue

    #                     category_crumbs = crumbs[:-1] if len(crumbs) > 1 else crumbs

    #                     current_parent_id = "0"
    #                     for c in category_crumbs:
    #                         c_id = self._generate_cat_id(c["name"], c["link"])
    #                         state_machine.add_category(cat_id=c_id, name=c["name"], parent_id=current_parent_id)
    #                         current_parent_id = c_id
    #                         cat_id, cat_name, cat_link = c_id, c["name"], c["link"]

    #                     if cat_id:
    #                         logger.info(f"📂 [SCHEMA] Категория товара: {cat_name} (ID: {cat_id})")
    #                         return cat_id, cat_name, cat_link

    #     except Exception as e:
    #         logger.debug(f"⚠️ Ошибка разбора хлебных крошек: {e}")

    #     return cat_id, cat_name, cat_link

    # async def _extract_dom_breadcrumbs_fallback(self, page: Page, state_machine: CatalogStateMachine, page_url: str = "") -> tuple:
    #     try:
    #         crumb_locators = page.locator('[data-qaid="breadcrumbs"] a, .b-breadcrumb__item a, .breadcrumbs a, ul.breadcrumb li a')
    #         count = await crumb_locators.count()
    #         if count > 0:
    #             crumbs = []
    #             for i in range(count):
    #                 el = crumb_locators.nth(i)
    #                 name = self.clean_text(await el.inner_text())
    #                 raw_link = await el.get_attribute("href") or ""
    #                 link = self._make_absolute_url(raw_link, page_url) if page_url else raw_link
    #                 if name and name.lower() not in ["головна", "главная", "home"]:
    #                     crumbs.append({"name": name, "link": link})
                
    #             if crumbs:
    #                 category_crumbs = crumbs[:-1] if len(crumbs) > 1 else crumbs
    #                 current_parent_id = "0"
    #                 cat_id, cat_name, cat_link = "", "", ""
    #                 for c in category_crumbs:
    #                     c_id = self._generate_cat_id(c["name"], c["link"])
    #                     state_machine.add_category(cat_id=c_id, name=c["name"], parent_id=current_parent_id)
    #                     current_parent_id = c_id
    #                     cat_id, cat_name, cat_link = c_id, c["name"], c["link"]
    #                 return cat_id, cat_name, cat_link
    #     except Exception:
    #         pass
    #     return "", "", ""

    async def _extract_variant_links_from_dom(self, page: Page, current_url: str) -> List[dict]:
        """Искать текст опции исключительно по чистым DOM-узлам без использования атрибутов."""
        try:
            try:
                await page.wait_for_selector("main, article, [class*='product'], [class*='char']", timeout=5000)
            except Exception:
                pass

            raw_variant_links = await page.evaluate("""
                (pageUrl) => {
                    const clean = (str) => (str || '').replace(/[\\s\\u00a0]+/g, ' ').trim();
                    const currentCleanUrl = pageUrl.split('#')[0].split('?')[0];

                    // Чистый алгоритм обхода текстовых узлов (без проверки атрибутов)
                    const extractCleanText = (el) => {
                        if (!el) return '';
                        const findFirstText = (node) => {
                            if (!node || node.nodeType !== 1) return '';
                            
                            // 1. Ищем прямые текстовые узлы внутри текущего тега
                            let directText = '';
                            for (let child of node.childNodes) {
                                if (child.nodeType === 3) {
                                    const t = clean(child.textContent);
                                    if (t) directText += (directText ? ' ' : '') + t;
                                }
                            }
                            if (directText) return directText;
                            
                            // 2. Если прямых текстовых узлов нет — рекурсивно заходим строго в первый дочерний тег
                            if (node.firstElementChild) {
                                return findFirstText(node.firstElementChild);
                            }
                            
                            return '';
                        };

                        return findFirstText(el) || clean(el.textContent);
                    };

                    const h1El = document.querySelector('h1');
                    const mainTitle = clean(h1El ? h1El.textContent : document.title).toLowerCase();
                    const titleWords = mainTitle.replace(/[^\\w\\u0400-\\u04FF]/g, ' ').split(/\\s+/).filter(w => w.length > 2);

                    const mainArea = document.querySelector('main, article, [class*="product-page" i], [class*="product-detail" i]') || document.body;

                    const excludeSelectors = [
                        'header', 'footer', 'nav',
                        '[class*="recommend" i]', '[class*="related" i]', '[class*="similar" i]',
                        '[class*="slider" i]', '[class*="carousel" i]', '[class*="bought" i]',
                        '[class*="cross" i]', '[class*="upsell" i]', '[class*="viewed" i]',
                        '[class*="popular" i]', '[id*="recommend" i]', '[id*="related" i]'
                    ];

                    const allLinks = Array.from(mainArea.querySelectorAll('a[href]'));
                    const results = [];
                    const seenUrls = new Set();

                    const currentSlugParts = currentCleanUrl.split('/').filter(Boolean).pop().split('-');

                    for (const a of allLinks) {
                        if (a.closest(excludeSelectors.join(','))) continue;

                        const href = a.href ? a.href.split('#')[0] : '';
                        const hrefClean = href ? href.split('?')[0] : '';

                        if (!href || hrefClean === currentCleanUrl || seenUrls.has(hrefClean)) continue;
                        
                        // Игнорируем спец-схемы (tel:, mailto:, javascript:, viber: и т.д.)
                        if (/^(tel:|mailto:|javascript:|viber:|whatsapp:|tg:)/i.test(href.trim())) continue;

                        // Игнорируем прямые файлы изображений, архивов и документов
                        if (/\\.(jpg|jpeg|png|webp|gif|svg|avif|bmp|pdf|zip|rar|7z|doc|docx|xls|xlsx)$/i.test(hrefClean)) continue;

                        if (href.includes('sc_content') || href.includes('/category-') || href.includes('/cart') || href.includes('/checkout')) continue;

                        const valText = extractCleanText(a);

                        const insideOptionWrapper = !!a.closest('[class*="option" i], [class*="variant" i], [class*="color" i], [class*="mod" i], [class*="swatch" i], [class*="offer" i], [class*="param" i], [class*="attr" i]');
                        let isMatch = insideOptionWrapper;

                        if (!isMatch && hrefClean) {
                            const candidateSlugParts = hrefClean.split('/').filter(Boolean).pop().split('-');
                            if (currentSlugParts.length > 3 && candidateSlugParts.length > 3) {
                                const commonParts = candidateSlugParts.filter(p => currentSlugParts.includes(p));
                                if ((commonParts.length / currentSlugParts.length) >= 0.6) {
                                    isMatch = true;
                                }
                            }
                        }

                        if (!isMatch && valText && titleWords.length > 3) {
                            const linkWords = valText.toLowerCase().replace(/[^\\w\\u0400-\\u04FF]/g, ' ').split(/\\s+/).filter(w => w.length > 2);
                            if (linkWords.length > 2) {
                                const commonWords = linkWords.filter(w => titleWords.includes(w));
                                if ((commonWords.length / linkWords.length) >= 0.5) {
                                    isMatch = true;
                                }
                            }
                        }

                        if (isMatch) {
                            const getLabelFromContainer = (cNode) => {
                                if (!cNode) return '';
                                const l = cNode.querySelector('label, [class*="label" i], [class*="title" i], [class*="name" i], legend, [class*="heading" i], [id*="attr" i]');
                                if (l) {
                                    const txt = clean(l.textContent);
                                    if (txt && txt.length < 50) return txt.replace(/[:\\s]+$/, '');
                                }
                                return '';
                            };

                            const getKeywordFromClasses = (cNode) => {
                                if (!cNode) return '';
                                const str = ((cNode.className || '') + ' ' + (cNode.id || '')).toLowerCase();
                                if (str.includes('color')) return 'color';
                                if (str.includes('size')) return 'size';
                                if (str.includes('volume') || str.includes('capacity')) return 'volume';
                                if (str.includes('memory') || str.includes('storage') || str.includes('ram') || str.includes('rom')) return 'memory';
                                return '';
                            };

                            let groupName = '';
                            let currNode = a.parentElement;
                            for (let depth = 0; depth < 4 && currNode && currNode !== mainArea && currNode !== document.body; depth++) {
                                groupName = getLabelFromContainer(currNode) || getKeywordFromClasses(currNode);
                                if (!groupName && currNode.previousElementSibling) {
                                    groupName = getLabelFromContainer(currNode.previousElementSibling) || getKeywordFromClasses(currNode.previousElementSibling);
                                }
                                if (groupName) break;
                                currNode = currNode.parentElement;
                            }

                            if (!groupName) {
                                const container = a.closest('[class*="mod" i], [class*="option" i], [class*="group" i], [class*="prop" i], [class*="color" i], [class*="size" i], [class*="switch" i], div, section');
                                if (container) {
                                    const allGroups = Array.from(mainArea.querySelectorAll('[class*="mod" i], [class*="option" i], [class*="group" i], [class*="prop" i], [class*="color" i], [class*="size" i], [class*="switch" i]'));
                                    const parentGroups = Array.from(new Set(allGroups.map(g => g.closest('div, section, fieldset') || g)));
                                    const unnamed = parentGroups.filter(g => !getLabelFromContainer(g) && !getLabelFromContainer(g.previousElementSibling));
                                    const gIdx = unnamed.indexOf(container);
                                    if (gIdx !== -1 && unnamed.length > 1) {
                                        groupName = 'option_' + (gIdx + 1);
                                    }
                                }
                            }

                            if (!groupName) groupName = 'option';
                            seenUrls.add(hrefClean);
                            let cleanVal = valText || '';
                            if (cleanVal.length > 30 || /[0-9]+\\s*(грн|₴|\\$|€)/i.test(cleanVal) || /переглянути|купити|buy/i.test(cleanVal)) {
                                cleanVal = '';
                            }

                            // Если имя группы совпало со значением (например, black:black), нормализуем имя группы
                            let finalGroup = groupName;
                            if (finalGroup.toLowerCase() === cleanVal.toLowerCase() || ['green','blue','red','black','white','pink','yellow','violet','gold','tiffany','champagne','olive'].includes(finalGroup.toLowerCase())) {
                                finalGroup = 'color';
                            }

                            results.push({
                                url: href,
                                group_name: finalGroup,
                                value: cleanVal || 'variant'
                            });
                        }
                    }
                    return results;
                }
            """, current_url)

            media_extensions = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.svg', '.avif', '.pdf', '.zip', '.rar')
            variant_links = []
            for item in raw_variant_links:
                abs_url = self._make_absolute_url(item["url"], current_url)
                clean_abs = abs_url.split('#')[0].split('?')[0].lower() if abs_url else ""
                
                if (
                    abs_url 
                    and not abs_url.startswith(("tel:", "mailto:", "javascript:", "viber:", "whatsapp:", "tg:"))
                    and not clean_abs.endswith(media_extensions)
                    and clean_abs != current_url.split('#')[0].split('?')[0].lower()
                ):
                    variant_links.append({
                        "url": abs_url,
                        "group_name": self.clean_text(item["group_name"]) or "option",
                        "value": self.clean_text(item["value"])
                    })

            if variant_links:
                logger.info(f"🔗 [VARIANT LINKS] Найдено {len(variant_links)} валидных ссылок на смежные варианты.")

            return variant_links

        except Exception as e:
            logger.debug(f"⚠️ Ошибка извлечения ссылок вариантов: {e}")
            return []

    async def _extract_link_variants(
        self, 
        page: Page, 
        current_url: str, 
        state_machine: CatalogStateMachine, 
        parent_product_id: str,
        fast_parser: Optional[Any] = None,
        selectors_map: Optional[dict] = None
    ) -> None:
        """Обходит страницы вариантов в изолированных браузерных контекстах v_context."""
        if parent_product_id not in state_machine.parents:
            return
        parent = state_machine.parents[parent_product_id]

        variant_links = await self._extract_variant_links_from_dom(page, current_url)
        if not variant_links:
            return

        browser = page.context.browser
        parent_cookies = await page.context.cookies()

        for v_info in variant_links:
            v_url = v_info["url"]
            v_context = None
            v_page = None
            try:
                logger.info(f"===== 🌐 [VARIANT PAGE] Открытие варианта в новом контексте: {v_url}")
                if browser:
                    v_context = await browser.new_context()
                    if parent_cookies:
                        await v_context.add_cookies(parent_cookies)
                    v_page = await v_context.new_page()
                else:
                    v_page = await page.context.new_page()

                await v_page.goto(v_url, wait_until="domcontentloaded", timeout=20000)
                try:
                    await v_page.wait_for_selector("h1, main, article, [itemprop='name']", timeout=2000)
                except Exception:
                    pass

                # 1. ПЕРВИЧНЫЙ СЛОЙ v2: Первым делом пробуем снять точные данные через FastSelectorParser (карта Gemini)
                v2_product = None
                if fast_parser and selectors_map:
                    try:
                        v2_product = await fast_parser.extract_product(
                            page=v_page, 
                            url=v_url, 
                            selectors_map=selectors_map, 
                            state_machine=state_machine
                        )
                    except Exception as v2_err:
                        logger.debug(f"⚠️ [VARIANT v2 WARN] Ошибка съема по селекторам для {v_url}: {v2_err}")

                # if v2_product and parent.category_id and v2_product.category_id and parent.category_id != v2_product.category_id:
                #     logger.warning(f"⛔ [NOT A VARIANT] Пропуск ссылки {v_url}: Категория '{v2_product.category_name}' не совпадает с родителем '{parent.category_name}'.")
                #     continue

                if v2_product and parent.category_id and v2_product.category_id and parent.category_id != v2_product.category_id:
                    p_words = set(re.findall(r'\w{3,}', parent.title.lower()))
                    v_words = set(re.findall(r'\w{3,}', v2_product.title.lower()))
                    common_words = p_words.intersection(v_words)
                    
                    # Если категории разные И названия не имеют общих ключевых слов — отбрасываем левый товар из рекомендаций
                    if p_words and v_words and not common_words:
                        logger.warning(f"⛔ [NOT A VARIANT] Пропуск ссылки {v_url}: Категория '{v2_product.category_name}' и заголовок '{v2_product.title}' не соответствуют родителю '{parent.title}'.")
                        continue
                    else:
                        logger.warning(f"⚠️ [VARIANT CATEGORY MISMATCH] Категория варианта '{v2_product.category_name}' отличается от родителя, но заголовок схож. Обработка продолжается.")                

                # 2. ВТОРИЧНЫЙ СЛОЙ v1: Сбор Schema.org / DOM для страховки и добора
                v_product_id = v2_product.product_id if (v2_product and v2_product.product_id) else await self.resolve_product_id(v_page, v_url)
                if not v_product_id:
                    v_product_id = parent_product_id

                # === УМНАЯ ЗАЩИТА ОТ ПЕРЕЗАПИСИ ДАННЫХ V2 ===
                v_id_str = str(v_product_id).strip()
                parent_id_str = str(parent_product_id).strip()
                
                existing_variant = next((v for v in parent.variants if str(v.product_id) == v_id_str), None)
                if existing_variant or v_id_str == parent_id_str:
                    logger.info(f"✅ [V1 SKIP LINK] Вариант ID {v_id_str} уже корректно обработан через v2. Пропускаем.")
                    continue

                # ==================================================
                v_dom_title = await v_page.evaluate("""
                    () => {
                        const h1 = document.querySelector('h1');
                        if (h1 && h1.textContent.trim()) return h1.textContent.trim();
                        const og = document.querySelector('meta[property="og:title"], meta[name="og:title"]');
                        if (og && og.content.trim()) return og.content.trim();
                        return document.title ? document.title.trim() : '';
                    }
                """)
                v_title = (v2_product.title if v2_product and v2_product.title else None) or self.clean_text(v_dom_title) or parent.title

                v_sku = (v2_product.sku if v2_product and v2_product.sku else None) or parent.sku
                v_price = v2_product.price if (v2_product and v2_product.price > 0) else parent.price
                v_fact_price = v2_product.fact_price if (v2_product and v2_product.fact_price > 0) else parent.fact_price
                v_avail = (v2_product.available if v2_product and v2_product.available else None) or parent.available
                v_image = (v2_product.image if v2_product and v2_product.image else None) or await self._extract_active_dom_image(v_page) or parent.image

                # Извлечение JSON-LD данных со страницы варианта
                scripts = await v_page.locator('script[type="application/ld+json"]').all_inner_texts()
                for script_text in scripts:
                    try:
                        data = json.loads(script_text)
                        items = self._get_json_ld_items(data)
                        for item in items:
                            if isinstance(item, dict) and item.get("@type") in ["Product", "IndividualProduct"]:
                                schema_sku = self.extract_clean_sku(str(item.get("sku", "") or item.get("productID", "")), v_url)
                                if schema_sku:
                                    v_sku = schema_sku
                                p_val, fp_val, avail_raw = self._parse_json_ld_offers(item.get("offers"))
                                if fp_val > 0:
                                    v_fact_price = fp_val
                                    v_price = p_val if p_val > 0 else fp_val
                                if avail_raw:
                                    v_avail = avail_raw
                                images = item.get("image", [])
                                clean_v_imgs = []
                                if isinstance(images, list):
                                    for img in images:
                                        img_str = img if isinstance(img, str) else (img.get("url") if isinstance(img, dict) else "")
                                        if img_str:
                                            abs_url = self._make_absolute_url(img_str, v_url)
                                            if abs_url and abs_url not in clean_v_imgs:
                                                clean_v_imgs.append(abs_url)
                                elif isinstance(images, str) and images:
                                    abs_url = self._make_absolute_url(images, v_url)
                                    if abs_url:
                                        clean_v_imgs.append(abs_url)
                                if clean_v_imgs:
                                    v_image = ", ".join(clean_v_imgs)
                                break
                    except Exception:
                        continue

                # Базовый атрибут модификации из переключателя DOM
                g_name = self.clean_text(v_info.get("group_name", ""))
                val = self.clean_text(v_info.get("value", ""))

                # Фильтрация бессмысленных заглушек DOM
                if g_name.lower() in ["варіант", "variant", "option"]:
                    g_name = ""
                if val.lower() in ["variant", "варіант"]:
                    val = ""

                # Фолбэк-парсер опций из SKU/URL варианта (например, 80192-65-olive -> size:65, color:olive)
                if not g_name or not val:
                    target_sku = v_sku or self.extract_clean_sku("", v_url)
                    parts = re.split(r'[-_]', target_sku)
                    for part in parts:
                        p_clean = part.strip()
                        if p_clean.isdigit() and len(p_clean) in [2, 3]:  # Размер/Высота (55, 65, 75)
                            g_name = g_name or "size"
                            val = val or p_clean
                        elif p_clean.lower() in ["black", "blue", "green", "pink", "violet", "gold", "tiffany", "champagne", "olive", "red", "white", "yellow"]:
                            g_name = g_name or "color"
                            val = val or p_clean.lower()

                v_mod_attrs = {}
                if g_name and val:
                    v_mod_attrs[g_name] = val
                elif val:
                    v_mod_attrs["option"] = val

                variant = ProductVariant(
                    product_id=str(v_product_id).strip(),
                    parent_product_id=parent_product_id,
                    sku=v_sku,
                    parent_sku=parent.sku,
                    title=v_title,
                    description=parent.description,
                    price=v_price,
                    fact_price=v_fact_price,
                    available=v_avail,
                    image=v_image,
                    product_link=v_url,
                    category_id=parent.category_id,
                    category_name=parent.category_name,
                    category_link=parent.category_link,
                    modification_attributes=v_mod_attrs
                )

                state_machine.add_variant(parent_product_id, variant)

            except Exception as e:
                logger.warning(f"⚠️ [VARIANT PAGE FAILED] Ошибка обработки варианта {v_url}: {e}")
            finally:
                if v_page:
                    try:
                        await v_page.close()
                    except Exception:
                        pass
                if v_context:
                    try:
                        await v_context.close()
                    except Exception:
                        pass

    async def _extract_interactive_variants(self, page: Page, url: str, state_machine: CatalogStateMachine, parent_product_id: str) -> None:
        if parent_product_id not in state_machine.parents:
            return
        parent = state_machine.parents[parent_product_id]

        try:
            await page.wait_for_selector('.b-product-mods, [class*="mods__button"], [data-qaid="product_option"], [class*="swatch"]', timeout=3000)
        except Exception:
            return

        selectors = [
            '.b-product-mods__button',
            'span[class*="mods__button"]',
            'button[class*="variant"]',
            'button[class*="option"]',
            '[data-qaid="product_option"]',
            '[class*="swatch"]'
        ]

        for sel in selectors:
            try:
                count = await page.locator(sel).count()
            except Exception:
                count = 0
            if count > 0:
                for idx in range(count):
                    try:
                        current_el = page.locator(sel).nth(idx)
                        if await current_el.count() == 0 or not await current_el.is_visible():
                            continue

                        opt_text = self.clean_text(await current_el.inner_text())
                        if not opt_text: continue

                        # Попытка извлечь системный ID варианта напрямую из элемента до клика
                        v_product_id = await current_el.evaluate("""
                            el => el.getAttribute('data-product-id') || el.getAttribute('data-product_id') || 
                                  el.getAttribute('data-variant-id') || el.getAttribute('data-value') || 
                                  el.getAttribute('value') || el.getAttribute('data-id') || ''
                        """)

                        await current_el.click(force=True)
                        await page.wait_for_timeout(400)

                        current_variant_url = page.url
                        
                        # 1. Считывание оригинального H1/Title страницы варианта со страховочным каскадом
                        v_dom_title = await page.evaluate("""
                            () => {
                                const h1 = document.querySelector('h1');
                                if (h1 && h1.textContent.trim()) {
                                    return h1.textContent.trim();
                                }
                                const ogTitle = document.querySelector('meta[property="og:title"], meta[name="og:title"]');
                                if (ogTitle && ogTitle.content.trim()) {
                                    return ogTitle.content.trim();
                                }
                                return document.title ? document.title.trim() : '';
                            }
                        """)
                        v_title = self.clean_text(v_dom_title) if v_dom_title else parent.title

                        real_v_sku = parent.sku
                        v_price, v_fact_price = parent.price, parent.fact_price
                        v_avail = parent.available
                        v_image_json = ""

                        try:
                            scripts = await page.locator('script[type="application/ld+json"]').all_inner_texts()
                            for script_text in scripts:
                                data = json.loads(script_text)
                                items = data if isinstance(data, list) else [data]
                                for item in items:
                                    if isinstance(item, dict) and item.get("@type") in ["Product", "IndividualProduct"]:
                                        found_id = str(item.get("productID") or item.get("g:id") or "")
                                        if found_id: v_product_id = found_id

                                        found_sku = str(item.get("sku") or item.get("productID", ""))
                                        if found_sku: real_v_sku = found_sku

                                        p_val, fp_val, avail_raw = self._parse_json_ld_offers(item.get("offers"))
                                        if fp_val > 0:
                                            v_fact_price = fp_val
                                            v_price = p_val if p_val > 0 else fp_val
                                        if avail_raw: v_avail = avail_raw
                                        images = item.get("image", [])
                                        clean_v_imgs = []
                                        if isinstance(images, list):
                                            for img in images:
                                                img_str = img if isinstance(img, str) else (img.get("url") if isinstance(img, dict) else "")
                                                if img_str:
                                                    abs_url = self._make_absolute_url(img_str, current_variant_url)
                                                    if abs_url and abs_url not in clean_v_imgs:
                                                        clean_v_imgs.append(abs_url)
                                        elif isinstance(images, str) and images:
                                            abs_url = self._make_absolute_url(images, current_variant_url)
                                            if abs_url:
                                                clean_v_imgs.append(abs_url)
                                        if clean_v_imgs:
                                            v_image_json = ", ".join(clean_v_imgs)
                                        break
                        except Exception:
                            pass

                        # Фолбэк извлечения v_product_id из DOM после клика
                        if not v_product_id:
                            v_product_id = await self.resolve_product_id(page, current_variant_url)

                        if not v_product_id:
                            logger.warning(f"⚠️ [SKIP] Вариант '{opt_text}' пропущен [{current_variant_url}]: не найден системный product_id варианта.")
                            continue

                        if v_image_json:
                            v_image_json = self._make_absolute_url(v_image_json, current_variant_url)

                        if not v_image_json or v_image_json == parent.image:
                            dom_image = await self._extract_active_dom_image(page)
                            v_image = dom_image if dom_image else parent.image
                        else:
                            v_image = v_image_json

                        # Попытка извлечь понятное имя группы переключателей из DOM
                        group_name = await current_el.evaluate("""
                            el => {
                                const clean = (str) => (str || '').replace(/[\\s\\u00a0]+/g, ' ').trim();
                                const getLabelFromContainer = (cNode) => {
                                    if (!cNode) return '';
                                    const l = cNode.querySelector('label, [class*="label"], [class*="title"], [class*="name"], legend, [class*="heading"], [id*="attr"]');
                                    if (l) {
                                        const txt = clean(l.textContent);
                                        if (txt && txt.length < 50) return txt.replace(/[:\\s]+$/, '');
                                    }
                                    return '';
                                };

                                const getKeywordFromClasses = (cNode) => {
                                    if (!cNode) return '';
                                    const str = ((cNode.className || '') + ' ' + (cNode.id || '')).toLowerCase();
                                    if (str.includes('color')) return 'color';
                                    if (str.includes('size')) return 'size';
                                    if (str.includes('volume') || str.includes('capacity')) return 'volume';
                                    if (str.includes('memory') || str.includes('storage') || str.includes('ram') || str.includes('rom')) return 'memory';
                                    return '';
                                };

                                let foundName = '';
                                let currNode = el.parentElement;
                                for (let depth = 0; depth < 4 && currNode && currNode !== document.body; depth++) {
                                    foundName = getLabelFromContainer(currNode) || getKeywordFromClasses(currNode);
                                    if (!foundName && currNode.previousElementSibling) {
                                        foundName = getLabelFromContainer(currNode.previousElementSibling) || getKeywordFromClasses(currNode.previousElementSibling);
                                    }
                                    if (foundName) return foundName;
                                    currNode = currNode.parentElement;
                                }

                                const attrVal = el.getAttribute('aria-label') || el.getAttribute('data-title') || el.getAttribute('title');
                                if (attrVal && clean(attrVal)) return clean(attrVal);

                                const container = el.closest('[class*="mod"], [class*="option"], [class*="group"], [class*="prop"], [class*="switch"], div, section');
                                if (container) {
                                    const allGroups = Array.from(document.querySelectorAll('[class*="mod"], [class*="option"], [class*="group"], [class*="prop"], [class*="swatch"], [class*="switch"], [data-qaid="product_option"]'));
                                    const parentGroups = Array.from(new Set(allGroups.map(g => g.closest('div, section, fieldset') || g)));
                                    const unnamed = parentGroups.filter(g => !getLabelFromContainer(g) && !getLabelFromContainer(g.previousElementSibling));
                                    const gIdx = unnamed.indexOf(container);
                                    if (gIdx !== -1 && unnamed.length > 1) {
                                        return 'option_' + (gIdx + 1);
                                    }
                                }

                                return 'option';
                            }
                        """)
                        clean_group_name = self.clean_text(group_name)
                        clean_val = opt_text if opt_text.lower() not in ["variant", "варіант"] else ""

                        if clean_group_name.lower() in ["варіант", "variant", "option"]:
                            clean_group_name = ""

                        # Извлечение ключа и значения из SKU при отсутствии данных в DOM
                        if not clean_group_name or not clean_val:
                            parts = re.split(r'[-_]', real_v_sku)
                            for part in parts:
                                p_clean = part.strip()
                                if p_clean.isdigit() and len(p_clean) in [2, 3]:
                                    clean_group_name = clean_group_name or "size"
                                    clean_val = clean_val or p_clean
                                elif p_clean.lower() in ["black", "blue", "green", "pink", "violet", "gold", "tiffany", "champagne", "olive", "red", "white", "yellow"]:
                                    clean_group_name = clean_group_name or "color"
                                    clean_val = clean_val or p_clean.lower()

                        # === УМНАЯ ЗАЩИТА ОТ ПЕРЕЗАПИСИ ДАННЫХ V2 ===
                        v_id_str = str(v_product_id).strip()
                        parent_id_str = str(parent_product_id).strip()
                        
                        # Если этот вариант уже есть в списке, или его ID совпадает с ID родителя (дефолтный вариант, обработанный v2)
                        existing_variant = next((v for v in parent.variants if str(v.product_id) == v_id_str), None)
                        if existing_variant or v_id_str == parent_id_str:
                            logger.info(f"✅ [V1 SKIP] Вариант ID {v_id_str} уже корректно обработан через v2 или является дефолтным. Пропускаем, чтобы не создавать дубли и не портить цены.")
                            continue
                        # ==================================================

                        v_mod_attrs = {}
                        if clean_group_name and clean_val:
                            v_mod_attrs[clean_group_name] = clean_val
                            parent.modification_attributes[clean_group_name] = ""
                        elif clean_val:
                            v_mod_attrs["option"] = clean_val
                            parent.modification_attributes["option"] = ""

                        variant = ProductVariant(
                            product_id=str(v_product_id).strip(),
                            parent_product_id=parent_product_id,
                            sku=real_v_sku,
                            parent_sku=parent.sku,
                            title=v_title,  # Точное оригинальное название со страницы варианта
                            description=parent.description,
                            price=v_price,
                            fact_price=v_fact_price,
                            available=v_avail,
                            image=v_image,
                            product_link=current_variant_url,
                            category_id=parent.category_id,
                            category_name=parent.category_name,
                            category_link=parent.category_link,
                            modification_attributes=v_mod_attrs
                        )
                        added = state_machine.add_variant(parent_product_id, variant)
                        # if added:
                        #     logger.info(f"🔹 [VARIANT] Добавлен вариант для ID {parent_product_id}: {variant.title} (ID: {variant.product_id})")
                    except Exception:
                        pass
                
                if len(parent.variants) > 0:
                    break

    def _get_json_ld_items(self, data: Any) -> list:
        items = []
        if isinstance(data, list):
            for item in data:
                items.extend(self._get_json_ld_items(item))
        elif isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                items.extend(self._get_json_ld_items(data["@graph"]))
            else:
                items.append(data)
        return items

    async def _ensure_full_page_loaded(self, page: Page) -> None:
        """Гарантирует подгрузку динамических характеристик через скролл и клик по вкладкам."""
        try:
            # 1. Ждем появления блоков с информацией (характеристики, описание, таблицы)
            try:
                await page.wait_for_selector(
                    "main, article, table, [class*='char'], [class*='desc'], [class*='spec'], [class*='param'], [class*='tab']",
                    timeout=4000
                )
            except Exception:
                pass

            # 2. Легкий автоскролл для срабатывания Lazy-load и JS-гидратации
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3);")
            await page.wait_for_timeout(500)

            # 3. Раскрытие вкладок «Характеристики» / «Описание», если они спрятаны в переключателях
            await page.evaluate("""
                () => {
                    const clean = (str) => (str || '').replace(/[\\s\\u00a0]+/g, ' ').trim().toLowerCase();
                    const tabKeywords = ['характеристик', 'опис', 'descrip', 'specif', 'param', 'детал'];
                    const candidates = Array.from(document.querySelectorAll('button, a, li, div[class*="tab" i], div[class*="accordion" i]'));
                    
                    for (const el of candidates) {
                        const txt = clean(el.textContent);
                        if (tabKeywords.some(kw => txt.includes(kw))) {
                            try { el.click(); } catch(e) {}
                        }
                    }
                }
            """)
            await page.wait_for_timeout(500)
            await page.evaluate("window.scrollTo(0, 0);")
        except Exception as e:
            logger.debug(f"⚠️ [PAGE HYDRATION WARN] Ошибка при подгрузке DOM: {e}")       

    async def parse_page(self, page: Page, url: str, state_machine: CatalogStateMachine, existing_product: Optional[ProductParent] = None) -> bool:
        # 0. Активация гидратации DOM (скролл + раскладывание вкладок до съема данных)
        await self._ensure_full_page_loaded(page)   

        # 1. Извлечение категорий через универсальный CategoryExtractor (ДО очистки DOM)
        ext_cat_id, ext_cat_name, ext_cat_link = await self.category_extractor.extract_and_register_categories(
            page, url, state_machine
        )
                
        extracted_product_id = existing_product.product_id if (existing_product and existing_product.product_id) else None

        if existing_product and extracted_product_id:
            state_machine.add_parent(existing_product)

        # 2. Предварительный сбор fallback_sku из Schema.org (страховочная сетка)
        if not extracted_product_id:
            # 2. Предварительный сбор fallback_sku из Schema.org
            fallback_sku = ""
            scripts = await page.locator('script[type="application/ld+json"]').all_inner_texts()
            for script_text in scripts:
                try:
                    data = json.loads(script_text)
                    items = self._get_json_ld_items(data)
                    for item in items:
                        if isinstance(item, dict) and item.get("@type") in ["Product", "IndividualProduct"]:
                            fallback_sku = self.extract_clean_sku(str(item.get("sku", "") or item.get("productID", "")), url)
                            if fallback_sku:
                                break
                except Exception:
                    continue

            if not fallback_sku:
                fallback_sku = self.extract_clean_sku("", url)

            # 3. Shopify API Fast-Path
            if "/products/" in url:
                try:
                    js_url = url.split("?")[0].rstrip("/") + ".js"
                    data = await page.evaluate("""
                        async (jsUrl) => {
                            try {
                                const res = await fetch(jsUrl);
                                if (res.ok) return await res.json();
                            } catch(e) {}
                            return null;
                        }
                    """, js_url)

                    if data and isinstance(data, dict) and data.get("title"):
                        product_id = str(data.get("id") or "").strip() or fallback_sku
                        if product_id:
                            parent_sku = str(data.get("variants", [{}])[0].get("sku") or product_id).strip()
                            title = self.clean_text(data.get("title", ""))
                            description = self.clean_text(data.get("description", ""))
                            images = data.get("images", [])
                            clean_images = []
                            if isinstance(images, list):
                                for img in images:
                                    img_str = img if isinstance(img, str) else (img.get("src") if isinstance(img, dict) else "")
                                    if img_str:
                                        if img_str.startswith("//"):
                                            img_str = "https:" + img_str
                                        if img_str not in clean_images:
                                            clean_images.append(img_str)
                            main_img = ", ".join(clean_images)
                            variants = data.get("variants", [])
                            fact_price = float(variants[0].get("price", 0) or 0) / 100.0 if variants else 0.0
                            old_price = float(variants[0].get("compare_at_price", 0) or 0) / 100.0 if variants else 0.0
                            avail_str = "InStock" if any(v.get("available") for v in variants) else "OutOfStock"

                            parent = ProductParent(
                                product_id=product_id, sku=parent_sku, title=title, description=description,
                                price=old_price if old_price > fact_price else fact_price,
                                fact_price=fact_price, available=avail_str, image=main_img,
                                product_link=url, category_id=ext_cat_id, category_name=ext_cat_name, category_link=ext_cat_link
                            )
                            if state_machine.add_parent(parent):
                                logger.info(f"✅ [SHOPIFY API] Товар: {title} | ID: {product_id} | SKU: {parent_sku} | Цена: {fact_price}")
                                extracted_product_id = product_id
                except Exception as e:
                    logger.debug(f"⚠️ Ошибка вызова Shopify fetch: {e}")

            
            # 4. Schema.org + DOM с гарантированным fallback_sku
            if not extracted_product_id:
                try:
                    dom_pid = await self.resolve_product_id(page, url, fallback_sku=fallback_sku)

                    for script_text in scripts:
                        data = json.loads(script_text)
                        items = self._get_json_ld_items(data)
                        for item in items:
                            if isinstance(item, dict) and item.get("@type") in ["Product", "IndividualProduct"]:
                                schema_pid = str(item.get("productID") or item.get("g:id") or "").strip()

                                # Определяем итоговый ID с приоритетом Schema -> DOM -> Fallback SKU
                                product_id = schema_pid or dom_pid or fallback_sku
                                if not product_id:
                                    continue

                                raw_sku = item.get("sku") or item.get("productID", "")
                                parent_sku = self.extract_clean_sku(raw_sku, url)
                                title = self.clean_text(item.get("name", ""))
                                description = self.clean_text(item.get("description", ""))

                                images = item.get("image", [])
                                clean_images = []
                                if isinstance(images, list):
                                    for img in images:
                                        img_str = img if isinstance(img, str) else (img.get("url") if isinstance(img, dict) else "")
                                        if img_str:
                                            abs_url = self._make_absolute_url(img_str, url)
                                            if abs_url and abs_url not in clean_images:
                                                clean_images.append(abs_url)
                                elif isinstance(images, str) and images:
                                    abs_url = self._make_absolute_url(images, url)
                                    if abs_url:
                                        clean_images.append(abs_url)

                                main_img = ", ".join(clean_images)

                                brand_name = self._extract_name_or_str(item.get("brand"))
                                manufacturer = self._extract_name_or_str(item.get("manufacturer"))
                                country_of_origin = self._extract_name_or_str(item.get("countryOfOrigin"))

                                price, fact_price, raw_availability = self._parse_json_ld_offers(item.get("offers"))

                                parent = ProductParent(
                                    product_id=product_id, sku=parent_sku, title=title, description=description,
                                    price=price, fact_price=fact_price, available=raw_availability,
                                    image=main_img, product_link=url,
                                    category_id=ext_cat_id, category_name=ext_cat_name, category_link=ext_cat_link,
                                    brand_name=brand_name, manufacturer=manufacturer, country_of_origin=country_of_origin
                                )

                                extracted_product_id = product_id
                                added = state_machine.add_parent(parent)
                                if added:
                                    logger.info(f"✅ [SCHEMA] Товар: {title} | ID: {product_id} | SKU: {parent_sku} | fact_price: {fact_price} | old_price: {price}")
                                break
                        if extracted_product_id:
                            break
                except Exception as e:
                    logger.debug(f"⚠️ Ошибка Schema.org парсинга: {e}")

            # 5. DOM / Meta Fallback
            if not extracted_product_id:
                try:
                    dom_pid = await self.resolve_product_id(page, url, fallback_sku=fallback_sku)
                    if dom_pid:
                        meta_data = await page.evaluate("""
                            () => {
                                const getMeta = (attr, val) => {
                                    const el = document.querySelector(`meta[${attr}="${val}"]`);
                                    return el ? el.getAttribute('content') : '';
                                };
                                
                                const title = getMeta('property', 'og:title') || 
                                            getMeta('name', 'title') || 
                                            (document.querySelector('h1')?.textContent || '').trim() || 
                                            document.title;
                                            
                                const image = getMeta('property', 'og:image') || getMeta('name', 'image');
                                const price = getMeta('property', 'product:price:amount') || 
                                            getMeta('property', 'og:price:amount') || 
                                            getMeta('name', 'price');
                                            
                                return { title, image, price };
                            }
                        """)

                        title = self.clean_text(meta_data.get("title", ""))
                        if title:
                            parent_sku = fallback_sku
                            og_image = meta_data.get("image", "")
                            main_img = self._make_absolute_url(og_image, url) if og_image else await self._extract_active_dom_image(page)

                            raw_price = meta_data.get("price", "")
                            fact_price = 0.0
                            if raw_price:
                                try:
                                    clean_p = re.sub(r'[^\d.]', '', raw_price.replace(',', '.'))
                                    fact_price = float(clean_p) if clean_p else 0.0
                                except ValueError:
                                    pass

                            if fact_price == 0.0:
                                try:
                                    dom_price = await page.evaluate("""
                                        () => {
                                            const priceEl = document.querySelector('[class*="price" i]:not([class*="old" i]):not([class*="compare" i])');
                                            if (priceEl) {
                                                const txt = priceEl.textContent.replace(/[^0-9.,]/g, '').replace(',', '.');
                                                if (txt && !isNaN(parseFloat(txt))) return parseFloat(txt);
                                            }
                                            return 0.0;
                                        }
                                    """)
                                    if dom_price > 0:
                                        fact_price = dom_price
                                except Exception:
                                    pass

                            parent = ProductParent(
                                product_id=dom_pid,
                                sku=parent_sku,
                                title=title,
                                description="",
                                price=fact_price,
                                fact_price=fact_price,
                                available="InStock",
                                image=main_img,
                                product_link=url,
                                category_id=ext_cat_id,
                                category_name=ext_cat_name,
                                category_link=ext_cat_link
                            )

                            extracted_product_id = dom_pid
                            if state_machine.add_parent(parent):
                                logger.info(f"✅ [DOM/OG FALLBACK] Товар: {title} | ID: {dom_pid} | SKU: {parent_sku} | Цена: {fact_price}")
                except Exception as e:
                    logger.debug(f"⚠️ Ошибка DOM/OG Fallback: {e}")

        if not extracted_product_id:
            logger.warning(f"⚠️ [SKIP] Товар пропущен [{url}]: не найден системный product_id.")
            return False

        # 6. Пост-обход DOM, вариантов и описаний
        parent = state_machine.parents[extracted_product_id]

        if not parent.image:
            parent.image = await self._extract_active_dom_image(page)

        if not parent.description:
            try:
                desc_locators = page.locator('[itemprop="description"], [data-qaid="product_description"], .b-product-info__description, #description, .product-description')
                if await desc_locators.count() > 0:
                    parent.description = self.clean_text(await desc_locators.first.inner_text())
                    logger.info(f"📝 [DOM] Собрано описание из DOM ({len(parent.description)} симв.)")
            except Exception:
                pass

        if not getattr(parent, "currency", None) or parent.currency == "USD":
            try:
                micro_currency = await page.evaluate("""
                    () => {
                        const el = document.querySelector('[itemprop="priceCurrency"], meta[property="og:price:currency"], meta[name="currency"]');
                        return el ? (el.getAttribute('content') || el.textContent || '').trim() : '';
                    }
                """)
                if micro_currency:
                    clean_curr = self.clean_text(micro_currency)
                    if clean_curr:
                        parent.currency = clean_curr
                        # Пробрасываем валюту во все дочерние варианты, если они есть
                        for variant in getattr(parent, "variants", []):
                            variant.currency = clean_curr
            except Exception as e:
                logger.debug(f"⚠️ [MICRODATA CURRENCY] Не удалось извлечь валюту из meta-тегов: {e}")                

        await self._extract_dom_prices_fallback(page, parent)
        # await self._extract_interactive_variants(page, url, state_machine, extracted_product_id)
        # await self._extract_link_variants(page, url, state_machine, extracted_product_id)

        # Синхронизация вариантов и выравнивание матрицы модификаций
        all_mod_keys = set(parent.modification_attributes.keys())
        for variant in parent.variants:
            all_mod_keys.update(variant.modification_attributes.keys())

        # Если Ollama нашла именованные атрибуты у вариантов, удаляем безымянный 'option' у родителя
        if any(k.lower() != "option" for k in all_mod_keys) and "option" in parent.modification_attributes:
            parent.modification_attributes.pop("option", None)
            for variant in parent.variants:
                if any(k.lower() != "option" for k in variant.modification_attributes):
                    variant.modification_attributes.pop("option", None)

        # Синхронизация вариантов: заполняем ТОЛЬКО отсутствующие данные, не перезаписывая корректные значения от v2
        for variant in parent.variants:
            if not variant.brand_name: variant.brand_name = parent.brand_name
            if not variant.manufacturer: variant.manufacturer = parent.manufacturer
            if not variant.country_of_origin: variant.country_of_origin = parent.country_of_origin
            if not variant.category_id: variant.category_id = parent.category_id
            if not variant.category_name: variant.category_name = parent.category_name
            if not variant.category_link: variant.category_link = parent.category_link
            if not variant.available: variant.available = parent.available

            if variant.sales_notes is None:
                variant.sales_notes = parent.sales_notes

            if variant.bonus is None:
                variant.bonus = parent.bonus
                # logger.info(f"⚠️ [V1 SYNC] Вариант {variant.product_id} имел bonus=None. Заполнен бонусом родителя: '{variant.bonus}'")
            # elif variant.bonus == "":
            #     logger.info(f"✅ [V1 PROTECTED] Вариант {variant.product_id} имеет намеренно пустой bonus=''. Оставляем как есть, НЕ перезаписываем родителем ('{parent.bonus}')")
            
            # ИЗМЕНЕНИЕ: Заполняем цену только если она равна 0 или отсутствует. 
            # Никогда не перезаписываем корректную цену варианта ценой родителя!
            if not variant.price or variant.price == 0.0:
                variant.price = parent.price
            if not variant.fact_price or variant.fact_price == 0.0:
                variant.fact_price = parent.fact_price

        return True

    @staticmethod
    def _extract_name_or_str(obj: Any) -> str:
        """Вспомогательный метод: извлекает имя из строки или словаря {'@type': 'Brand', 'name': '...'}'."""
        if isinstance(obj, str):
            return obj.strip()
        if isinstance(obj, dict):
            return str(obj.get("name") or obj.get("title") or "").strip()
        return ""   
