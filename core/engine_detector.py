import logging
from typing import List
from playwright.async_api import Page
from core.engines.base import BaseEngine
from core.engines.shopify import ShopifyEngine
from core.engines.prom import PromEngine
from core.engines.opencart import OpenCartEngine
from core.json_engine import JSONEngine  # Импорт нового движка
from core.engines.generic import GenericEngine

logger = logging.getLogger("ChittaParser")


class EngineDetector:
    """Диспетчер автоматического определения специализированного движка."""

    def __init__(self, parser_instance):
        self.engines: List[BaseEngine] = [
            ShopifyEngine(parser_instance),
            PromEngine(parser_instance),
            OpenCartEngine(parser_instance),
            JSONEngine(parser_instance),     # JSON-движок перед Generic-фолбэком
            GenericEngine(parser_instance)  # Фолбэк для всего остального
        ]
        self.engine_map = {
            "shopify": self.engines[0],
            "prom-ua": self.engines[1],
            "opencart": self.engines[2],
            "json": self.engines[3],
            "general": self.engines[4]
        }

    async def select_engine(self, page: Page, url: str, forced_engine: str = "general") -> BaseEngine:
        """Определяет подходящий движок под целевой сайт или отдаёт принудительно выбранный."""
        clean_forced = (forced_engine or "general").strip().lower()
        if clean_forced in self.engine_map and clean_forced != "general":
            selected = self.engine_map[clean_forced]
            # logger.info(f"⚙️ [ENGINE DETECTOR] Принудительно выбран движок: {selected.__class__.__name__} (--engine {clean_forced})")
            return selected

        for engine in self.engines:
            if await engine.can_handle(page, url):
                # logger.info(f"⚙️ [ENGINE DETECTOR] Выбран движок: {engine.__class__.__name__} для {url}")
                return engine
        
        return self.engines[-1]