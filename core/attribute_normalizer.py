"""
Модуль для нормализации и классификации сырых атрибутов через LLM.
Преобразует raw_attributes в структурированные attributes и modification_attributes.
"""
import json
import logging
import re
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
        """Строит универсальный промпт для LLM для нормализации атрибутов."""
        
        # Форматируем сырые данные для отображения в промпте
        parent_attrs_str = json.dumps(raw_data["parent_raw"], ensure_ascii=False, indent=2)
        
        variants_str = ""
        for v in raw_data["variants"]:
            variants_str += f"\n--- Вариант: {v['title']} (SKU: {v['sku']}) ---\n"
            variants_str += json.dumps(v["raw_attributes"], ensure_ascii=False, indent=2)
        
        prompt = f"""
Ты — интеллектуальный анализатор данных интернет-магазина.
Твоя задача: проанализировать сырые атрибуты товара и его вариантов, чтобы разделить их на две категории:

1. **Статические атрибуты (static_attributes)**: Характеристики, которые ОДИНАКОВЫ для всех вариантов товара.
   Примеры: Бренд, Материал корпуса, Страна производства, Гарантия, Тип экрана, Количество колес.
   Эти данные будут записаны только в родительский товар.

2. **Ключи модификаций (modification_keys)**: Характеристики, которые ОТЛИЧАЮТСЯ у разных вариантов.
   Это НЕ обязательно только цвет и размер. Это может быть: Объем памяти, Мощность, Тип ткани, Комплектация, Год выпуска и т.д.
   Ключи должны быть на английском в snake_case (например: 'color', 'size', 'storage_capacity', 'power', 'fabric_type').

ВХОДНЫЕ ДАННЫЕ:
=== РОДИТЕЛЬСКИЙ ТОВАР ===
{parent_attrs_str}

=== ВАРИАНТЫ ===
{variants_str}

ПРАВИЛА АНАЛИЗА:
1. Сравни значения атрибутов across всех вариантов.
2. Если значение атрибута (или часть названия) меняется от варианта к варианту — это ключ модификации.
   - Пример: "Цвет: Красный" vs "Цвет: Синий" → ключ "color".
   - Пример: "Память: 128GB" vs "Память: 256GB" → ключ "storage_capacity".
3. Если атрибут одинаков для всех — это static_attributes.
4. Названия ключей в результате должны быть на английском языке в snake_case.
5. variant_modifications: сопоставь значения модификаций с product_id вариантов.

ВЕРНИ ТОЛЬКО валидный JSON в формате:
{{
  "static_attributes": {{"название_атрибута_en": "значение", ...}},
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
            logger.info("🤖 [LLM CALL] Отправка запроса на нормализацию атрибутов...")
            response = await self.ollama_client.generate(prompt)
            
            logger.debug(f"📥 [LLM RESPONSE] Получен ответ типа {type(response)}: {str(response)[:200]}...")
            
            # Парсим JSON из ответа
            # Предполагаем, что ollama_client.generate возвращает строку или dict
            if isinstance(response, str):
                logger.debug("📝 [LLM RESPONSE] Ответ - строка, пытаемся найти JSON...")
                # Ищем JSON в ответе
                start_idx = response.find('{')
                end_idx = response.rfind('}') + 1
                if start_idx >= 0 and end_idx > start_idx:
                    json_str = response[start_idx:end_idx]
                    logger.debug(f"📦 [LLM JSON] Извлечена JSON строка: {json_str[:100]}...")
                    result = json.loads(json_str)
                    logger.info("✅ [LLM PARSE] JSON успешно распарсен")
                    return result
                else:
                    logger.warning("⚠️ [LLM PARSE] JSON не найден в ответе")
            elif isinstance(response, dict):
                logger.info("✅ [LLM RESPONSE] Ответ уже является словарем")
                return response
            else:
                logger.warning(f"⚠️ [LLM RESPONSE] Неожиданный тип ответа: {type(response)}")
                
        except json.JSONDecodeError as e:
            logger.error(f"❌ [NORMALIZER] Ошибка парсинга JSON от LLM: {e}")
        except Exception as e:
            logger.error(f"❌ [NORMALIZER] Ошибка вызова LLM: {e}")
        
        return None
    
    def _apply_llm_results(self, parent: ProductParent, llm_data: Dict) -> None:
        """Применяет результаты нормализации от LLM."""
        logger.info(f"✅ [LLM APPLY] Применяем результаты нормализации для {parent.product_id}")
        
        # Логгируем сырые данные от LLM для отладки
        logger.debug(f"🔍 [LLM DEBUG] Сырые данные от LLM: {json.dumps(llm_data, ensure_ascii=False, indent=2)}")
        
        # Применяем статические атрибуты к родителю
        static_attrs = llm_data.get("static_attributes", {})
        if static_attrs:
            parent.attributes = {str(k).strip(): str(v).strip() for k, v in static_attrs.items() if k and v}
            logger.info(f"📋 [LLM APPLY] Записано {len(parent.attributes)} статических атрибутов в родителя")
        else:
            logger.warning("⚠️ [LLM APPLY] LLM не вернул статические атрибуты")
        
        # Получаем ключи модификаций
        raw_keys = response.get('modification_keys', [])
        # Жесткий фильтр системных полей
        blacklist = {'sku', 'id', 'article', 'code', 'код', 'арт', 'номер'}
        modification_keys = [k for k in raw_keys if k.lower() not in blacklist]

        if len(raw_keys) != len(modification_keys):
            print(f"🛡️ [FILTER] Исключены ключи: {set(raw_keys) - set(modification_keys)}")
        
        # Применяем модификации к вариантам
        variant_mods = llm_data.get("variant_modifications", {})
        logger.debug(f"🔍 [LLM DEBUG] variant_modifications от LLM: {variant_mods}")
        
        # Собираем все возможные ключи для сопоставления: product_id (int и str) и SKU
        variant_id_map = {}
        variant_sku_map = {}
        for v in parent.variants:
            # Маппинг по ID
            variant_id_map[str(v.product_id)] = v
            variant_id_map[v.product_id] = v  # На случай если ключ числовой
            logger.debug(f"🗺️ [LLM DEBUG] Зарегистрирован вариант ID: {v.product_id} (str: '{str(v.product_id)}')")
            
            # Маппинг по SKU
            if v.sku:
                variant_sku_map[str(v.sku).strip().lower()] = v
                logger.debug(f"🗺️ [LLM DEBUG] Зарегистрирован вариант SKU: {v.sku}")
        
        applied_count = 0
        for llm_key, v_mods in variant_mods.items():
            logger.debug(f"🔍 [LLM DEBUG] Обработка ключа LLM '{llm_key}' -> {v_mods}")
            
            # Ищем вариант сначала по ID, потом по SKU
            v = variant_id_map.get(llm_key) or variant_id_map.get(str(llm_key))
            if not v:
                # Если не нашли по ID, пробуем найти по SKU (нормализуя регистр)
                v = variant_sku_map.get(str(llm_key).strip().lower())
                if v:
                    logger.debug(f"✅ [LLM APPLY] Найден вариант по SKU '{llm_key}' -> ID {v.product_id}")
            
            if v and v_mods:
                for mk, mv in v_mods.items():
                    if mk and mv:
                        v.modification_attributes[str(mk).strip()] = str(mv).strip()
                        applied_count += 1
                        logger.debug(f"✏️ [LLM APPLY] Вариант {v.product_id}: записана модификация '{mk}' = '{mv}'")
            else:
                logger.warning(f"⚠️ [LLM APPLY] Не найден вариант для ключа LLM '{llm_key}'. Доступные IDs: {list(variant_id_map.keys())}")
        
        logger.info(f"✅ [LLM APPLY] Всего применено {applied_count} модификаций к вариантам")
    
    def _apply_heuristic_normalization(self, parent: ProductParent) -> None:
        """
        Улучшенная эвристическая нормализация без LLM.
        Приоритет: данные из special_fields (найденные парсером) > анализ SKU/названия > raw_attributes.
        """
        logger.info(f"🔧 [NORMALIZER] Применяем улучшенную эвристику для {parent.product_id}")
        
        all_products = [parent] + parent.variants
        
        # ЭТАП 1: Перенос данных из special_fields (если парсер уже нашел модификации)
        for prod in all_products:
            if hasattr(prod, 'special_fields') and prod.special_fields:
                if 'color' in prod.special_fields and not prod.modification_attributes.get('color'):
                    prod.modification_attributes['color'] = prod.special_fields['color']
                    logger.debug(f"✅ Перенесен цвет из special_fields для {prod.product_id}: {prod.special_fields['color']}")
                if 'size' in prod.special_fields and not prod.modification_attributes.get('size'):
                    prod.modification_attributes['size'] = prod.special_fields['size']
                    logger.debug(f"✅ Перенесен размер из special_fields для {prod.product_id}: {prod.special_fields['size']}")

        # ЭТАП 2: Эвристический анализ для тех, у кого модификации еще не заполнены
        for prod in all_products:
            # Пропускаем, если уже есть и цвет, и размер
            if prod.modification_attributes.get('color') and prod.modification_attributes.get('size'):
                continue

            text_to_analyze = f"{prod.title} {prod.sku}".lower()
            
            # --- Определение цвета ---
            if not prod.modification_attributes.get('color'):
                color_candidates = ['black', 'white', 'red', 'blue', 'green', 'olive', 'pink', 'gold', 
                                    'champagne', 'tiffany', 'silver', 'grey', 'gray', 'brown', 'orange', 
                                    'purple', 'yellow', 'beige', 'navy', 'cyan', 'magenta']
                
                # Поиск в названии и SKU
                for color in color_candidates:
                    if color in text_to_analyze:
                        prod.modification_attributes['color'] = color
                        logger.debug(f"🎨 Найден цвет в названии/SKU для {prod.product_id}: {color}")
                        break
                
                # Если не найдено, пробуем найти в raw_attributes
                if not prod.modification_attributes.get('color'):
                    for raw in prod.raw_attributes or []:
                        if isinstance(raw, dict):
                            val = str(list(raw.values())[0]).lower() if raw else ""
                        elif isinstance(raw, str):
                            val = raw.lower()
                        else:
                            continue
                        
                        for color in color_candidates:
                            if color in val:
                                prod.modification_attributes['color'] = raw if isinstance(raw, str) else list(raw.values())[0]
                                break
                        if prod.modification_attributes.get('color'):
                            break

            # --- Определение размера / типа комплекта ---
            if not prod.modification_attributes.get('size'):
                # 1. Проверка на наборы (Set, Kit, 3-piece)
                if any(x in text_to_analyze for x in ['set', 'набір', 'kit', 'piece', '3-', 'три ', 'комплект']):
                    prod.modification_attributes['size'] = 'Set'
                    logger.debug(f"📦 Найден набор для {prod.product_id}")
                else:
                    # 2. Поиск цифр в SKU, отличающихся от родителя
                    if prod.parent_product_id and parent.sku:
                        parent_numbers = set(re.findall(r'\d+', parent.sku or ""))
                        current_numbers = set(re.findall(r'\d+', prod.sku or ""))
                        diff_numbers = current_numbers - parent_numbers
                        
                        if diff_numbers:
                            # Фильтруем: оставляем числа длиной 2-3 цифры (размеры типа 55, 65, 75)
                            candidates = [n for n in diff_numbers if 2 <= len(n) <= 3]
                            if candidates:
                                prod.modification_attributes['size'] = max(candidates) # Берем наибольшее
                                logger.debug(f"📏 Найден размер через разницу SKU для {prod.product_id}: {prod.modification_attributes['size']}")
                    
                    # 3. Поиск явных маркеров размера в названии (например, "65 см")
                    if not prod.modification_attributes.get('size'):
                        match = re.search(r'\b(\d{2,3})\s*["\']?\s*(cm|см|inch|дюйм)?\b', text_to_analyze)
                        if match:
                            prod.modification_attributes['size'] = match.group(1)
                            logger.debug(f"📏 Найден размер в названии для {prod.product_id}: {prod.modification_attributes['size']}")

        # ЭТАП 3: Заполнение статических атрибутов из raw_attributes (если они есть)
        # Собираем общие атрибуты, которые не являются модификациями
        static_attrs = {}
        mod_keys = {'color', 'size', 'volume', 'weight', 'memory'}
        
        for item in parent.raw_attributes or []:
            if isinstance(item, dict):
                for k, v in item.items():
                    key_lower = str(k).lower().strip()
                    if not any(mk in key_lower for mk in mod_keys):
                        static_attrs[str(k).strip()] = str(v).strip()
        
        if static_attrs:
            parent.attributes = static_attrs
            logger.debug(f"📋 Добавлено {len(static_attrs)} статических атрибутов для родителя {parent.product_id}")
