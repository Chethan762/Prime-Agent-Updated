import asyncio
from playwright.async_api import async_playwright

URL = "https://www.flipkart.com/google-pixel-10a-lavender-256-gb/product-reviews/itmba3073a0f391e?pid=MOBHP6CE4EYBUEW9&lid&marketplace=FLIPKART"

async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()

    try:
        await page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=30000
        )

        await page.wait_for_timeout(3000)

        print("DIVS:", await page.locator("div").count())

        body_text = await page.locator("body").inner_text()

        print("BODY TEXT LENGTH:", len(body_text))
        print("\n--- BODY TEXT ---")
        print(body_text[:5000])

    finally:
        await browser.close()
        await pw.stop()


asyncio.run(main())
