import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright


TARGET_URL = 'https://www.flipkart.com/boat-rockerz-plus-440-dual-driver-80h-battery-beast-mode-enx-bt-v6-0-ipx4-asap-charge-bluetooth/product-reviews/itmc6a6530779dfd?pid=ACCHZEFE7QZXMVFD&lid=LSTACCHZEZXMVFDFBNE5K&marketplace=FLIPKART'

OUTPUT_PATH = (
    Path(__file__).resolve().parent.parent
    / "output"
    / "reviews.json"
)

REVIEW_CONTAINER_SELECTOR = 'div.fWi7J_'

SCROLL_CONTAINER_SELECTOR = 'div.lQLKCP'

REVIEW_TEXT_SELECTOR = 'div > div:nth-of-type(1) > div > div > div > div:nth-of-type(3)'

RATING_SELECTOR = 'div > div:nth-of-type(1) > div > div > div > div:nth-of-type(1) > div:nth-of-type(2) > div:nth-of-type(1)'

REVIEWER_NAME_SELECTOR = 'div > div:nth-of-type(1) > div > div > div > div:nth-of-type(3)'

DATE_SELECTOR = 'div > div:nth-of-type(1) > div > div > div > div:nth-of-type(5) > div:nth-of-type(3) > div > div:nth-of-type(2)'

# Fallbacks stay inside the same review card when inspection cannot provide trusted selectors.
ENABLE_REVIEWER_FALLBACK = True
ENABLE_DATE_FALLBACK = True


async def wait_for_reviews(
    page,
    timeout_seconds=30,
):
    """Wait until observed review containers exist."""

    deadline = (
        asyncio.get_running_loop().time()
        + timeout_seconds
    )

    while (
        asyncio.get_running_loop().time()
        < deadline
    ):

        count = await page.locator(
            REVIEW_CONTAINER_SELECTOR
        ).count()

        if count > 0:
            return count

        await page.wait_for_timeout(1000)

    return 0


async def get_scroll_state(
    scroll_container,
):

    return await scroll_container.evaluate(
        """
        (el) => ({
            top: el.scrollTop,
            height: el.scrollHeight,
            client: el.clientHeight
        })
        """
    )


async def scroll_internal_container(
    page,
    max_rounds=80,
):

    container = page.locator(
        SCROLL_CONTAINER_SELECTOR
    ).first

    if await container.count() == 0:

        print(
            "Observed internal scroll container "
            "not found."
        )

        return

    previous_top = -1
    previous_height = -1
    previous_review_count = -1

    stable_rounds = 0

    print(
        "Scrolling internal review container..."
    )

    for round_number in range(
        1,
        max_rounds + 1,
    ):

        state_before = await get_scroll_state(
            container
        )

        review_count_before = await page.locator(
            REVIEW_CONTAINER_SELECTOR
        ).count()

        await container.evaluate(
            """
            (el) => {
                const amount = Math.max(
                    500,
                    Math.floor(el.clientHeight * 0.85)
                );

                el.scrollTop = Math.min(
                    el.scrollTop + amount,
                    el.scrollHeight
                );
            }
            """
        )

        await page.wait_for_timeout(700)

        state_after = await get_scroll_state(
            container
        )

        review_count_after = await page.locator(
            REVIEW_CONTAINER_SELECTOR
        ).count()

        print(
            f"Scroll {round_number}: "
            f"top {state_before['top']} -> "
            f"{state_after['top']}, "
            f"height {state_before['height']} -> "
            f"{state_after['height']}, "
            f"reviews {review_count_before} -> "
            f"{review_count_after}"
        )

        current_top = state_after["top"]
        current_height = state_after["height"]

        if (
            current_top == previous_top
            and current_height == previous_height
            and review_count_after
            == previous_review_count
        ):

            stable_rounds += 1

        else:

            stable_rounds = 0

        previous_top = current_top
        previous_height = current_height
        previous_review_count = review_count_after

        at_bottom = (
            current_top
            + state_after["client"]
            >= current_height - 5
        )

        if at_bottom and stable_rounds >= 3:
            break

        if stable_rounds >= 6:
            break

    print(
        "Internal scrolling finished."
    )


