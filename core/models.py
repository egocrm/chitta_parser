from dataclasses import dataclass, field
from typing import Dict, List, Optional


def compute_bonus(price: float, fact_price: float) -> str:
    """Автоматически рассчитывает процент скидки, если старая цена выше текущей."""
    if price > fact_price > 0:
        discount = round(((price - fact_price) / price) * 100)
        if discount > 0:
            return f"-{discount}%"
    return ""


@dataclass
class Category:
    """Модель категории для categories.csv"""
    id: str
    name: str
    parent_id: str


@dataclass
class ProductVariant:
    """Модель дочернего варианта товара (SKU/ID варианта)"""
    product_id: str = ""
    parent_product_id: str = ""
    sku: str = ""
    parent_sku: str = ""
    title: str = ""
    model: str = ""
    description: str = ""
    add_description: str = ""
    instructs: str = ""
    sales_notes: str = ""
    price: float = 0.0
    fact_price: float = 0.0
    bonus: str = ""
    currency: str = "UAH"
    category_id: str = ""
    category_name: str = ""
    image: str = ""
    product_link: str = ""
    category_link: str = ""
    tags: str = ""
    brand_name: str = ""
    manufacturer: str = ""
    country_of_origin: str = ""
    available: str = "yes"
    stock: int = 50
    
    # Сырые данные: список найденных пар {name: value} или просто {value: ""}, если имя не найдено
    raw_attributes: List[Dict[str, str]] = field(default_factory=list)
    
    # Структурированные данные (заполняются после нормализации)
    attributes: Dict[str, str] = field(default_factory=dict)
    modification_attributes: Dict[str, str] = field(default_factory=dict)
    
    rules_of_use: str = ""
    links_rules_of_use: str = ""
    instructions: str = ""
    links_instructions: str = ""
    reviews: str = ""
    links_reviews: str = ""

    def update_bonus(self) -> None:
        """Обновляет поле bonus, если оно не было заполнено явно."""
        if not self.bonus:
            self.bonus = compute_bonus(self.price, self.fact_price)


@dataclass
class ProductParent:
    product_id: str = ""
    sku: str = ""
    title: str = ""
    model: str = ""
    description: str = ""
    add_description: str = ""
    instructs: str = ""
    sales_notes: str = ""
    price: float = 0.0
    fact_price: float = 0.0
    bonus: str = ""
    currency: str = "UAH"
    category_id: str = ""
    category_name: str = ""
    image: str = ""
    product_link: str = ""
    category_link: str = ""
    tags: str = ""
    brand_name: str = ""
    manufacturer: str = ""
    country_of_origin: str = ""
    available: str = "yes"
    stock: int = 50
    
    # Сырые данные родителя (если есть)
    raw_attributes: List[Dict[str, str]] = field(default_factory=list)
    
    # Структурированные данные (заполняются после нормализации)
    attributes: Dict[str, str] = field(default_factory=dict)
    modification_attributes: Dict[str, str] = field(default_factory=dict)
    
    variants: List[ProductVariant] = field(default_factory=list)
    
    rules_of_use: str = ""
    links_rules_of_use: str = ""
    instructions: str = ""
    links_instructions: str = ""
    reviews: str = ""
    links_reviews: str = ""

    def sanitize_modification_attributes(self) -> None:
        """Удаляет из родительских статических attributes все ключи, попавшие в modification_attributes родителя или вариантов, переносит одинаковые значения вариантов в статику, а статические атрибуты с разными значениями превращает в модификации."""
        if self.variants:
            # 1. Поиск статических атрибутов (attributes), у которых у вариантов РАЗНЫЕ значения -> автоматическая конвертация в модификации
            all_static_keys = set()
            for v in self.variants:
                all_static_keys.update(v.attributes.keys())

            for k in list(all_static_keys):
                vals = {str(v.attributes.get(k, "")).strip() for v in self.variants if str(v.attributes.get(k, "")).strip()}
                # Если у вариантов 2 или более уникальных значения по одному статическому атрибуту
                if len(vals) > 1:
                    # Регистрируем ось модификации у родителя
                    self.modification_attributes[k] = ""
                    # Переносим значения в modification_attributes каждого варианта и вычищаем из статики
                    for v in self.variants:
                        val = v.attributes.pop(k, None)
                        if val and str(val).strip():
                            v.modification_attributes[k] = str(val).strip()
                    # Удаляем из статических атрибутов родителя
                    self.attributes.pop(k, None)

            # 2. Поиск характеристик в modification_attributes, которые имеют одинаковые непустые значения у ВСЕХ вариантов -> обратный перенос в статику
            all_mod_keys = set()
            for v in self.variants:
                all_mod_keys.update(v.modification_attributes.keys())

            for k in list(all_mod_keys):
                vals = {str(v.modification_attributes.get(k, "")).strip() for v in self.variants if str(v.modification_attributes.get(k, "")).strip()}
                # Если у всех вариантов значение по ключу одинаковое (всего 1 уникальное значение)
                if len(vals) == 1 and len(self.variants) > 1:
                    common_val = list(vals)[0]
                    # Переносим характеристику в статические атрибуты родителя
                    self.attributes[k] = common_val
                    # Удаляем из модификаций родителя и всех вариантов
                    self.modification_attributes.pop(k, None)
                    for v in self.variants:
                        v.modification_attributes.pop(k, None)

        # 3. Удаляем из родительских статических attributes только те ключи, которые остались активными модификациями
        active_mod_keys = {k.lower().strip() for k, v in self.modification_attributes.items()}
        for v in self.variants:
            for k, v_val in v.modification_attributes.items():
                if v_val and str(v_val).strip():
                    active_mod_keys.add(k.lower().strip())
        
        if not active_mod_keys:
            return

        # Удаляем совпадения из родительских статических характеристик
        keys_to_remove = [k for k in self.attributes.keys() if k.lower().strip() in active_mod_keys]
        for k in keys_to_remove:
            del self.attributes[k]

    def update_bonus(self) -> None:
        """Пересчитывает bonus для родителя и всех его дочерних вариантов."""
        if not self.bonus:
            self.bonus = compute_bonus(self.price, self.fact_price)
        for v in self.variants:
            v.update_bonus()