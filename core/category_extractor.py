import hashlib
import json
import logging
import re
import zlib
from typing import List, Tuple, Optional
from urllib.parse import urlparse, urljoin
from playwright.async_api import Page
from core.state_machine import CatalogStateMachine

logger = logging.getLogger("ChittaParser")


class CategoryExtractor:
    """Универсальный 3-уровневый каскад извлечения категорий и построения дерева в StateMachine."""

    SYSTEM_URL_SEGMENTS = {
        # E-commerce & System Technical Routing
        "shop", "catalog", "catalogs", "category", "categories", "product", "products",
        "item", "items", "goods", "p", "g", "c", "tovar", "tovary", "collections",
        "collection", "store", "online-store", "online_store", "index", "php", "html",
        "htm", "aspx", "jsp", "buy", "view", "detail", "details", "dp", "pd", "sp",
        "search", "cart", "checkout", "account", "page", "pages", "blog", "blogs",
        "tag", "tags", "brand", "brands", "manufacturer", "manufacturers", "vendor",
        "vendors", "filter", "filters", "sort", "api", "v1", "v2", "ajax", "main",
        "home", "default", "site", "web", "public", "common", "main_page", "all-products",

        # ISO 639-1 Language Codes (2-letter)
        "en", "uk", "ua", "ru", "de", "fr", "es", "it", "pl", "ro", "cs", "sk", "hu",
        "bg", "el", "nl", "sv", "da", "fi", "no", "pt", "tr", "zh", "ja", "ko", "ar",
        "he", "fa", "th", "vi", "id", "ms", "be", "kk", "az", "hy", "ka", "lt", "lv",
        "et", "hr", "sr", "sl", "sq", "mk", "bs", "is", "ga", "cy", "eu", "ca", "gl",

        # ISO Country & Regional Prefixes
        "us", "gb", "uk", "ca", "au", "nz", "ie", "de", "fr", "es", "it", "nl", "be",
        "ch", "at", "pl", "cz", "sk", "hu", "ro", "bg", "gr", "se", "dk", "fi", "no",
        "ua", "ru", "by", "kz", "ge", "am", "az", "tr", "il", "sa", "ae", "jp", "cn",
        "tw", "hk", "kr", "sg", "in", "br", "mx", "ar", "cl", "co", "za", "vr", "global",

        # Common Locales (Underscore & Hyphen Formats)
        "en_us", "en-us", "en_gb", "en-gb", "en_ca", "en-ca", "en_au", "en-au",
        "en_ie", "en-ie", "en_nz", "en-nz", "en_eu", "en-eu", "en_international",
        "uk_ua", "uk-ua", "ru_ua", "ru-ua", "ru_ru", "ru-ru", "ru_kz", "ru-kz",
        "de_de", "de-de", "de_at", "de-at", "de_ch", "de-ch", "fr_fr", "fr-fr",
        "fr_ca", "fr-ca", "fr_be", "fr-be", "fr_ch", "fr-ch", "es_es", "es-es",
        "es_mx", "es-mx", "es_ar", "es-ar", "es_cl", "es-cl", "es_co", "es-co",
        "it_it", "it-it", "it_ch", "it-ch", "nl_nl", "nl-nl", "nl_be", "nl-be",
        "pl_pl", "pl-pl", "pt_pt", "pt-pt", "pt_br", "pt-br", "zh_cn", "zh-cn",
        "zh_tw", "zh-tw", "zh_hk", "zh-hk", "ja_jp", "ja-jp", "ko_kr", "ko-kr",
        "tr_tr", "tr-tr", "ro_ro", "ro-ro", "cs_cz", "cs-cz", "sk_sk", "sk-sk",
        "hu_hu", "hu-hu", "bg_bg", "bg-bg", "el_gr", "el-gr", "sv_se", "sv-se",
        "da_dk", "da-dk", "fi_fi", "fi-fi", "no_no", "no-no", "ar_sa", "ar-sa",
        "ar_ae", "ar-ae", "he_il", "he-il", "vi_vn", "vi-vn", "th_th", "th-th",
        "id_id", "id-id", "ms_my", "ms-my"
    }

    IGNORE_CRUMB_NAMES = {
        # Ukrainian / Russian
        "головна", "головна сторінка", "главная", "главная страница", 
        "каталог", "каталог товарів", "каталог товаров", "усі товари", "все товары",
        "магазин", "товар", "товари", "товары",

        # English
        "home", "homepage", "main", "main page", "start", "index", 
        "catalog", "catalogs", "catalogue", "shop", "store", "all products", "products",

        # German (DE)
        "startseite", "start", "hauptseite", "home", "katalog", "shop", "produkte", "alle produkte",

        # French (FR)
        "accueil", "page d'accueil", "catalogue", "boutique", "produits", "tous les produits",

        # Spanish (ES)
        "inicio", "portada", "página principal", "catálogo", "tienda", "productos", "todos los productos",

        # Italian (IT)
        "home", "inizio", "pagina iniziale", "catalogo", "negozio", "prodotti", "tutti i prodotti",

        # Polish (PL)
        "strona główna", "glowna", "strona glowna", "katalog", "sklep", "produkty", "wszystkie produkty",

        # Romanian (RO) / Moldavian
        "acasă", "acasa", "pagina principală", "catalog", "magazin", "produse", "toate produsele",

        # Czech (CS) & Slovak (SK)
        "domů", "domu", "hlavní strana", "domov", "hlavná stránka", "katalog", "katalóg", "obchod", "produkty",

        # Portuguese (PT)
        "início", "inicio", "página inicial", "catálogo", "loja", "produtos", "todos os produtos",

        # Dutch (NL)
        "home", "startpagina", "catalogus", "winkel", "producten", "alle producten",

        # Turkish (TR)
        "anasayfa", "ana sayfa", "katalog", "mağaza", "ürünler", "tüm ürünler",

        # Hungarian (HU)
        "főoldal", "fooldal", "katalógus", "bolt", "termékek",

        # Bulgarian (BG)
        "начало", "главна", "каталог", "магазин", "продукти",

        # Greek (EL)
        "αρχική", "αρχικη", "κατάλογος", "καταλογος", "προϊόντα",

        # Swedish (SV) / Danish (DA) / Norwegian (NO)
        "hem", "hjem", "startsida", "katalog", "butik", "produkter",

        # Finnish (FI)
        "etusivu", "katalogi", "kauppa", "tuotteet"
    }

    @classmethod
    def _generate_cat_id(cls, name: str, link: str = "") -> str:
        """Генерирует детерминированный хэш категории без риска коллизий."""
        if link:
            match = re.search(r'[/-](?:g|c|cat|category)(\d{3,})(?:[/-]|\.html|$)', link, re.IGNORECASE)
            if match:
                return match.group(1)
        clean_name = re.sub(r'\s+', ' ', name.strip().lower())
        return hashlib.sha256(clean_name.encode('utf-8')).hexdigest()[:10]

    @classmethod
    def _clean_text(cls, text: str) -> str:
        if not text:
            return ""
        clean = re.sub(r'<[^>]+>', ' ', str(text))
        return re.sub(r'\s+', ' ', clean).strip()

    @classmethod
    def _slug_to_title(cls, slug: str) -> str:
        """Преобразует URL-слаг (tovari-dlya-kotov) в читаемый заголовок (Tovari Dlya Kotov)."""
        clean_slug = re.sub(r'[^\w\s-]', '', slug)
        words = clean_slug.replace('-', ' ').replace('_', ' ').split()
        return ' '.join(word.capitalize() for word in words)

    # =========================================================================
    # УРОВЕНЬ 1: Schema.org JSON-LD
    # =========================================================================
    async def _extract_json_ld(self, page: Page, page_url: str) -> List[dict]:
        crumbs = []
        try:
            scripts = await page.locator('script[type="application/ld+json"]').all_inner_texts()
            for script_text in scripts:
                try:
                    data = json.loads(script_text)
                    items = data if isinstance(data, list) else [data]
                    if isinstance(data, dict) and "@graph" in data:
                        items = data["@graph"]

                    for item in items:
                        if isinstance(item, dict) and item.get("@type") == "BreadcrumbList":
                            elements = item.get("itemListElement", [])
                            if not elements:
                                continue

                            sorted_els = sorted(
                                elements, 
                                key=lambda x: int(x.get("position", 0)) if str(x.get("position", "0")).isdigit() else 0
                            )                            
                            for el in sorted_els:
                                c_name = self._clean_text(el.get("name") or el.get("item", {}).get("name", ""))
                                raw_link = el.get("item") if isinstance(el.get("item"), str) else el.get("item", {}).get("@id", "") or el.get("url", "")
                                c_link = urljoin(page_url, raw_link) if raw_link else ""
                                
                                if c_name and c_name.lower() not in self.IGNORE_CRUMB_NAMES:
                                    crumbs.append({"name": c_name, "link": c_link})

                            if crumbs:
                                # logger.info(f"📂 [CAT LEVEL 1: JSON-LD] Найдено {len(crumbs)} крошек.")
                                return crumbs
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"⚠️ Ошибка вызова Level 1 JSON-LD: {e}")
        return crumbs

    # =========================================================================
    # УРОВЕНЬ 2: Универсальные DOM-селекторы
    # =========================================================================
    async def _extract_dom(self, page: Page, page_url: str) -> List[dict]:
        try:
            raw_crumbs = await page.evaluate("""
                () => {
                    const selectors = [
                        '[itemtype*="BreadcrumbList"]',
                        'nav[class*="breadcrumb" i]', 'div[class*="breadcrumb" i]',
                        'ul[class*="breadcrumb" i]', 'ol[class*="breadcrumb" i]',
                        '[aria-label*="breadcrumb" i]', '[class*="breadcrumbs" i]',
                        '[id*="breadcrumb" i]', '.page-path', '.b-breadcrumb'
                    ];

                    let container = null;
                    for (let sel of selectors) {
                        const found = document.querySelector(sel);
                        if (found && found.textContent.trim().length > 0) {
                            container = found;
                            break;
                        }
                    }

                    if (!container) return [];

                    const links = Array.from(container.querySelectorAll('a, [itemprop="name"], span'));
                    const result = [];
                    const seenNames = new Set();

                    for (let el of links) {
                        const txt = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                        let href = el.getAttribute('href') || el.closest('a')?.getAttribute('href') || '';
                        
                        if (txt && txt.length < 60 && !seenNames.has(txt.toLowerCase())) {
                            seenNames.add(txt.toLowerCase());
                            result.push({ name: txt, link: href });
                        }
                    }
                    return result;
                }
            """)

            crumbs = []
            for c in raw_crumbs:
                c_name = self._clean_text(c.get("name", ""))
                raw_link = c.get("link", "")
                c_link = urljoin(page_url, raw_link) if raw_link else ""

                if c_name and c_name.lower() not in self.IGNORE_CRUMB_NAMES:
                    crumbs.append({"name": c_name, "link": c_link})

            if crumbs:
                # logger.info(f"📂 [CAT LEVEL 2: DOM] Найдено {len(crumbs)} крошек.")
                return crumbs

        except Exception as e:
            logger.debug(f"⚠️ Ошибка вызова Level 2 DOM: {e}")
        return []

    # =========================================================================
    # УРОВЕНЬ 3: Фолбэк по URL товара
    # =========================================================================
    def _extract_url_fallback(self, page_url: str) -> List[dict]:
        crumbs = []
        try:
            parsed = urlparse(page_url)
            path_segments = [s.strip() for s in parsed.path.split('/') if s.strip()]

            # Отфильтровываем технические сегменты
            cat_segments = [
                s for s in path_segments 
                if s.lower() not in self.SYSTEM_URL_SEGMENTS and not re.match(r'^\d+$', s)
            ]

            # Если сегментов больше 1, последний — это продукт
            if len(cat_segments) > 1:
                cat_segments = cat_segments[:-1]
            elif len(cat_segments) == 1 and len(path_segments) == 1:
                # Если всего один сегмент на весь путь, возможно это категория или товар без вложенности
                pass

            base_netloc = f"{parsed.scheme}://{parsed.netloc}"
            running_path = ""
            for seg in path_segments:
                running_path += f"/{seg}"
                if seg in cat_segments:
                    c_name = self._slug_to_title(seg)
                    if c_name and c_name.lower() not in self.IGNORE_CRUMB_NAMES:
                        crumbs.append({"name": c_name, "link": f"{base_netloc}{running_path}"})

            # if crumbs:
            #     logger.info(f"📂 [CAT LEVEL 3: URL FALLBACK] Сгенерировано {len(crumbs)} категорий из URL: {page_url}")

        except Exception as e:
            logger.debug(f"⚠️ Ошибка вызова Level 3 URL Fallback: {e}")

        return crumbs

    # =========================================================================
    # ГЛАВНЫЙ ОРКЕСТРАТОР КАСКАДА
    # =========================================================================
    async def extract_and_register_categories(
        self, 
        page: Page, 
        page_url: str, 
        state_machine: CatalogStateMachine
    ) -> Tuple[str, str, str]:
        """
        Запускает 3-уровневый каскад и регистрирует связи категорий в StateMachine.
        Возвращает: (cat_id, cat_name, cat_link) для конечной подкатегории.
        """
        # 1. Запуск Level 1 (JSON-LD)
        crumbs = await self._extract_json_ld(page, page_url)

        # 2. Запуск Level 2 (DOM)
        if not crumbs:
            crumbs = await self._extract_dom(page, page_url)

        # 3. Запуск Level 3 (URL Fallback)
        if not crumbs:
            crumbs = self._extract_url_fallback(page_url)

        if not crumbs:
            # Дефолтный фолбэк, если URL пуст
            default_cat = state_machine.add_category(cat_id="100", name="Каталог", parent_id="0")
            return default_cat.id, default_cat.name, page_url

        # Отсекаем последний элемент, если это название самого товара в крошках
        if len(crumbs) > 1 and crumbs[-1]["link"].rstrip('/') == page_url.rstrip('/'):
            crumbs = crumbs[:-1]

        # Регистрация цепочки категорий в StateMachine
        current_parent_id = "0"
        final_cat_id, final_cat_name, final_cat_link = "", "", ""

        for c in crumbs:
            c_name = c["name"]
            c_link = c["link"]
            c_id = self._generate_cat_id(c_name, c_link)

            state_machine.add_category(cat_id=c_id, name=c_name, parent_id=current_parent_id)
            current_parent_id = c_id
            final_cat_id, final_cat_name, final_cat_link = c_id, c_name, c_link

        logger.info(f"✅ [CATEGORY BOUND] Привязана конечная категория: '{final_cat_name}' (ID: {final_cat_id})")
        return final_cat_id, final_cat_name, final_cat_link