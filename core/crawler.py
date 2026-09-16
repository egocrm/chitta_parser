import logging
from typing import Optional
from playwright.async_api import Page
from core.interceptor import NetworkInterceptor

logger = logging.getLogger(__name__)

async def crawl_catalog_pages(page: Page, interceptor: Optional[NetworkInterceptor] = None, max_pages: int = 10):
    """Обходит страницы каталога, пока есть кнопка 'Следующая' или новые товары."""
    current_page = 1
    
    while current_page <= max_pages:
        logger.info(f"Обработка страницы каталога #{current_page}")
        
        # Скролл вниз для срабатывания lazy-load и Ajax
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1500)
        
        # Поиск кнопки перехода на следующую страницу
        next_button = page.locator('a[rel="next"], a.pagination__next, button.next-page, [data-qa="pagination-next"]').first
        
        if await next_button.is_visible():
            current_page += 1
            await next_button.click()
            await page.wait_for_load_state("networkidle")
        else:
            logger.info("Кнопка 'Следующая страница' не найдена. Обход завершен.")
            break