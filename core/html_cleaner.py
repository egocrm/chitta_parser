# core/html_cleaner.py — Препроцессор HTML с ветвлением по LLM-провайдеру (v2)

import logging
import re
from typing import Optional
from bs4 import BeautifulSoup, Comment
import config

logger = logging.getLogger("ChittaParser")


class HTMLCleaner:
    """Очищает сырой HTML страницы товара до структурного скелета под конкретный LLM-провайдер."""

    # Служебные теги, удаляемые для ВСЕХ провайдеров
    NOISE_TAGS = {
        "script", "style", "svg", "noscript", "iframe", "header", "footer", 
        "nav", "aside", "head", "meta", "link", "canvas", "audio", "video"
    }

    # --- НАСТРОЙКИ ДЛЯ OLLAMA (Агрессивное сжатие для слабых моделей) ---
    OLLAMA_NOISE_SELECTORS = [
        '[class*="mobile-menu" i]', '[class*="main-menu" i]', '[class*="navbar" i]',
        '[class*="cookie" i]', '[class*="banner" i]', '[class*="popup" i]',
        '[class*="modal" i]', '[class*="review" i]', '[class*="comment" i]',
        '[class*="recommend" i]', '[class*="similar" i]', '[class*="related" i]',
        '[class*="viewed" i]', 'footer:not([class*="product" i])', 'header:not([class*="product" i])'
    ]
    OLLAMA_ALLOWED_ATTRIBUTES = {
        "id", "class", "itemprop", "itemtype", "itemscope", 
        "name", "type", "value", "href", "src", "alt", "title", "role"
    }

    # Безопасные селекторы шума для Gemini
    GEMINI_NOISE_SELECTORS = [
        '[class*="mobile-menu" i]', '[class*="main-menu" i]', '[class*="navbar" i]',
        'header.site-header', 'footer.site-footer', '[class*="cookie" i]',
        '[class*="banner" i]', '[class*="popup" i]', '[class*="modal" i]',
        '[class*="review" i]', '[class*="comment" i]', '.catalog-filter',
        '[class*="recommend" i]', '[class*="similar" i]', '[class*="related" i]',
        '[class*="viewed" i]', 'footer:not([class*="product" i])', 'header:not([class*="product" i])'
    ]
    GEMINI_ALLOWED_ATTRIBUTES = {
        "id", "class", "itemprop", "itemtype", "itemscope", 
        "name", "type", "value", "href", "src", "alt", "title", "role",
        "for", "checked", "selected", "disabled", "action"
    }

    @classmethod
    def is_allowed_attribute(cls, attr_name: str, allowed_set: set) -> bool:
        """Проверяет, входит ли атрибут в разрешенный набор или data-*/aria-*."""
        attr_lower = attr_name.lower()
        if attr_lower in allowed_set:
            return True
        if attr_lower.startswith("data-") or attr_lower.startswith("aria-"):
            return True
        return False

    # ИЗМЕНЕНИЕ В core/html_cleaner.py

    @classmethod
    def clean_html_for_selectors(
        cls, 
        raw_html: str, 
        provider: str = getattr(config, "DEFAULT_LLM_PROVIDER", "gemini"),
        max_text_len: Optional[int] = None
    ) -> str:
        """
        Преобразует HTML в скелет с защитой от удаления body и основных контейнеров.
        """
        if not raw_html:
            logger.warning("⚠️ [HTML CLEANER] Передан пустой raw_html!")
            return ""

        prov = provider.lower()
        
        # Выбираем профиль очистки
        if prov == "gemini":
            noise_selectors = cls.GEMINI_NOISE_SELECTORS
            allowed_attrs = cls.GEMINI_ALLOWED_ATTRIBUTES
            text_len_limit = max_text_len if max_text_len is not None else 100
        else: # ollama
            noise_selectors = cls.OLLAMA_NOISE_SELECTORS
            allowed_attrs = cls.OLLAMA_ALLOWED_ATTRIBUTES
            text_len_limit = max_text_len if max_text_len is not None else 40

        initial_len = len(raw_html)
        soup = BeautifulSoup(raw_html, "html.parser")

        # 1. Удаление неиспользуемых служебных тегов (НЕ удаляем header/footer/aside заголовочно, если там h1)
        safe_noise_tags = {"script", "style", "svg", "noscript", "iframe", "head", "meta", "link", "canvas", "audio", "video"}
        for tag in safe_noise_tags:
            for el in soup.find_all(tag):
                el.decompose()

        # 2. Удаление HTML-комментариев
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        # 3. Удаление шума по профилю провайдера с ЗАЩИТОЙ body, html, main и блоков с h1
        protected_tags = {"html", "body", "main", "article"}
        for selector in noise_selectors:
            for el in soup.select(selector):
                # ЗАЩИТА: Никогда не удаляем корневые элементы и блоки, содержащие h1
                if el.name in protected_tags:
                    continue
                if el.find("h1"):
                    continue
                el.decompose()

        # 4. Очистка атрибутов и усечение текста по лимиту профиля
        for el in soup.find_all(True):
            attrs_to_keep = {}
            for attr_k, attr_v in list(el.attrs.items()):
                if cls.is_allowed_attribute(attr_k, allowed_attrs):
                    if isinstance(attr_v, list):
                        attrs_to_keep[attr_k] = " ".join(attr_v)
                    else:
                        attrs_to_keep[attr_k] = str(attr_v)
            
            el.attrs = attrs_to_keep

            if not el.find_all(True):
                txt = el.get_text(strip=True)
                if len(txt) > text_len_limit:
                    el.string = txt[:text_len_limit] + "..."

        # 5. Выделение продуктового контейнера
        target_container = soup.body or soup
        cleaned_html = str(target_container)

        # 6. Очистка от пустых строк
        cleaned_html = re.sub(r'\n\s*\n', '\n', cleaned_html)
        final_len = len(cleaned_html)
        
        reduction = round((1 - (final_len / initial_len)) * 100, 1) if initial_len > 0 else 0
        
        # ДИАГНОСТИЧЕСКИЙ ЛОГ: Предупреждение, если результат слишком мал
        if final_len < 200:
            raw_snippet = raw_html[:400].replace('\n', ' ').strip()
            clean_dump = cleaned_html.replace('\n', ' ').strip()
            logger.warning(
                f"⚠️ [HTML CLEANER DIAGNOSTIC] Скелет HTML аномально мал ({final_len} симв.)! "
                f"Исходный HTML: {initial_len} симв. (Сжатие: -{reduction}%)\n"
                f"   🌐 [RAW HTML SNIPPET]: {raw_snippet}...\n"
                f"   🧹 [CLEANED HTML DUMP]: {clean_dump}"
            )

        return cleaned_html        
