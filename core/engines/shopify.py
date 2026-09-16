import logging
from typing import List, Optional, Any
from playwright.async_api import Page
from core.engines.base import BaseEngine
from core.models import ProductParent, ProductVariant

logger = logging.getLogger("ChittaParser")


class ShopifyEngine(BaseEngine):
    """Изолированный движок для магазинов на платформе Shopify."""

    async def can_handle(self, page: Page, url: str) -> bool:
        if "/products/" in url:
            return True
        content = await page.content()
        return "cdn.shopify.com" in content or "Shopify.theme" in content

    async def extract_catalog_links(self, page: Page) -> List[str]:
        raw_hrefs = await page.eval_on_selector_all('a[href]', 'els => els.map(el => el.href)')
        clean_links = []
        current_page = page.url.split("?")[0].rstrip('/')

        for href in raw_hrefs:
            clean_url = href.split("?")[0].split("#")[0].rstrip('/')
            if "/products/" in clean_url and clean_url != current_page and clean_url not in clean_links:
                clean_links.append(clean_url)

        logger.info(f"⚡ [SHOPIFY ENGINE] Найдено {len(clean_links)} ссылок на товары")
        return clean_links

    async def parse_product_page(
        self, 
        page: Page, 
        url: str, 
        state_machine: Optional[Any] = None
    ) -> Optional[ProductParent]:
        js_url = url.split("?")[0].rstrip("/") + ".js"
        try:
            response = await page.request.get(js_url)
            if response.status == 200:
                data = await response.json()
                
                product_id = str(data.get("id") or "").strip()
                if not product_id:
                    product_id = await self.parser.resolve_product_id(page, url)

                if not product_id:
                    logger.warning(f"⚠️ [SHOPIFY ENGINE] Товар пропущен [{url}]: не найден product_id.")
                    return None

                raw_variants = data.get("variants", [])
                first_var = raw_variants[0] if raw_variants else {}
                parent_sku = str(first_var.get("sku") or product_id).strip()

                title = self.parser.clean_text(data.get("title", ""))
                description = self.parser.clean_text(data.get("description", ""))
                brand_name = self.parser.clean_text(data.get("vendor", ""))
                
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

                fact_price = float(first_var.get("price", 0) or 0) / 100.0
                raw_old = float(first_var.get("compare_at_price", 0) or 0) / 100.0
                old_price = raw_old if raw_old > fact_price else fact_price

                available = "yes" if any(v.get("available") for v in raw_variants) else "no"

                # Извлечение категорий из хлебных крошек
                cat_id, cat_name, cat_link = "", "", ""
                if state_machine:
                    cat_id, cat_name, cat_link = await self.parser.category_extractor.extract_and_register_categories(
                        page, url, state_machine
                    )
                    
                parent = ProductParent(
                    product_id=product_id,
                    sku=parent_sku,
                    title=title,
                    description=description,
                    brand_name=brand_name,
                    price=old_price,
                    fact_price=fact_price,
                    available=available,
                    image=main_img,
                    product_link=url,
                    category_id=cat_id,
                    category_name=cat_name,
                    category_link=cat_link
                )

                # Карта названий опций Shopify (option1, option2, option3)
                options_map = {}
                for opt in data.get("options", []):
                    if isinstance(opt, dict) and "name" in opt and "position" in opt:
                        options_map[f"option{opt['position']}"] = self.parser.clean_text(opt["name"])

                # Разбор всех дочерних вариантов товара
                parsed_variants = []
                for var in raw_variants:
                    v_id = str(var.get("id") or "").strip()
                    v_sku = str(var.get("sku") or v_id).strip()
                    v_fact_price = float(var.get("price", 0) or 0) / 100.0
                    v_raw_old = float(var.get("compare_at_price", 0) or 0) / 100.0
                    v_old_price = v_raw_old if v_raw_old > v_fact_price else v_fact_price
                    v_title = self.parser.clean_text(var.get("title", ""))
                    
                    v_img_raw = var.get("featured_image", {})
                    v_img = v_img_raw.get("src", "") if isinstance(v_img_raw, dict) else ""
                    if v_img.startswith("//"):
                        v_img = "https:" + v_img
                    if not v_img:
                        v_img = main_img

                    v_available = "yes" if var.get("available") else "no"

                    # Формирование словаря модификационных атрибутов варианта
                    v_mod_attrs = {}
                    for opt_key, opt_name in options_map.items():
                        opt_val = var.get(opt_key)
                        if opt_val and str(opt_val).lower() != "default title":
                            clean_opt_val = self.parser.clean_text(opt_val)
                            v_mod_attrs[opt_name] = clean_opt_val
                            parent.modification_attributes[opt_name] = ""

                    # Формирование дочернего варианта
                    v_obj = ProductVariant(
                        product_id=v_id,
                        parent_product_id=product_id,
                        sku=v_sku,
                        parent_sku=parent_sku,
                        title=f"{title} ({v_title})" if v_title and v_title.lower() != "default title" else title,
                        price=v_old_price,
                        fact_price=v_fact_price,
                        brand_name=brand_name,
                        available=v_available,
                        image=v_img,
                        product_link=f"{url}?variant={v_id}",
                        category_id=cat_id,
                        category_name=cat_name,
                        category_link=cat_link,
                        modification_attributes=v_mod_attrs
                    )
                    parsed_variants.append(v_obj)

                if state_machine:
                    state_machine.add_parent(parent)
                    for v_obj in parsed_variants:
                        state_machine.add_variant(product_id, v_obj)
                elif len(parsed_variants) > 1 or (len(parsed_variants) == 1 and parsed_variants[0].product_id != product_id):
                    parent.variants = parsed_variants

                # Автоматический расчет процентов скидки (bonus)
                parent.update_bonus()

                logger.info(f"🛍️ [SHOPIFY ENGINE] Товар: '{title}' | ID: {product_id} | Вариантов: {len(parent.variants)} | Bonus: '{parent.bonus}'")

                # Разблокировано: Ollama обогатит этот товар данными со страницы
                return parent
        except Exception as e:
            logger.debug(f"⚠️ [SHOPIFY ENGINE] Ошибка загрузки .js API: {e}")

        logger.warning(f"⚠️ [SHOPIFY ENGINE] Товар пропущен [{url}]: не удалось извлечь данные.")
        return None