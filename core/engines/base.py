from abc import ABC, abstractmethod
from typing import List, Optional
from playwright.async_api import Page
from core.models import ProductParent
from core.state_machine import CatalogStateMachine

class BaseEngine(ABC):
    """Базовый абстрактный класс для всех движков парсинга."""

    def __init__(self, parser_instance):
        self.parser = parser_instance

    @abstractmethod
    async def can_handle(self, page: Page, url: str) -> bool:
        pass

    @abstractmethod
    async def extract_catalog_links(self, page: Page) -> List[str]:
        pass

    @abstractmethod
    async def parse_product_page(
        self, 
        page: Page, 
        url: str, 
        state_machine: Optional[CatalogStateMachine] = None
    ) -> Optional[ProductParent]:
        """Парсит карточку товара и возвращает объект ProductParent."""
        pass