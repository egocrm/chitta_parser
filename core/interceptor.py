import asyncio
import logging
from typing import List, Dict, Any
from playwright.async_api import Page, Response

logger = logging.getLogger("ChittaParser")

class NetworkInterceptor:
    """Перехватчик сетевого XHR/Fetch трафика для извлечения сырых JSON-ответов API."""

    def __init__(self):
        self.payloads: List[Dict[str, Any]] = []

    def attach(self, page: Page):
        """Подключает прослушиватель ответов к странице Playwright."""
        page.on("response", lambda response: asyncio.create_task(self._handle_response(response)))

    async def _handle_response(self, response: Response):
        try:
            content_type = response.headers.get("content-type", "").lower()
            url = response.url

            # Игнорируем статичные медиа-ресурсы
            if any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp', '.css', '.js', '.woff', '.svg']):
                return

            if "application/json" in content_type or "json" in content_type:
                try:
                    json_data = await response.json()
                    if json_data:
                        self.payloads.append({"url": url, "json": json_data})
                        logger.info(f"🌐 [NETWORK] Перехвачен JSON-пакет [{len(self.payloads)}]: {url}")
                except Exception:
                    pass
        except Exception:
            pass

    def get_payloads(self) -> List[Dict[str, Any]]:
        return self.payloads

    def clear(self):
        self.payloads.clear()