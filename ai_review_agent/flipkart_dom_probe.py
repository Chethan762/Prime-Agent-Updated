import asyncio
from playwright.async_api import async_playwright

URL = "https://www.flipkart.com/google-pixel-10a-lavender-256-gb/product-reviews/itmba3073a0f391e?pid=MOBHP6CE4EYBUEW9&lid&marketplace=FLIPKART"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        target = page.get_by_text("Terrific purchase", exact=True).first

        print("TARGET_COUNT:", await page.get_by_text("Terrific purchase", exact=True).count())

        if await target.count():
            info = await target.evaluate("""
                el => {
                    const result = [];
                    let node = el;
                    for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
                        result.push({
                            level: i,
                            tag: node.tagName,
                            class: node.className,
                            id: node.id,
                            text: (node.innerText || '').slice(0, 1000)
                        });
                    }
                    return result;
                }
            """)

            for item in info:
                print("\\nLEVEL:", item["level"])
                print("TAG:", item["tag"])
                print("CLASS:", item["class"])
                print("ID:", item["id"])
                print("TEXT:", item["text"])

        await browser.close()

asyncio.run(main())
