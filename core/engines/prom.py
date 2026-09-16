import json
import logging
from typing import List, Optional
from playwright.async_api import Page
from core.engines.base import BaseEngine
from core.models import ProductParent
from core.state_machine import CatalogStateMachine

logger = logging.getLogger("ChittaParser")

class PromEngine(BaseEngine):
    """Изолированный движок для сайтов и маркетплейсов на платформе Prom.ua."""

    async def can_handle(self, page: Page, url: str) -> bool:
        if "prom.ua" in url or "prom.st" in url:
            return True
        content = await page.content()
        return "data-qaid" in content or "b-product-carousel" in content

    async def extract_catalog_links(self, page: Page) -> List[str]:
        return await self.parser.extract_product_links(page)

    async def parse_product_page(
        self, 
        page: Page, 
        url: str, 
        state_machine: Optional[CatalogStateMachine] = None
    ) -> Optional[ProductParent]:
        product_id = await self.parser.resolve_product_id(page, url)
        if not product_id:
            logger.warning(f"⚠️ [PROM ENGINE] Товар пропущен [{url}]: не найден системный product_id.")
            return None

        scripts = await page.locator('script[type="application/ld+json"]').all_inner_texts()
        
        # 1. Извлечение категорий из хлебных крошек
        cat_id, cat_name, cat_link = "", "", ""
        if state_machine:
            cat_id, cat_name, cat_link = await self.parser.category_extractor.extract_and_register_categories(
                page, url, state_machine
            )
            
        # 2. Извлечение Schema.org
        for script_text in scripts:
            try:
                data = json.loads(script_text)
                items = self.parser._get_json_ld_items(data)
                for item in items:
                    if isinstance(item, dict) and item.get("@type") in ["Product", "IndividualProduct"]:
                        parent_sku = self.parser.extract_clean_sku(str(item.get("sku", "")), url)
                        title = self.parser.clean_text(item.get("name", ""))
                        description = self.parser.clean_text(item.get("description", ""))

                        images = item.get("image", [])
                        clean_images = []
                        if isinstance(images, list):
                            for img in images:
                                img_str = img if isinstance(img, str) else (img.get("url") if isinstance(img, dict) else "")
                                if img_str:
                                    abs_url = self.parser._make_absolute_url(img_str, url)
                                    if abs_url and abs_url not in clean_images:
                                        clean_images.append(abs_url)
                        elif isinstance(images, str) and images:
                            abs_url = self.parser._make_absolute_url(images, url)
                            if abs_url:
                                clean_images.append(abs_url)

                        main_img = ", ".join(clean_images)

                        price, fact_price, raw_avail = self.parser._parse_json_ld_offers(item.get("offers"))

                        brand_name = self.parser._extract_name_or_str(item.get("brand"))
                        manufacturer = self.parser._extract_name_or_str(item.get("manufacturer"))
                        country_of_origin = self.parser._extract_name_or_str(item.get("countryOfOrigin"))

                        parent = ProductParent(
                            product_id=product_id,
                            sku=parent_sku,
                            title=title,
                            description=description,
                            price=price,
                            fact_price=fact_price,
                            available=raw_avail,
                            image=main_img,
                            product_link=url,
                            category_id=cat_id,
                            category_name=cat_name,
                            category_link=cat_link,
                            brand_name=brand_name,
                            manufacturer=manufacturer,
                            country_of_origin=country_of_origin
                        )

                        await self.parser._extract_dom_prices_fallback(page, parent)

                        # 3. Сбор интерактивных вариантов
                        if state_machine:
                            state_machine.add_parent(parent)
                            await self.parser._extract_interactive_variants(page, url, state_machine, product_id)

                        logger.info(f"🏬 [PROM ENGINE] Товар: {title} | ID: {product_id} | SKU: {parent_sku} | Цена: {parent.fact_price}")
                        return parent
            except Exception:
                continue

        logger.warning(f"⚠️ [PROM ENGINE] Товар пропущен [{url}]: не удалось извлечь данные Schema.org.")
        return None