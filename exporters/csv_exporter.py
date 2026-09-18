import csv
import logging
import os
import re
from datetime import datetime
from typing import List, Set
from urllib.parse import urlparse, urlunparse
from core.models import Category, ProductParent

logger = logging.getLogger("ChittaParser")


class CSVExporter:
    """Генерация 6 стандартных CSV-файлов импорта с раскладкой по папкам префикса и временным штампам."""

    def __init__(self, output_dir: str = "output_csv"):
        self.output_dir = output_dir
        self.delimiter = ";"
        self.current_prefix = "catalog"
        os.makedirs(self.output_dir, exist_ok=True)

    def _rewrite_url(self, url: str) -> str:
        """Заменяет домен/поддомен исходного сайта на шаблон XX.live.chitta.site."""
        return url
        if not url or not isinstance(url, str):
            return ""
        clean_url = url.strip()
        if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
            return clean_url
        try:
            parsed = urlparse(clean_url)
            if not parsed.netloc:
                return clean_url
            new_netloc = f"{self.current_prefix}.live.chitta.site"
            return urlunparse((parsed.scheme, new_netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
        except Exception:
            return clean_url

    def _clean_text(self, text: str) -> str:
        """Экранирует разделители и убирает символы переноса строк."""
        if not text:
            return ""
        clean = str(text).replace(";", ",").replace("\n", " ").replace("\r", "").strip()
        return clean

    def _open_csv(self, filename: str):
        filepath = os.path.join(self.output_dir, filename)
        return open(filepath, "w", newline="", encoding="utf-8-sig")

    def _get_extra_id_keys(self, parents: List[ProductParent]) -> List[str]:
        """Собирает все динамические альтернативные ключи ID (product_id_N, parent_id_N, parent_product_id_N)."""
        import json
        extra_keys: Set[str] = set()
        for parent in parents:
            for k, v in parent.__dict__.items():
                if (k.startswith("product_id_") or k.startswith("parent_id_") or k.startswith("parent_product_id_")) and not k.startswith("_"):
                    # Очищаем значение от экранирующих кавычек перед проверкой на JSON
                    val_str = str(v).strip().lstrip('"').rstrip('"').strip()
                    is_json = False
                    if val_str.startswith("{") or val_str.startswith("["):
                        try:
                            json.loads(val_str)
                            is_json = True
                        except Exception:
                            is_json = False
                    if isinstance(v, (int, float, str)) and not is_json and len(val_str) < 100:
                        extra_keys.add(k)
            for variant in parent.variants:
                for k, v in variant.__dict__.items():
                    if (k.startswith("product_id_") or k.startswith("parent_id_") or k.startswith("parent_product_id_")) and not k.startswith("_"):
                        val_str = str(v).strip().lstrip('"').rstrip('"').strip()
                        is_json = False
                        if val_str.startswith("{") or val_str.startswith("["):
                            try:
                                json.loads(val_str)
                                is_json = True
                            except Exception:
                                is_json = False
                        if isinstance(v, (int, float, str)) and not is_json and len(val_str) < 100:
                            extra_keys.add(k)

        # Сортировка ключей по типу и номеру (product_id_1, product_id_2, parent_id_1...)
        def sort_key(k: str):
            match = re.search(r'(\d+)$', k)
            num = int(match.group(1)) if match else 0
            prefix = "1" if k.startswith("product_id_") else ("2" if k.startswith("parent_id_") else "3")
            return (num, prefix, k)

        return sorted(list(extra_keys), key=sort_key)        

    def _get_all_attribute_keys(self, parents: List[ProductParent]) -> List[str]:
        """Собирает все уникальные ключи динамических атрибутов из attributes И modification_attributes, очищая их от префиксов. Исключает ВСЕ ключи модификаций (не только color/size)."""
        # Ключи модификаций собираются динамически из всех товаров
        modification_keys = set()
        for parent in parents:
            for k in parent.modification_attributes.keys():
                clean_k = k[13:] if k.startswith("modification_") else k
                modification_keys.add(clean_k.lower())
            for variant in parent.variants:
                for k in variant.modification_attributes.keys():
                    clean_k = k[13:] if k.startswith("modification_") else k
                    modification_keys.add(clean_k.lower())
        
        attr_keys: Set[str] = set()
        for parent in parents:
            # Собираем из attributes
            for k in parent.attributes.keys():
                clean_k = k[5:] if k.startswith("attr_") else k
                # Пропускаем ключи модификаций
                if clean_k.lower() not in modification_keys:
                    attr_keys.add(clean_k)
            # Собираем из modification_attributes (но не добавляем их в attr_keys)
            # Этот блок оставлен для совместимости, но ничего не добавляет
            for k in parent.modification_attributes.keys():
                pass  # Модификации не попадают в атрибуты
                
            for variant in parent.variants:
                # Собираем из attributes варианта
                for k in variant.attributes.keys():
                    clean_k = k[5:] if k.startswith("attr_") else k
                    # Пропускаем ключи модификаций
                    if clean_k.lower() not in modification_keys:
                        attr_keys.add(clean_k)
                # Собираем из modification_attributes варианта (но не добавляем их в attr_keys)
                for k in variant.modification_attributes.keys():
                    pass  # Модификации не попадают в атрибуты
        return sorted(list(attr_keys))

    def export_categories(self, categories: List[Category], filename: str):
        filepath = os.path.join(self.output_dir, filename)
        with self._open_csv(filename) as f:
            writer = csv.writer(f, delimiter=self.delimiter)
            writer.writerow(["id", "parent_id", "name"])
            for cat in categories:
                writer.writerow([cat.id, cat.parent_id, self._clean_text(cat.name)])
        # logger.info(f"💾 [EXPORT] Сохранен файл категорий: {filepath}")

    def export_goods(self, parents: List[ProductParent], dynamic_attrs: List[str], extra_id_keys: List[str], filename: str):
        filepath = os.path.join(self.output_dir, filename)
        base_headers = ["product_id", "parent_product_id"] + extra_id_keys + [
            "sku", "parent_sku", "title", "model", "description", "add_description", 
            "instructs", "sales_notes", "price", "fact_price", "bonus", "category_id", 
            "category_name", "image", "product_link", "category_link", "tags",
            "brand_name", "manufacturer", "country_of_origin"
        ]
        
        attr_headers = [f"attr_{attr}" for attr in dynamic_attrs]
        
        with self._open_csv(filename) as f:
            writer = csv.writer(f, delimiter=self.delimiter)
            writer.writerow(base_headers + attr_headers)

            for p in parents:
                # 1. Запись родительского товара
                extra_id_vals = [self._clean_text(getattr(p, k, "")) for k in extra_id_keys]
                p_row = [p.product_id, ""] + extra_id_vals + [
                    p.sku, "", self._clean_text(p.title), self._clean_text(p.model), 
                    self._clean_text(p.description), self._clean_text(p.add_description),
                    self._clean_text(p.instructs), self._clean_text(p.sales_notes), 
                    p.price, p.fact_price, self._clean_text(p.bonus),
                    p.category_id, self._clean_text(p.category_name), self._rewrite_url(p.image), 
                    self._rewrite_url(p.product_link), self._rewrite_url(p.category_link), self._clean_text(p.tags),
                    self._clean_text(p.brand_name), self._clean_text(p.manufacturer), self._clean_text(p.country_of_origin)
                ]
                for attr in dynamic_attrs:
                    val = p.attributes.get(attr, "") or p.attributes.get(f"attr_{attr}", "")
                    p_row.append(self._clean_text(val))
                writer.writerow(p_row)

                # 2. Запись дочерних вариантов
                for v in p.variants:
                    if getattr(v, 'is_synthetic', False):
                        continue

                    if v.product_id == p.product_id:
                        continue
                    v_extra_vals = [self._clean_text(getattr(v, k, "")) for k in extra_id_keys]
                    v_row = [v.product_id, v.parent_product_id] + v_extra_vals + [
                        v.sku, v.parent_sku, self._clean_text(v.title), self._clean_text(v.model), 
                        self._clean_text(v.description), self._clean_text(v.add_description),
                        self._clean_text(v.instructs), self._clean_text(v.sales_notes), 
                        v.price, v.fact_price, self._clean_text(v.bonus),
                        v.category_id, self._clean_text(v.category_name), self._rewrite_url(v.image), 
                        self._rewrite_url(v.product_link), self._rewrite_url(v.category_link), self._clean_text(v.tags),
                        self._clean_text(v.brand_name), self._clean_text(v.manufacturer), self._clean_text(v.country_of_origin)
                    ]
                    for attr in dynamic_attrs:
                        # Приоритет: свои атрибуты варианта -> атрибуты родителя
                        # modification_attributes НЕ попадают в файл _goods (только в _attrs)
                        val = (
                            v.attributes.get(attr, "") 
                            or v.attributes.get(f"attr_{attr}", "") 
                            or p.attributes.get(attr, "") 
                            or p.attributes.get(f"attr_{attr}", "")
                        )
                        v_row.append(self._clean_text(val))
                    writer.writerow(v_row)

        # logger.info(f"💾 [EXPORT] Сохранен основной каталог товаров: {filepath}")

    def export_price(self, parents: List[ProductParent], extra_id_keys: List[str], filename: str):
        filepath = os.path.join(self.output_dir, filename)
        headers = ["product_id", "parent_product_id"] + extra_id_keys + ["sku", "price", "fact_price", "bonus", "currency"]
        
        with self._open_csv(filename) as f:
            writer = csv.writer(f, delimiter=self.delimiter)
            writer.writerow(headers)

            for p in parents:
                p_extra_vals = [self._clean_text(getattr(p, k, "")) for k in extra_id_keys]
                writer.writerow([p.product_id, ""] + p_extra_vals + [p.sku, p.price, p.fact_price, self._clean_text(p.bonus), p.currency])

                for v in p.variants:
                    if v.product_id == p.product_id:
                        continue
                    v_extra_vals = [self._clean_text(getattr(v, k, "")) for k in extra_id_keys]
                    writer.writerow([v.product_id, v.parent_product_id] + v_extra_vals + [v.sku, v.price, v.fact_price, self._clean_text(v.bonus), v.currency])       
        # logger.info(f"💾 [EXPORT] Сохранен прайс-лист: {filepath}")

    def export_stock(self, parents: List[ProductParent], extra_id_keys: List[str], filename: str):
        filepath = os.path.join(self.output_dir, filename)
        headers = ["product_id", "parent_product_id"] + extra_id_keys + ["sku", "available", "stock"]

        with self._open_csv(filename) as f:
            writer = csv.writer(f, delimiter=self.delimiter)
            writer.writerow(headers)

            for p in parents:
                p_extra_vals = [self._clean_text(getattr(p, k, "")) for k in extra_id_keys]
                writer.writerow([p.product_id, ""] + p_extra_vals + [p.sku, p.available, p.stock])

                for v in p.variants:
                    if v.product_id == p.product_id:
                        continue
                    v_extra_vals = [self._clean_text(getattr(v, k, "")) for k in extra_id_keys]
                    writer.writerow([v.product_id, v.parent_product_id] + v_extra_vals + [v.sku, v.available, v.stock])                                      
        # logger.info(f"💾 [EXPORT] Сохранен остаток складов: {filepath}")

    def export_extended(self, parents: List[ProductParent], extra_id_keys: List[str], filename: str):
        filepath = os.path.join(self.output_dir, filename)
        headers = ["product_id", "parent_product_id"] + extra_id_keys + [
            "sku", "rules_of_use", "links_rules_of_use", 
            "instructions", "links_instructions", "reviews", "links_reviews"
        ]

        with self._open_csv(filename) as f:
            writer = csv.writer(f, delimiter=self.delimiter)
            writer.writerow(headers)

            for p in parents:
                p_extra_vals = [self._clean_text(getattr(p, k, "")) for k in extra_id_keys]
                writer.writerow([
                    p.product_id, ""] + p_extra_vals + [
                    p.sku, self._clean_text(p.rules_of_use), self._rewrite_url(p.links_rules_of_use), 
                    self._clean_text(p.instructions), self._rewrite_url(p.links_instructions), 
                    self._clean_text(p.reviews), self._rewrite_url(p.links_reviews)
                ])

                for v in p.variants:
                    if v.product_id == p.product_id:
                        continue
                    v_extra_vals = [self._clean_text(getattr(v, k, "")) for k in extra_id_keys]
                    writer.writerow([
                        v.product_id, v.parent_product_id] + v_extra_vals + [
                        v.sku, self._clean_text(v.rules_of_use), self._rewrite_url(v.links_rules_of_use), 
                        self._clean_text(v.instructions), self._rewrite_url(v.links_instructions), 
                        self._clean_text(v.reviews), self._rewrite_url(v.links_reviews)
                    ])
        # logger.info(f"💾 [EXPORT] Сохранен расширенный сервис-файл: {filepath}")

    def _get_all_modification_attribute_keys(self, parents: List[ProductParent]) -> List[str]:
        mod_keys: Set[str] = set()
        for parent in parents:
            for k, v in parent.modification_attributes.items():
                if v and str(v).strip():
                    clean_k = k[13:] if k.startswith("modification_") else k
                    clean_k = clean_k.strip()
                    if clean_k and len(clean_k) <= 35 and "\n" not in clean_k:
                        mod_keys.add(clean_k)
            for variant in parent.variants:
                for k, v in variant.modification_attributes.items():
                    if v and str(v).strip():
                        clean_k = k[13:] if k.startswith("modification_") else k
                        clean_k = clean_k.strip()
                        if clean_k and len(clean_k) <= 35 and "\n" not in clean_k:
                            mod_keys.add(clean_k)
        return sorted(list(mod_keys))

    def _get_mod_value(self, obj, attr_name: str) -> str:
        """Безопасный регистронезависимый поиск значения модификации."""
        mods = getattr(obj, "modification_attributes", {})
        if not mods:
            return ""
        if attr_name in mods:
            return self._clean_text(mods[attr_name])
        if f"modification_{attr_name}" in mods:
            return self._clean_text(mods[f"modification_{attr_name}"])
        
        attr_lower = attr_name.lower().strip()
        for k, v in mods.items():
            clean_k = k[13:] if k.startswith("modification_") else k
            if clean_k.lower().strip() == attr_lower:
                return self._clean_text(v)
        return ""

    def export_attrs(self, parents: List[ProductParent], mod_attrs: List[str], extra_id_keys: List[str], filename: str):
        filepath = os.path.join(self.output_dir, filename)
        base_headers = ["product_id", "parent_product_id"] + extra_id_keys + ["sku"]
        mod_headers = [f"modification_{attr}" for attr in mod_attrs]
        tail_headers = ["price", "fact_price", "bonus", "currency", "available", "stock", "image", "sales_notes"]
        
        with self._open_csv(filename) as f:
            writer = csv.writer(f, delimiter=self.delimiter)
            writer.writerow(base_headers + mod_headers + tail_headers)

            for p in parents:
                # 1. Запись родителя
                p_extra_vals = [self._clean_text(getattr(p, k, "")) for k in extra_id_keys]
                p_row = [p.product_id, ""] + p_extra_vals + [p.sku]

                default_mods = getattr(p, 'default_variant_mods', {})
                for attr in mod_attrs:
                    val = ""
                    # Сначала ищем в дефолтных модификациях
                    for k, v in default_mods.items():
                        if k.lower() == attr.lower() or f"modification_{k}".lower() == attr.lower():
                            val = v
                            break
                    
                    # Если не нашли в дефолтных, фолбэк на старые modification_attributes (на всякий случай)
                    if not val:
                        val = self._get_mod_value(p, attr)
                        
                    p_row.append(self._clean_text(val))
                p_row.extend([
                    p.price, p.fact_price, self._clean_text(p.bonus), p.currency,
                    p.available, p.stock, self._rewrite_url(p.image), self._clean_text(p.sales_notes)
                ])
                writer.writerow(p_row)

                # 2. Запись всех вариантов
                for v in p.variants:
                    # === ТОЧКА 3: ДИАГНОСТИКА ЭКСПОРТА ВАРИАНТОВ ===
                    if v.product_id == p.product_id:
                        logger.error(f"❌ [DIAG 3] КРИТИЧЕСКАЯ ОШИБКА: Пытаемся экспортировать вариант с ID={v.product_id}, который равен ID родителя {p.product_id}! SKU: {v.sku}, Bonus: '{v.bonus}'")
                    # ==========================================
                    export_product_id = v.parent_product_id if getattr(v, 'is_synthetic', False) else v.product_id
                    
                    v_extra_vals = [self._clean_text(getattr(v, k, "")) for k in extra_id_keys]
                    v_row = [export_product_id, v.parent_product_id] + v_extra_vals + [v.sku]
                    
                    for attr in mod_attrs:
                        v_row.append(self._get_mod_value(v, attr))
                    v_row.extend([
                        v.price, v.fact_price, self._clean_text(v.bonus), v.currency,
                        v.available, v.stock, self._rewrite_url(v.image), self._clean_text(v.sales_notes)
                    ])
                    writer.writerow(v_row)

                # 3. Запись модификаций, которые были слиты с родителем (ID варианта == ID родителя)
                for mod in getattr(p, 'modifications', []):
                    combo = mod.get('combo', {})
                    mod_sku = f"{p.sku}_{'-'.join(str(v) for v in combo.values())}" if p.sku else p.product_id
                    
                    mod_row = [p.product_id, p.product_id] + [self._clean_text(getattr(p, k, "")) for k in extra_id_keys] + [mod_sku]
                    
                    for attr in mod_attrs:
                        val = ""
                        for k, v in combo.items():
                            if k.lower() == attr.lower() or f"modification_{k}".lower() == attr.lower():
                                val = v
                                break
                        mod_row.append(self._clean_text(val))
                        
                    mod_row.extend([
                        mod.get('old_price', p.price), 
                        mod.get('price', p.fact_price), 
                        self._clean_text(mod.get('bonus', p.bonus)), 
                        p.currency,
                        p.available, 
                        p.stock, 
                        self._rewrite_url(mod.get('image', p.image)), 
                        self._clean_text(p.sales_notes)
                    ])
                    writer.writerow(mod_row)

        # logger.info(f"💾 [EXPORT] Сохранен матричный файл модификаций: {filepath}")

    def export_all(self, categories: List[Category], parents: List[ProductParent], prefix: str = "catalog"):
        self.current_prefix = prefix
        timestamp_dir = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        target_dir = os.path.join(self.output_dir, prefix, timestamp_dir)
        os.makedirs(target_dir, exist_ok=True)

        old_output_dir = self.output_dir
        self.output_dir = target_dir

        try:
            dynamic_attrs = self._get_all_attribute_keys(parents)
            mod_attrs = self._get_all_modification_attribute_keys(parents)
            extra_id_keys = self._get_extra_id_keys(parents)

            self.export_categories(categories, f"{prefix}_cats.csv")
            self.export_goods(parents, dynamic_attrs, extra_id_keys, f"{prefix}_goods.csv")
            self.export_attrs(parents, mod_attrs, extra_id_keys, f"{prefix}_attrs.csv")
            self.export_price(parents, extra_id_keys, f"{prefix}_price.csv")
            self.export_stock(parents, extra_id_keys, f"{prefix}_stock.csv")
            self.export_extended(parents, extra_id_keys, f"{prefix}_extended.csv")
            logger.info(f"✅ [SUCCESS] Все 6 файлов успешно сгенерированы в '{target_dir}'!")
        finally:
            self.output_dir = old_output_dir