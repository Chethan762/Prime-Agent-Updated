import asyncio
from playwright.async_api import async_playwright

URL = "https://www.flipkart.com/google-pixel-10a-lavender-256-gb/product-reviews/itmba3073a0f391e?pid=MOBHP6CE4EYBUEW9&lid&marketplace=FLIPKART"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        print("TITLE:", await page.title())
        print("URL:", page.url)
        print("BODY_TEXT_START")
        print((await page.locator("body").inner_text())[:5000])
        print("BODY_TEXT_END")
        print("HTML_LENGTH:", len(await page.content()))

        await browser.close()

asyncio.run(main())