def clean_text(value):

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    return re.sub(
        r"\s+",
        " ",
        value,
    )


def parse_rating(value):

    value = clean_text(value)

    if not value:
        return None

    match = re.search(
        r"(?<!\d)([0-5](?:\.\d+)?)",
        value,
    )

    if not match:
        return None

    try:
        number = float(
            match.group(1)
        )

    except Exception:
        return None

    if number < 0 or number > 5:
        return None

    return number


async def safe_inner_text(locator):
    try:
        if await locator.count() == 0:
            return None
        return clean_text(await locator.first.inner_text())
    except Exception:
        return None


async def extract_field_text(card, selector, kind=None):
    """Selector-first extraction with semantic per-card fallback."""
    if selector:
        value = await safe_inner_text(card.locator(selector))
        if value:
            return value

    try:
        texts = await card.locator("div,span,p,a").all_inner_texts()
    except Exception:
        return None

    cleaned = []
    seen = set()
    for item in texts:
        item = clean_text(item)
        if not item or item in seen:
            continue
        seen.add(item)
        cleaned.append(item)

    if not cleaned:
        return None

    if kind == "rating":
        for item in cleaned:
            if re.fullmatch(r"[1-5](?:\.0)?", item):
                return item
        return None

    if kind == "date":
        date_re = re.compile(r"(?:today|yesterday|\d+\s+(?:day|days|week|weeks|month|months|year|years)\s+ago)", re.I)
        for item in cleaned:
            match = date_re.search(item)
            if match:
                return match.group(0)
        return None

    if kind == "review_text":
        candidates = []
        date_re = re.compile(r"(?:today|yesterday|\d+\s+(?:day|days|week|weeks|month|months|year|years)\s+ago)", re.I)
        for item in cleaned:
            low = item.lower()
            if len(item) < 12 or len(item) > 1000:
                continue
            if "verified purchase" in low or low.startswith("helpful"):
                continue
            if re.fullmatch(r"[1-5](?:\.0)?", item):
                continue
            if date_re.search(item):
                continue
            candidates.append(item)
        if candidates:
            return max(candidates, key=len)
        return None

    return cleaned[0]


async def extract_reviewer_name_fallback(card, review_text=None, rating=None, date=None):
    """Find a reviewer name only inside the current review card."""
    try:
        texts = await card.locator("div,span,p,a").all_inner_texts()
    except Exception:
        return None

    review_norm = clean_text(review_text) or ""
    date_norm = clean_text(date) or ""
    rating_norm = clean_text(str(rating)) if rating is not None else ""
    date_re = re.compile(r"(?:today|yesterday|\d+\s+(?:day|days|week|weeks|month|months|year|years)\s+ago)", re.I)
    blocked = ('verified purchase', 'helpful', 'report abuse', 'reply', 'read more', 'read less', 'customer review')
    review_words = ('good', 'nice', 'bad', 'best', 'worst', 'product', 'quality', 'excellent', 'awesome', 'great', 'poor', 'comfortable', 'sound', 'battery', 'worth', 'value', 'amazing', 'super', 'love', 'hate', 'delivery', 'purchase', 'works', 'working', 'not')

    scored = []
    seen = set()
    for raw in texts:
        text = clean_text(raw)
        if not text or text in seen:
            continue
        seen.add(text)
        low = text.lower()
        if text == review_norm or text == date_norm or text == rating_norm:
            continue
        if low in blocked or "verified purchase" in low or low.startswith("helpful"):
            continue
        if date_re.search(text) or re.fullmatch(r"[0-5](?:\.0)?", text):
            continue
        if len(text) < 2 or len(text) > 80 or len(text.split()) > 6:
            continue
        if text.endswith(".") or re.search(r"[!?](2,)|https?://", text):
            continue

        words = text.split()
        score = 0
        if 1 <= len(words) <= 5:
            score += 3
        if all(re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", w.strip(",")) for w in words):
            score += 4
        if len(text) <= 40:
            score += 1
        if any(w.lower().strip(".,!") in review_words for w in words):
            score -= 3
        scored.append((score, -len(text), text))

    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][2] if scored[0][0] >= 5 else None


