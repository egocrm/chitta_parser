"""
Модуль для нормализации и классификации сырых атрибутов через LLM.
Преобразует raw_attributes в структурированные attributes и modification_attributes.
"""
import json
import logging
from typing import Dict, List, Any, Optional, Tuple
from core.models import ProductParent, ProductVariant

logger = logging.getLogger("ChittaParser")


class AttributeNormalizer:
    """Нормализует и классифицирует атрибуты с помощью LLM."""
    
    def __init__(self, ollama_client=None):
        self.ollama_client = ollama_client
    
    async def normalize_parent_attributes(self, parent: ProductParent) -> None:
        """
        Нормализует все сырые атрибуты родителя и его вариантов.
        
        Логика:
        1. Собираем все raw_attributes из родителя и вариантов
        2. Отправляем в LLM для определения:
           - Названий атрибутов (если они не были найдены парсером)
           - Какие атрибуты являются общими (статическими)
           - Какие атрибуты являются модификациями (различаются у вариантов)
        3. Заполняем structured attributes и modification_attributes
        """
        if not parent.variants:
            # Если нет вариантов, просто очищаем raw_attributes
            parent.raw_attributes = []
            return
        
        # Шаг 1: Агрегация всех сырых данных
        all_raw_data = self._aggregate_raw_attributes(parent)
        
        if not all_raw_data:
            logger.warning(f"⚠️ [NORMALIZER] Нет сырых атрибутов для нормализации у товара {parent.product_id}")
            return
        
        # Шаг 2: Формирование промпта для LLM
        prompt = self._build_normalization_prompt(parent, all_raw_data)
        
        # Шаг 3: Вызов LLM
        try:
            llm_response = await self._call_llm(prompt)
            if llm_response:
                # Шаг 4: Парсинг ответа и применение результатов
                self._apply_llm_results(parent, llm_response)
            else:
                # Фолбэк: простая эвристика без LLM
                self._apply_heuristic_normalization(parent)
        except Exception as e:
            logger.error(f"❌ [NORMALIZER] Ошибка при нормализации атрибутов: {e}")
            # Фолбэк на эвристику
            self._apply_heuristic_normalization(parent)
        
        # Очищаем raw_attributes после нормализации
        parent.raw_attributes = []
        for v in parent.variants:
            v.raw_attributes = []
    
    def _aggregate_raw_attributes(self, parent: ProductParent) -> Dict[str, Any]:
        """Собирает все raw_attributes в единую структуру."""
        result = {
            "parent_raw": parent.raw_attributes or [],
            "variants": []
        }
        
        for v in parent.variants:
            variant_data = {
                "product_id": v.product_id,
                "title": v.title,
                "sku": v.sku,
                "raw_attributes": v.raw_attributes or []
            }
            result["variants"].append(variant_data)
        
        return result
    
    def _build_normalization_prompt(self, parent: ProductParent, raw_data: Dict) -> str:
        """Строит промпт для LLM для нормализации атрибутов."""
        
        # Форматируем сырые данные для отображения в промпте
        parent_attrs_str = json.dumps(raw_data["parent_raw"], ensure_ascii=False, indent=2)
        
        variants_str = ""
        for v in raw_data["variants"]:
            variants_str += f"\n--- Вариант: {v['title']} (SKU: {v['sku']}) ---\n"
            variants_str += json.dumps(v["raw_attributes"], ensure_ascii=False, indent=2)
        
        prompt = f"""
Ты — эксперт по нормализации данных интернет-магазинов.
Твоя задача: проанализировать сырые атрибуты товара и его вариантов, определить:
1. Корректные названия атрибутов (некоторые могут быть без имени, только значение)
2. Какие атрибуты являются ОБЩИМИ для всех вариантов (статические характеристики)
3. Какие атрибуты являются МОДИФИКАЦИЯМИ (отличают варианты друг от друга, например цвет, размер)

ВХОДНЫЕ ДАННЫЕ:
=== РОДИТЕЛЬСКИЙ ТОВАР ===
{parent_attrs_str}

=== ВАРИАНТЫ ===
{variants_str}

ПРАВИЛА:
1. Модификации — это обычно: цвет, размер, объем памяти, вес, комплектация.
2. Статические атрибуты — это: материал, бренд, страна, гарантия, общие технические характеристики.
3. Если у атрибута нет явного имени, определи его по контексту значения.
4. Верни ТОЛЬКО валидный JSON в формате:
{{
  "static_attributes": {{"название_атрибута": "значение", ...}},
  "modification_keys": ["ключ1", "ключ2"],
  "variant_modifications": {{
    "product_id_варианта": {{"ключ_модификации": "значение", ...}},
    ...
  }}
}}

ОТВЕТ (только JSON):
"""
        return prompt
    
    async def _call_llm(self, prompt: str) -> Optional[Dict]:
        """Вызывает LLM для нормализации атрибутов."""
        if not self.ollama_client:
            logger.warning("⚠️ [NORMALIZER] Ollama клиент не инициализирован, используем эвристику")
            return None
        
        try:
            response = await self.ollama_client.generate(prompt)
            # Парсим JSON из ответа
            # Предполагаем, что ollama_client.generate возвращает строку
            if isinstance(response, str):
                # Ищем JSON в ответе
                start_idx = response.find('{')
                end_idx = response.rfind('}') + 1
                if start_idx >= 0 and end_idx > start_idx:
                    json_str = response[start_idx:end_idx]
                    return json.loads(json_str)
            elif isinstance(response, dict):
                return response
        except Exception as e:
            logger.error(f"❌ [NORMALIZER] Ошибка вызова LLM: {e}")
        
        return None
    
    def _apply_llm_results(self, parent: ProductParent, llm_data: Dict) -> None:
        """Применяет результаты нормализации от LLM."""
        
        # Применяем статические атрибуты к родителю
        static_attrs = llm_data.get("static_attributes", {})
        parent.attributes = {str(k).strip(): str(v).strip() for k, v in static_attrs.items() if k and v}
        
        # Получаем ключи модификаций
        mod_keys = llm_data.get("modification_keys", [])
        for mk in mod_keys:
            if mk and str(mk).strip():
                parent.modification_attributes[str(mk).strip()] = ""
        
        # Применяем модификации к вариантам
        variant_mods = llm_data.get("variant_modifications", {})
        for v in parent.variants:
            v_mods = variant_mods.get(str(v.product_id), {})
            if v_mods:
                for mk, mv in v_mods.items():
                    if mk and mv:
                        v.modification_attributes[str(mk).strip()] = str(mv).strip()
    
    def _apply_heuristic_normalization(self, parent: ProductParent) -> None:
        """
        Простая эвристическая нормализация без LLM.
        Используется как фолбэк или для быстрых тестов.
        """
        logger.info(f"🔧 [NORMALIZER] Применяем эвристическую нормализацию для {parent.product_id}")
        
        # Словари известных модификаций
        mod_keywords = {
            "color": ["цвет", "color", "колір"],
            "size": ["размер", "size", "розмір", "габарит"],
            "volume": ["объем", "volume", "об'єм", "літраж"],
            "weight": ["вес", "weight", "вага"],
            "memory": ["память", "memory", "пам'ять", "gb", "гб"],
        }
        
        # Собираем все уникальные ключи из raw_attributes
        all_keys = set()
        all_values = {}
        
        for item in parent.raw_attributes:
            if isinstance(item, dict):
                for k, v in item.items():
                    if k and v:
                        all_keys.add(str(k).lower().strip())
                        all_values[str(k).strip()] = str(v).strip()
        
        for v in parent.variants:
            for item in v.raw_attributes:
                if isinstance(item, dict):
                    for k, val in item.items():
                        if k and val:
                            all_keys.add(str(k).lower().strip())
                            all_values[str(k).strip()] = str(val).strip()
        
        # Классифицируем ключи
        static_attrs = {}
        mod_attrs_keys = set()
        
        for key_lower in all_keys:
            is_mod = False
            for mod_key, keywords in mod_keywords.items():
                if any(kw in key_lower for kw in keywords):
                    mod_attrs_keys.add(key_lower)
                    is_mod = True
                    break
            
            if not is_mod:
                # Находим оригинальный ключ (с правильным регистром)
                orig_key = next((k for k in all_values.keys() if k.lower().strip() == key_lower), key_lower)
                static_attrs[orig_key] = all_values.get(orig_key, "")
        
        # Применяем к родителю
        parent.attributes = static_attrs
        for mk in mod_attrs_keys:
            orig_key = next((k for k in all_values.keys() if k.lower().strip() == mk), mk)
            parent.modification_attributes[orig_key] = ""
        
        # Заполняем модификации вариантов
        for v in parent.variants:
            for item in v.raw_attributes:
                if isinstance(item, dict):
                    for k, val in item.items():
                        if k and val:
                            key_lower = str(k).lower().strip()
                            if key_lower in mod_attrs_keys:
                                v.modification_attributes[k.strip()] = str(val).strip()
