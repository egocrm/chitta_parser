import logging
from typing import List, Optional
from playwright.async_api import Page
from core.engines.base import BaseEngine
from core.models import ProductParent
from core.state_machine import CatalogStateMachine

logger = logging.getLogger("ChittaParser")

class GenericEngine(BaseEngine):
    """Универсальный движок-фолбэк, делегирующий полный цикл парсинга UniversalSemanticParser."""

    async def can_handle(self, page: Page, url: str) -> bool:
        return True

    async def extract_catalog_links(self, page: Page) -> List[str]:
        return await self.parser.extract_product_links(page)

    async def parse_product_page(
        self, 
        page: Page, 
        url: str, 
        state_machine: Optional[CatalogStateMachine] = None,
        existing_product: Optional[ProductParent] = None
    ) -> Optional[ProductParent]:
        if not state_machine:
            state_machine = CatalogStateMachine(max_parents=100)

        # Передаем управление в единый центр UniversalSemanticParser
        success = await self.parser.parse_page(page, url, state_machine, existing_product)
        if success:
            parents = state_machine.get_all_parents()
            if parents:
                return parents[-1]
            if existing_product:
                return existing_product
        return None