async def extract_date_fallback(card):
    """Extract a relative date from the current review card only."""
    try:
        texts = await card.locator("div,span,p,a").all_inner_texts()
    except Exception:
        return None
    pattern = re.compile(r"(?:today|yesterday|\d+\s+(?:day|days|week|weeks|month|months|year|years)\s+ago)", re.I)
    for raw in texts:
        text = clean_text(raw)
        if not text:
            continue
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


async def extract_reviews(page):

    containers = page.locator(
        REVIEW_CONTAINER_SELECTOR
    )

    count = await containers.count()

    print(
        f"Review container count: {count}"
    )

    reviews = []

    for index in range(count):

        card = containers.nth(index)

        try:

            review_text = await extract_field_text(
                card, REVIEW_TEXT_SELECTOR, "review_text"
            )

            rating_text = await extract_field_text(
                card, RATING_SELECTOR, "rating"
            )

            rating = parse_rating(rating_text)

            reviewer_name = (
                await extract_field_text(
                    card, REVIEWER_NAME_SELECTOR, "reviewer_name"
                )
                if REVIEWER_NAME_SELECTOR else None
            )

            date = (
                await extract_field_text(
                    card, DATE_SELECTOR, "date"
                )
                if DATE_SELECTOR else None
            )

            # IMPORTANT: all fallbacks are scoped to this exact review card.
            if ENABLE_REVIEWER_FALLBACK and not reviewer_name:
                reviewer_name = await extract_reviewer_name_fallback(
                    card, review_text=review_text, rating=rating, date=date
                )

            if ENABLE_DATE_FALLBACK and not date:
                date = await extract_date_fallback(card)

            reviews.append(
                {
                    "review_text": review_text,
                    "rating": rating,
                    "reviewer_name": reviewer_name,
                    "date": date,
                }
            )

        except Exception as exc:

            print(
                f"Extraction error at review "
                f"{index}: {exc}"
            )

    return reviews


def deduplicate_reviews(
    reviews,
):

    unique = []
    seen = set()

    for review in reviews:

        key = (
            clean_text(
                review.get("review_text")
            ),
            review.get("rating"),
            clean_text(
                review.get("reviewer_name")
            ),
            clean_text(
                review.get("date")
            ),
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(review)

    return unique


async def main():

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True
        )

        page = await browser.new_page(
            viewport={
                "width": 1440,
                "height": 900,
            }
        )

        print(
            f"Navigating to {TARGET_URL}"
        )

        await page.goto(
            TARGET_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        await page.wait_for_timeout(3000)

        initial_count = await wait_for_reviews(
            page,
            timeout_seconds=30,
        )

        print(
            f"Initial review containers: "
            f"{initial_count}"
        )

        if initial_count == 0:

            await browser.close()

            raise RuntimeError(
                "No observed review containers "
                "were found."
            )

        await scroll_internal_container(
            page
        )

        await page.wait_for_timeout(
            1500
        )

        reviews = await extract_reviews(
            page
        )

        print(
            f"Extracted {len(reviews)} "
            f"reviews before deduplication."
        )

        reviews = deduplicate_reviews(
            reviews
        )

        print(
            f"{len(reviews)} unique reviews "
            f"after deduplication."
        )

        with open(
            OUTPUT_PATH,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                reviews,
                f,
                ensure_ascii=False,
                indent=2,
            )

        print(
            f"Saved reviews to: {OUTPUT_PATH}"
        )

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
