import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


TARGET_URL = "https://web-scraping.dev/reviews"
GRAPHQL_ENDPOINT = "https://web-scraping.dev/api/graphql"
PAGE_SIZE = 20  # as observed in the captured request


GRAPHQL_QUERY = """
query GetReviews($first: Int, $after: String) {
  reviews(first: $first, after: $after) {
    edges {
      node {
        rid
        text
        rating
        date
      }
      cursor
    }
    pageInfo {
      startCursor
      endCursor
      hasPreviousPage
      hasNextPage
    }
  }
}
"""


async def fetch_all_reviews(page):
    """Iterate through the GraphQL cursor pagination and collect reviews."""
    cursor = ""
    has_next = True
    raw_reviews = []

    while has_next:
        payload = {
            "query": GRAPHQL_QUERY,
            "variables": {"first": PAGE_SIZE, "after": cursor},
        }

        try:
            response = await page.request.post(
                GRAPHQL_ENDPOINT,
                data=json.dumps(payload),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        except PlaywrightTimeoutError:
            # If the request times out, break the loop – we cannot continue safely.
            break

        if not response.ok:
            # Unexpected status, stop pagination.
            break

        try:
            data = await response.json()
        except Exception:
            break

        reviews_section = data.get("data", {}).get("reviews", {})
        edges = reviews_section.get("edges", [])
        for edge in edges:
            node = edge.get("node", {})
            raw_reviews.append(
                {
                    "review_text": node.get("text"),
                    "rating": node.get("rating"),
                    "date": node.get("date"),
                }
            )

        page_info = reviews_section.get("pageInfo", {})
        has_next = page_info.get("hasNextPage", False)
        cursor = page_info.get("endCursor", "")

        # Defensive stop: if no new edges were returned, avoid infinite loop.
        if not edges:
            break

    # Deduplicate reviews (using text+date+rating as a simple key)
    seen = set()
    deduped = []
    for rev in raw_reviews:
        key = (rev.get("review_text"), rev.get("rating"), rev.get("date"))
        if key not in seen:
            seen.add(key)
            # Ensure missing fields are explicit nulls
            deduped.append(
                {
                    "review_text": rev.get("review_text"),
                    "rating": rev.get("rating"),
                    "date": rev.get("date"),
                }
            )
    return deduped


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(TARGET_URL, timeout=30000)
        except PlaywrightTimeoutError:
            # Navigation timeout – continue anyway; the GraphQL endpoint may still work.
            pass

        reviews = await fetch_all_reviews(page)

        await browser.close()

    # Prepare output path
    output_path = (
        Path(__file__).resolve().parent.parent / "output" / "reviews.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write JSON
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
