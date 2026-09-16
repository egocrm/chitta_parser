import json
import logging
import re
from typing import List, Optional, Tuple
from playwright.async_api import Page
from core.engines.base import BaseEngine
from core.models import ProductParent
from core.state_machine import CatalogStateMachine

logger = logging.getLogger("ChittaParser")


class OpenCartEngine(BaseEngine):
    """Изолированный движок для магазинов на платформе OpenCart / Journal3."""

    async def can_handle(self, page: Page, url: str) -> bool:
        if "route=product/" in url or "product_id=" in url:
            return True
        content = await page.content()
        return any(sig in content for sig in [
            "catalog/view/theme",
            "catalog/view/javascript",
            "index.php?route=",
            "common/cart"
        ])

    def _parse_price_number(self, raw_text: str) -> float:
        """Очищает строку от символов валют, налогов, пробелов и приводит во float."""
        if not raw_text:
            return 0.0
        clean_str = raw_text.replace('\xa0', ' ').strip()
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
        except ValueError:
            return 0.0

    async def _extract_prices(self, page: Page) -> Tuple[float, float]:
        """Многоуровневый каскад извлечения цен под стандартную и кастомную верстку OpenCart."""
        fact_price, old_price = 0.0, 0.0

        # Уровень 1: Микроразметка JSON-LD
        try:
            ld_json_texts = await page.locator('script[type="application/ld+json"]').all_inner_texts()
            for text in ld_json_texts:
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        offers = data.get("offers", {})
                        if isinstance(offers, dict) and "price" in offers:
                            fact_price = float(offers.get("price") or 0.0)
                        elif isinstance(offers, list) and offers and "price" in offers[0]:
                            fact_price = float(offers[0].get("price") or 0.0)
                        if fact_price > 0:
                            break
                except Exception:
                    continue
        except Exception:
            pass

        # Уровень 2: Извлечение зачеркнутой старой цены (.price-old / line-through)
        if fact_price == 0.0:
            old_el = page.locator('.price-old, span.price-old, s.price-old, del, span[style*="line-through"]').first
            if await old_el.count() > 0:
                old_price = self._parse_price_number(await old_el.inner_text())

            new_el = page.locator('.price-new, span.price-new, .product-price-new').first
            if await new_el.count() > 0:
                fact_price = self._parse_price_number(await new_el.inner_text())

        # Уровень 3: Стандартная верстка OpenCart (h2 внутри #content / .price)
        if fact_price == 0.0:
            h2_els = page.locator('#content h2, .product-price, .price, #product .price')
            count = await h2_els.count()
            for i in range(count):
                txt = await h2_els.nth(i).inner_text()
                parsed = self._parse_price_number(txt)
                if parsed > 0:
                    fact_price = parsed
                    break

        # Уровень 4: Текстовый Regex-поиск
        if fact_price == 0.0:
            content_text = await page.locator('#content').inner_text() if await page.locator('#content').count() > 0 else await page.content()
            # Очищаем контекст от телефонов, артикулов и кодов товаров во избежание ложных срабатываний
            clean_text_for_price = re.sub(r'(?:модель|model|код|артикул|sku|тел|телефон|phone)[\s:]+[^\n\r,]+', '', content_text, flags=re.IGNORECASE)
            matches = re.findall(r'(?<!\d)(?:\d{1,3}(?:\s?\d{3})*|\d+)(?:[\.,]\d{1,2})?\s*(?:грн|₴|\$|€|eur|usd)', clean_text_for_price, re.IGNORECASE)
            if matches:
                for m in matches:
                    p = self._parse_price_number(m)
                    if 0 < p < 10000000:
                        fact_price = p
                        break

        final_old = old_price if old_price > fact_price else fact_price
        return fact_price, final_old

    async def extract_catalog_links(self, page: Page) -> List[str]:
        """Извлекает ссылки на товары с категорийной страницы OpenCart."""
        card_hrefs = await page.eval_on_selector_all(
            '.product-thumb a[href], .product-layout a[href], .product-card a[href], .caption a[href]',
            'els => els.map(el => el.href)'
        )
        
        clean_links = []
        current_page = page.url.split("#")[0]

        for href in card_hrefs:
            clean_url = href.split("#")[0]
            if clean_url != current_page and clean_url not in clean_links:
                clean_links.append(clean_url)

        # Фолбэк по всем ссылкам, содержащим признаки товара
        if not clean_links:
            raw_hrefs = await page.eval_on_selector_all('a[href]', 'els => els.map(el => el.href)')
            for href in raw_hrefs:
                clean_url = href.split("#")[0]
                if ("route=product/product" in clean_url or "product_id=" in clean_url) and clean_url != current_page:
                    if clean_url not in clean_links:
                        clean_links.append(clean_url)

        logger.info(f"⚡ [OPENCART ENGINE] Найдено {len(clean_links)} ссылок на товары")
        return clean_links

    async def parse_product_page(
        self, 
        page: Page, 
        url: str, 
        state_machine: Optional[CatalogStateMachine] = None
    ) -> Optional[ProductParent]:
        try:
            # 1. Извлечение product_id
            product_id = ""
            pid_match = re.search(r'product_id=(\d+)', url)
            if pid_match:
                product_id = pid_match.group(1)

            if not product_id:
                pid_input = page.locator('input[name="product_id"], input[type="hidden"][name*="product_id"]').first
                if await pid_input.count() > 0:
                    product_id = await pid_input.get_attribute("value") or ""

            if not product_id:
                product_id = await self.parser.resolve_product_id(page, url)

            if not product_id:
                logger.warning(f"⚠️ [OPENCART ENGINE] Товар пропущен [{url}]: не найден product_id.")
                return None

            # 2. Заголовок
            title_el = page.locator('#content h1, h1').first
            title = self.parser.clean_text(await title_el.inner_text()) if await title_el.count() > 0 else ""

            # 3. Цены
            fact_price, old_price = await self._extract_prices(page)

            # 4. SKU / Модель
            sku = ""
            sku_el = page.locator('.model, [itemprop="sku"], .sku, .product-model').first
            if await sku_el.count() > 0:
                sku = self.parser.clean_text(await sku_el.inner_text())

            if not sku or sku == product_id:
                content_text = await page.locator('#content').inner_text() if await page.locator('#content').count() > 0 else ""
                sku_match = re.search(r'(?:Модель|Model|Код товара|Артикул|Код):\s*([^\n\r<,]+)', content_text, re.IGNORECASE)
                if sku_match:
                    sku = sku_match.group(1).strip()
                else:
                    sku = product_id

            # 5. Описание
            desc_el = page.locator('#tab-description, .tab-content, .product-description, #tab-specification').first
            description = self.parser.clean_text(await desc_el.inner_text()) if await desc_el.count() > 0 else ""

            # 6. Главное изображение
            all_imgs = []
            img_els = page.locator('#content .thumbnails a, #content a.thumbnail, .product-image a, a[href*="image/catalog"], a[href*="image/data"]')
            count = await img_els.count()
            if count > 0:
                for i in range(count):
                    href = await img_els.nth(i).get_attribute("href") or await img_els.nth(i).get_attribute("src") or ""
                    if href and href not in all_imgs:
                        all_imgs.append(href)

            if not all_imgs:
                img_tags = page.locator('#content img[src*="catalog/"], #content img[src*="image/"]')
                t_count = await img_tags.count()
                for i in range(t_count):
                    src = await img_tags.nth(i).get_attribute("src") or ""
                    if src and src not in all_imgs:
                        all_imgs.append(src)

            main_img = ", ".join(all_imgs)

            # 7. Категории
            cat_id, cat_name, cat_link = "", "", ""
            if state_machine:
                cat_id, cat_name, cat_link = await self.parser.category_extractor.extract_and_register_categories(
                    page, url, state_machine
                )

            # Создаем товар строго в едином экземпляре (без виртуальных вариантов)
            parent = ProductParent(
                product_id=product_id,
                sku=sku,
                title=title,
                description=description,
                price=old_price,
                fact_price=fact_price,
                image=main_img,
                product_link=url,
                category_id=cat_id,
                category_name=cat_name,
                category_link=cat_link
            )

            # Автоматический расчет % скидки
            parent.update_bonus()

            logger.info(f"🛍️ [OPENCART ENGINE] Товар: '{title}' | ID: {product_id} | SKU: {sku} | Цена: {fact_price} | Bonus: '{parent.bonus}'")
            return parent

        except Exception as e:
            logger.warning(f"⚠️ [OPENCART ENGINE] Ошибка парсинга товара [{url}]: {e}")
            return None