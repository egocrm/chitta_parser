import asyncio
from camoufox.async_api import AsyncCamoufox

async def main():
    print("⏳ Проверяем Camoufox 0.5.3...")
    async with AsyncCamoufox(headless=True, locale="uk-UA") as browser:
        page = await browser.new_page()
        try:
            r = await page.goto("https://zhuk.ua/category-noutbuky/", timeout=15000)
            print(f"✅ УСПЕХ! Статус: {r.status}")
        except Exception as e:
            print(f"❌ Ошибка: {e}")

if __name__ == "__main__":
    asyncio.run(main())