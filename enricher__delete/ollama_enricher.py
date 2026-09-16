import json
import logging
import requests
from core.models import ProductParent

logger = logging.getLogger("ChittaParser")

class OllamaEnricher:
    """Извлечение фактических данных (условия, опт, примечания) из HTML через локальную модель Ollama."""

    def __init__(self, model_name: str = "qwen2.5-coder:3b", host: str = "http://localhost:11434"):
        self.model_name = model_name
        self.host = host

    def enrich_product(self, html_content: str, parent: ProductParent) -> ProductParent:
        """Анализирует HTML и заполняет фактические поля (sales_notes, add_description) без выдумывания."""
        
        prompt = f"""
You are a strict e-commerce data extraction engine. Analyze the provided product page HTML.
Your task is to extract ONLY explicitly stated factual information. Do NOT invent, assume, or fabricate any data. If information is missing, leave the field empty.

Extract into JSON format with exact keys:
1. "sales_notes": Look for bulk pricing conditions (e.g. "при замовленні від X шт"), minimum order requirements, promotional notes, or payment/delivery warnings.
2. "add_description": Look for additional specifications, materials, dimensions, or details not present in the title.

Return ONLY valid JSON. No markdown wrappers around JSON if possible, or standard JSON object.

HTML CONTENT:
{html_content[:12000]}
"""

        try:
            response = requests.post(
                f"{self.host}/api/generate",
                json={
                    "model": self.model_name,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json"
                },
                timeout=40
            )
            
            if response.status_code == 200:
                result = response.json()
                res_text = result.get("response", "{}")
                
                # Очистка от возможных markdown-оберток
                res_text = res_text.replace("```json", "").replace("```", "").strip()
                data = json.loads(res_text)

                if data.get("sales_notes"):
                    parent.sales_notes = str(data["sales_notes"]).strip()
                    logger.info(f"🤖 [OLLAMA] Фактический sales_notes: {parent.sales_notes}")

                if data.get("add_description"):
                    parent.add_description = str(data["add_description"]).strip()
                    logger.info(f"🤖 [OLLAMA] Фактический add_description: {parent.add_description}")

        except requests.exceptions.ConnectionError:
            logger.warning("⚠️ [OLLAMA] Не удалось подключиться к локальному серверу Ollama (проверьте, запущен ли ollama serve).")
        except Exception as e:
            logger.warning(f"⚠️ [OLLAMA] Ошибка обработки ответа модели: {e}")

        return parent