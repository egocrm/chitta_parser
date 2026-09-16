import logging
import re
from typing import Dict, List, Optional, Set
from core.models import Category, ProductParent, ProductVariant

logger = logging.getLogger("ChittaParser")

class CatalogStateMachine:
    """Управление графом категорий, товаров и вариантов в памяти с контролем уникальности по product_id."""

    def __init__(self, max_parents: int = 100):
        self.max_parents = max_parents
        self.categories: Dict[str, Category] = {}
        self.parents: Dict[str, ProductParent] = {}  # Ключ: product_id
        self.registered_product_ids: Set[str] = set()
        self.registered_variant_ids: Set[str] = set()

    @staticmethod
    def is_valid_image_url(url: str) -> bool:
        """Проверяет, что ссылка (или каждая ссылка в списке через запятую) ведет на файл изображения."""
        if not url:
            return False
        valid_extensions = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.svg', '.avif', '.bmp')
        cdn_indicators = ('cdn.shopify', '/images/', '/img/', '/media/', '/photos/', 'data:image/')
        parts = [p.strip() for p in str(url).split(",") if p.strip()]
        if not parts:
            return False
        for part in parts:
            clean_part = part.split("?")[0].split("#")[0].lower()
            has_ext = any(clean_part.endswith(ext) for ext in valid_extensions)
            has_cdn = any(ind in part.lower() for ind in cdn_indicators)
            if not (has_ext or has_cdn):
                return False
        return True

    def is_limit_reached(self) -> bool:
        """Проверяет достижение заданного лимита основных товаров."""
        return len(self.parents) >= self.max_parents

    def add_category(self, cat_id: str, name: str, parent_id: str = "0") -> Category:
        """Регистрирует категорию с контролем дубликатов ID."""
        cat_id_str = str(cat_id)
        if cat_id_str in self.categories:
            return self.categories[cat_id_str]
        
        category = Category(id=cat_id_str, parent_id=str(parent_id), name=name)
        self.categories[cat_id_str] = category
        # logger.info(f"📂 [CATEGORY] Добавлена категория: {name} (ID: {cat_id_str})")
        return category

    def add_parent(self, parent: ProductParent) -> bool:
        """Добавляет родительский товар с проверкой лимита, наличия системного product_id и валидности фото."""
        if self.is_limit_reached():
            logger.warning(f"⚠️ [LIMIT] Достигнут лимит основных товаров ({self.max_parents}). Товар {parent.title} пропущен.")
            return False

        if not parent.product_id:
            logger.warning(f"⚠️ [SKIP] Товар пропущен [{parent.product_link}]: не найден системный product_id в DOM/API.")
            return False

        if parent.product_id in self.registered_variant_ids:
            logger.info(f"⏩ [SKIP PARENT] ID {parent.product_id} уже зарегистрирован как дочерний вариант. Пропуск создания родителя.")
            return False

        if parent.product_id in self.registered_product_ids:
            logger.warning(f"⚠️ [PRODUCT_ID] Товар с product_id {parent.product_id} уже зарегистрирован.")
            return False

        # Валидация и фильтрация массива изображений (оставляем только корректные ссылки)
        if parent.image:
            valid_parts = [
                p.strip() for p in str(parent.image).split(",") 
                if p.strip() and CatalogStateMachine.is_valid_image_url(p.strip())
            ]
            if valid_parts:
                parent.image = ", ".join(valid_parts)
            else:
                logger.debug(f"⚠️ [MEDIA] У товара ID:{parent.product_id} некорректный URL фото ({parent.image}). Поле очищено.")
                parent.image = ""

        # Установка дефолтного наличия
        if parent.available == "yes" and parent.stock <= 0:
            parent.stock = 50

        self.parents[parent.product_id] = parent
        self.registered_product_ids.add(parent.product_id)
        return True

    def add_variant(self, parent_product_id: str, variant: ProductVariant) -> bool:
        """Привязывает дочерний вариант к родительскому product_id."""
        if parent_product_id not in self.parents:
            logger.error(f"❌ [ERROR] Не найден родительский product_id {parent_product_id} для варианта ID:{variant.product_id}.")
            return False

        if not variant.product_id:
            logger.warning(f"⚠️ [SKIP] Вариант пропущен [{variant.product_link}]: не найден системный product_id варианта.")
            return False

        # # Вариант 2: Если ID варианта совпадает с ID родителя (дефолтная активная модификация)
        # if variant.product_id == parent_product_id:
        #     parent = self.parents[parent_product_id]
            
        #     # 1. Переносим атрибуты модификации в родительский товар
        #     for attr_k, attr_v in variant.attributes.items():
        #         if attr_v and attr_k not in parent.attributes:
        #             parent.attributes[attr_k] = attr_v

        #     # 2. Обновляем SKU родителя, если у него был дефолтный ID
        #     if variant.sku and (not parent.sku or parent.sku == parent_product_id):
        #         parent.sku = variant.sku

        #     # 3. Обновляем изображение родителя, если у варианта оно более точное
        #     if self.is_valid_image_url(variant.image) and not self.is_valid_image_url(parent.image):
        #         parent.image = variant.image

        #     logger.info(
        #         f"ℹ️ [VARIANT ENRICH] Вариант '{variant.title}' (ID: {variant.product_id}) совпадает с родителем. "
        #         f"Данные перенесены в родительский товар, дублирующая запись пропущена."
        #     )
        #     return True

        # variant_reg_id = f"{parent_product_id}_{variant.product_id}"

        # if variant_reg_id in self.registered_product_ids:
        #     logger.warning(f"⚠️ [PRODUCT_ID] Вариант с product_id {variant.product_id} для родителя {parent_product_id} уже зарегистрирован.")
        #     return False

        # variant.parent_product_id = parent_product_id

        # # Наследование изображения у родителя, если у варианта нет своего
        # if not self.is_valid_image_url(variant.image):
        #     variant.image = self.parents[parent_product_id].image

        # # Наследование категории родителя
        # if not variant.category_id:
        #     variant.category_id = self.parents[parent_product_id].category_id
        #     variant.category_name = self.parents[parent_product_id].category_name

        # if not variant.category_link:
        #     variant.category_link = self.parents[parent_product_id].category_link            

        # parent = self.parents[parent_product_id]

        # # Регистрируем ключи модификаций варианта в родительском объекте
        # for mod_k in variant.modification_attributes.keys():
        #     parent.modification_attributes[mod_k] = ""

        # # Очищаем статические атрибуты родителя от ключей модификаций
        # parent.sanitize_modification_attributes()

        # parent.variants.append(variant)
        # self.registered_product_ids.add(variant_reg_id)
        # logger.info(f"🔹 [VARIANT] Добавлен вариант для ID {parent_product_id}: {variant.title} (ID: {variant.product_id}, SKU: {variant.sku})")
        # return True

        parent = self.parents[parent_product_id]

        # 1. Если ID варианта совпадает с родителем, обогащаем базовые поля родителя
        has_mod_values = any(v for v in variant.modification_attributes.values() if v and str(v).strip())
        
        if variant.product_id == parent_product_id:
            for attr_k, attr_v in variant.attributes.items():
                if attr_v and attr_k not in parent.attributes:
                    parent.attributes[attr_k] = attr_v

            if variant.sku and (not parent.sku or parent.sku == parent_product_id):
                parent.sku = variant.sku

            if self.is_valid_image_url(variant.image) and not self.is_valid_image_url(parent.image):
                parent.image = variant.image

            # Пропускаем только дефолтные дубликаты БЕЗ значений модификаций (например, повторный клик по базовой карточке)
            if not has_mod_values:
                logger.info(
                    f"ℹ️ [VARIANT ENRICH] Вариант '{variant.title}' (ID: {variant.product_id}) совпадает с родителем без доп. модификаций. Данные обогащены."
                )
                return True

        # 2. Формируем уникальный под-ключ регистрации в памяти с учетом значения модификации (например, "144_144_Колір:Pink")
        mod_sig = "|".join(f"{k}:{v}" for k, v in sorted(variant.modification_attributes.items()) if v)
        variant_reg_id = f"{parent_product_id}_{variant.product_id}" + (f"_{mod_sig}" if mod_sig else "")

        if variant_reg_id in self.registered_product_ids:
            logger.warning(f"⚠️ [PRODUCT_ID] Вариант '{variant.title}' (Mod: {mod_sig}) для родителя {parent_product_id} уже зарегистрирован.")
            return False

        variant.parent_product_id = parent_product_id

        # Наследование изображения у родителя, если у варианта нет своего
        if not self.is_valid_image_url(variant.image):
            variant.image = parent.image

        # Наследование категории родителя
        if not variant.category_id:
            variant.category_id = parent.category_id
            variant.category_name = parent.category_name

        if not variant.category_link:
            variant.category_link = parent.category_link

        # Регистрируем ключи модификаций варианта в родительском объекте
        for mod_k in variant.modification_attributes.keys():
            parent.modification_attributes[mod_k] = ""

        # Очищаем статические атрибуты родителя от ключей модификаций
        parent.sanitize_modification_attributes()

        parent.variants.append(variant)
        self.registered_product_ids.add(variant_reg_id)
        if variant.product_id and variant.product_id != parent_product_id:
            self.registered_variant_ids.add(variant.product_id)
        logger.info(f"🔹 [VARIANT] Добавлен вариант для ID {parent_product_id}: {variant.title} (ID: {variant.product_id}, SKU: {variant.sku}, Mod: {mod_sig})")
        return True       

    def get_all_parents(self) -> List[ProductParent]:
        return list(self.parents.values())

    def get_all_categories(self) -> List[Category]:
        return list(self.categories.values())