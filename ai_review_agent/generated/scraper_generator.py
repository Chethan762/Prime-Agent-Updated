# generated/scraper_generator.py

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from xmlrpc import client

from groq import Groq


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

EVIDENCE_PATH = BASE_DIR / "inspection_evidence.json"
GENERATED_SCRAPER = BASE_DIR / "review_scraper.py"
INSPECTOR_PATH = BASE_DIR.parent / "ipython_inspector.py"

PROJECT_DIR = BASE_DIR.parent
OUTPUT_DIR = PROJECT_DIR / "output"
OUTPUT_PATH = OUTPUT_DIR / "reviews.json"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# CONFIG
# ============================================================

MODEL = "openai/gpt-oss-120b"

MAX_ATTEMPTS = 2
REGENERATION_WAIT_SECONDS = 90
EXECUTION_TIMEOUT_SECONDS = 180


# ============================================================
# GROQ
# ============================================================

def get_groq_client():
    api_key = os.getenv("GROQ_API_KEY")

    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set."
        )

    return Groq(api_key=api_key)


# ============================================================
# LOAD EVIDENCE
# ============================================================

def run_inspection(target_url):
    """Run deterministic browser inspection before any LLM strategy generation."""
    print()
    print("=" * 70)
    print("STEP 1/3 - BROWSER INSPECTION")
    print("=" * 70)
    print(f"[INSPECTOR] Script: {INSPECTOR_PATH}")
    print(f"[INSPECTOR] Target: {target_url}")

    if not INSPECTOR_PATH.exists():
        raise FileNotFoundError(
            f"Inspector not found: {INSPECTOR_PATH}"
        )

    # Never reuse stale evidence. The inspector must create a fresh file.
    if EVIDENCE_PATH.exists():
        try:
            EVIDENCE_PATH.unlink()
            print("[INSPECTOR] Removed stale inspection_evidence.json")
        except Exception as exc:
            raise RuntimeError(
                f"Could not remove stale evidence: {exc}"
            )

    result = subprocess.run(
        [
            sys.executable,
            str(INSPECTOR_PATH),
            target_url,
        ],
        cwd=str(PROJECT_DIR),
        capture_output=True,
        text=True,
        timeout=180,
    )

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)

    if result.returncode != 0:
        raise RuntimeError(
            f"Inspection failed with exit code {result.returncode}."
        )

    if not EVIDENCE_PATH.exists():
        raise RuntimeError(
            f"Inspection finished but did not create: {EVIDENCE_PATH}"
        )

    print(f"[INSPECTOR] Fresh evidence created: {EVIDENCE_PATH}")


def load_evidence():
    if not EVIDENCE_PATH.exists():
        raise FileNotFoundError(
            f"Evidence file not found:\n{EVIDENCE_PATH}"
        )

    with open(
        EVIDENCE_PATH,
        "r",
        encoding="utf-8",
    ) as f:
        return json.load(f)

def normalize_strategy(strategy: dict, evidence: dict) -> dict:
    """
    Normalize and validate the LLM-selected scraping strategy.

    Important:
    - Never invent selectors for fields that the inspector did not reliably find.
    - Missing reviewer/date fields are handled by per-review DOM fallback.
    """

    best_fields = evidence.get("best_fields", {}) or {}
    strategy = dict(strategy or {})

    # Normalize strategy name
    strategy_name = strategy.get("strategy") or strategy.get("type")

    if strategy_name in {
        "infinite_scroll_dom",
        "internal_scroll_dom",
        "dynamic_scroll_or_load_more",
    }:
        strategy["strategy"] = "internal_scroll_dom"
    else:
        strategy["strategy"] = strategy_name or "internal_scroll_dom"

    # Evidence-backed selectors only
    field_mapping = {
        "review_text_selector": "review_text",
        "rating_selector": "rating",
        "reviewer_name_selector": "reviewer_name",
        "date_selector": "date",
        "verified_purchase_selector": "verified_purchase",
        "helpful_selector": "helpful",
    }

    for strategy_key, evidence_key in field_mapping.items():
        evidence_field = best_fields.get(evidence_key)

        # Only accept selectors actually present in evidence.
        if isinstance(evidence_field, dict):
            selector = evidence_field.get("selector")
            if selector:
                strategy[strategy_key] = selector
            elif strategy_key not in {
                "review_text_selector",
                "rating_selector",
            }:
                strategy[strategy_key] = None
        else:
            strategy[strategy_key] = None

    # Review container and scroll container can come from strategy,
    # but must not be empty.
    if not strategy.get("review_container_selector"):
        strategy["review_container_selector"] = (
            evidence.get("review_container_selector")
            or "div.fWi7J_"
        )

    if not strategy.get("scroll_container_selector"):
        strategy["scroll_container_selector"] = (
            evidence.get("scroll_container_selector")
            or "div.lQLKCP"
        )

    required_fields = ["review_text", "rating"]

    missing_required = []
    missing_optional = []

    for field in required_fields:
        key = f"{field}_selector"
        if not strategy.get(key):
            missing_required.append(field)

    for field in ["reviewer_name", "date", "verified_purchase", "helpful"]:
        key = f"{field}_selector"
        if not strategy.get(key):
            missing_optional.append(field)

    strategy["missing_required_fields"] = missing_required
    strategy["missing_fields"] = missing_optional
    strategy["fallback_required"] = bool(missing_optional)

    return strategy
# ============================================================
# SELECTOR HELPERS
# ============================================================

def make_scroll_selector(container):
    """
    Build a selector only from observed inspector evidence.
    """

    if not isinstance(container, dict):
        return None

    if container.get("selector"):
        return container["selector"]

    tag = container.get("tag")
    class_name = container.get("class_name")

    if not tag or not class_name:
        return None

    classes = str(class_name).split()

    valid_classes = []

    for cls in classes:

        if re.match(
            r"^[A-Za-z_][A-Za-z0-9_-]*$",
            cls,
        ):
            valid_classes.append(cls)

    if not valid_classes:
        return None

    return tag + "".join(
        f".{cls}"
        for cls in valid_classes
    )


def get_review_candidates(evidence):
    candidates = []

    for item in evidence.get(
        "review_candidates",
        [],
    ):

        if not isinstance(item, dict):
            continue

        selector = item.get("selector")

        if not selector:
            continue

        score = item.get("score", 0)

        try:
            score = float(score)
        except Exception:
            score = 0

        candidates.append(
            {
                "selector": selector,
                "score": score,
                "tag": item.get("tag"),
                "text": item.get(
                    "text",
                    "",
                )[:500],
                "field_hints": item.get(
                    "field_hints",
                    {},
                ),
            }
        )

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    unique = []
    seen = set()

    for candidate in candidates:

        selector = candidate["selector"]

        if selector in seen:
            continue

        seen.add(selector)
        unique.append(candidate)

    return unique[:10]


# ============================================================
# COMPACT EVIDENCE FOR GROQ
# ============================================================

def build_compact_evidence(evidence):

    best_fields = evidence.get(
        "best_fields",
        {},
    )

    fields = {}

    if isinstance(best_fields, dict):

        for field_name in (
            "review_text",
            "rating",
            "reviewer_name",
            "date",
        ):

            field = best_fields.get(
                field_name
            )

            if not isinstance(field, dict):
                continue

            selector = field.get(
                "selector"
            )

            if not selector:
                continue

            fields[field_name] = {
                "selector": selector,
                "example": field.get(
                    "example"
                ),
                "score": field.get(
                    "score"
                ),
                "tag": field.get(
                    "tag"
                ),
            }

    scroll_containers = []

    for item in evidence.get(
        "scroll_containers",
        [],
    ):

        selector = make_scroll_selector(
            item
        )

        if not selector:
            continue

        scroll_containers.append(
            {
                "selector": selector,
                "tag": item.get("tag"),
                "class_name": item.get(
                    "class_name"
                ),
                "client_height": item.get(
                    "client_height"
                ),
                "scroll_height": item.get(
                    "scroll_height"
                ),
            }
        )

    return {
        "url": evidence.get("url"),
        "review_strategy": evidence.get(
            "review_strategy"
        ),
        "api_pagination": evidence.get(
            "api_pagination",
            False,
        ),
        "has_more_pages": evidence.get(
            "has_more_pages"
        ),
        "review_candidates":
            get_review_candidates(
                evidence
            ),
        "best_fields": fields,
        "scroll_containers":
            scroll_containers,
    }


# ============================================================
# GROQ STRATEGY PROMPT
# ============================================================

def build_strategy_prompt(
    evidence,
    previous_error=None,
):

    compact = build_compact_evidence(
        evidence
    )

    evidence_json = json.dumps(
        compact,
        indent=2,
        ensure_ascii=False,
    )

    error_text = ""

    if previous_error:

        error_text = f"""
Previous execution failed.

Previous failure:

{previous_error}

You must choose a different configuration if
the previous configuration caused the failure.
"""

    prompt = f"""
You are a browser scraping architecture expert.

Your task is NOT to write Python code.

Your task is to select the correct scraping configuration
from deterministic browser inspection evidence.

Return ONLY valid JSON.

Do not return Markdown.
Do not explain your answer.
Do not invent selectors.

============================================================
INSPECTION EVIDENCE
============================================================

{evidence_json}

============================================================
REQUIRED JSON FORMAT
============================================================

{{
  "review_container_selector": "...",
  "scroll_container_selector": "...",
  "review_text_selector": "...",
  "rating_selector": "...",
  "reviewer_name_selector": null,
  "date_selector": null,
  "strategy": "internal_scroll_dom"
}}

============================================================
RULES
============================================================

1. review_container_selector MUST come from
   review_candidates.selector.

2. scroll_container_selector MUST come from
   scroll_containers.selector.

3. review_text_selector MUST come from
   best_fields.review_text.selector.

4. rating_selector MUST come from
   best_fields.rating.selector.

5. reviewer_name_selector:

   Use the exact observed selector only if
   best_fields contains reviewer_name.

   Otherwise return null.

6. date_selector:

   Use the exact observed selector only if
   best_fields contains date.

   Otherwise return null.

7. Never invent selectors.

8. Never modify selectors.

9. Do not choose a parent page container merely because
   it contains review text.

10. Prefer the review candidate whose text most clearly
    represents ONE complete review.

11. If review_strategy indicates internal scrolling,
    use internal_scroll_dom.

13. Do not generate API endpoints.

14. Do not generate scraping code.

15. Return JSON only.

{error_text}
"""

    return prompt


# ============================================================
# PARSE STRATEGY
# ============================================================

def clean_json_response(text):

    text = text.strip()

    if text.startswith("```"):

        lines = text.splitlines()

        if lines:
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        text = "\n".join(lines)

    return text.strip()


def generate_strategy(
    client,
    evidence,
    previous_error=None,
):

    prompt = build_strategy_prompt(
        evidence,
        previous_error,
    )

    print(
        f"[STRATEGY] Calling Groq: {MODEL}"
    )

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Return only valid JSON. "
                    "Never invent selectors."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        temperature=0,
        max_tokens=3000,
    )

    raw = response.choices[0].message.content

    if not raw:
        raise RuntimeError(
            "Groq returned an empty strategy."
        )

    raw = clean_json_response(raw)

    try:
        strategy = json.loads(raw)
    except Exception as exc:

        raise RuntimeError(
            "Groq returned invalid JSON:\n"
            + raw
        ) from exc

    print(
        "[STRATEGY] Selected configuration:"
    )

    print(
        json.dumps(
            strategy,
            indent=2,
            ensure_ascii=False,
        )
    )

    return strategy


# ============================================================
# VALIDATE STRATEGY AGAINST EVIDENCE
# ============================================================

def validate_strategy(
    strategy,
    evidence,
):

    if not isinstance(strategy, dict):

        return False, "Strategy is not an object."

    observed_reviews = {
        item.get("selector")
        for item in evidence.get(
            "review_candidates",
            [],
        )
        if isinstance(item, dict)
        and item.get("selector")
    }

    observed_scrolls = set()

    for item in evidence.get(
        "scroll_containers",
        [],
    ):

        selector = make_scroll_selector(
            item
        )

        if selector:
            observed_scrolls.add(
                selector
            )

    best_fields = evidence.get(
        "best_fields",
        {},
    )

    observed_field_selectors = {}

    for field_name, field in best_fields.items():

        if isinstance(field, dict):

            selector = field.get(
                "selector"
            )

            if selector:
                observed_field_selectors[
                    field_name
                ] = selector

    review_selector = strategy.get(
        "review_container_selector"
    )

    scroll_selector = strategy.get(
        "scroll_container_selector"
    )

    review_text_selector = strategy.get(
        "review_text_selector"
    )

    rating_selector = strategy.get(
        "rating_selector"
    )

    if review_selector not in observed_reviews:

        return False, (
            "Generated review container selector "
            "was not present in inspection evidence."
        )

    if scroll_selector not in observed_scrolls:

        return False, (
            "Generated scroll container selector "
            "was not present in inspection evidence."
        )

    if (
        review_text_selector
        != observed_field_selectors.get(
            "review_text"
        )
    ):

        return False, (
            "Generated review_text selector "
            "does not exactly match inspection evidence."
        )

    if (
        rating_selector
        != observed_field_selectors.get(
            "rating"
        )
    ):

        return False, (
            "Generated rating selector "
            "does not exactly match inspection evidence."
        )

    # Reviewer name is optional and evidence-driven.
    expected_reviewer = (
        observed_field_selectors.get(
            "reviewer_name"
        )
    )

    actual_reviewer = strategy.get(
        "reviewer_name_selector"
    )

    if expected_reviewer:

        if actual_reviewer != expected_reviewer:

            return False, (
                "reviewer_name selector does not "
                "match inspection evidence."
            )

    else:

        if actual_reviewer is not None:

            return False, (
                "reviewer_name was invented even though "
                "inspection evidence did not provide "
                "a trusted reviewer_name field."
            )

    # Date is optional and evidence-driven.
    expected_date = (
        observed_field_selectors.get(
            "date"
        )
    )

    actual_date = strategy.get(
        "date_selector"
    )

    if expected_date:

        if actual_date != expected_date:

            return False, (
                "date selector does not match "
                "inspection evidence."
            )

    else:

        if actual_date is not None:

            return False, (
                "date selector was invented even though "
                "inspection evidence did not provide it."
            )

    return True, None


# ============================================================
# GENERATE DETERMINISTIC SCRAPER
# ============================================================

def python_string(value):

    return repr(value)


def build_scraper_code(
    target_url,
    strategy,
):

    review_selector = strategy[
        "review_container_selector"
    ]

    scroll_selector = strategy[
        "scroll_container_selector"
    ]

    review_text_selector = strategy[
        "review_text_selector"
    ]

    rating_selector = strategy[
        "rating_selector"
    ]

    reviewer_selector = strategy.get(
        "reviewer_name_selector"
    )

    date_selector = strategy.get(
        "date_selector"
    )

    return f'''import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright


TARGET_URL = {python_string(target_url)}

OUTPUT_PATH = (
    Path(__file__).resolve().parent.parent
    / "output"
    / "reviews.json"
)

REVIEW_CONTAINER_SELECTOR = {python_string(review_selector)}

SCROLL_CONTAINER_SELECTOR = {python_string(scroll_selector)}

REVIEW_TEXT_SELECTOR = {python_string(review_text_selector)}

RATING_SELECTOR = {python_string(rating_selector)}

REVIEWER_NAME_SELECTOR = {python_string(reviewer_selector)}

DATE_SELECTOR = {python_string(date_selector)}

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
        (el) => ({{
            top: el.scrollTop,
            height: el.scrollHeight,
            client: el.clientHeight
        }})
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
            (el) => {{
                const amount = Math.max(
                    500,
                    Math.floor(el.clientHeight * 0.85)
                );

                el.scrollTop = Math.min(
                    el.scrollTop + amount,
                    el.scrollHeight
                );
            }}
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
            f"Scroll {{round_number}}: "
            f"top {{state_before['top']}} -> "
            f"{{state_after['top']}}, "
            f"height {{state_before['height']}} -> "
            f"{{state_after['height']}}, "
            f"reviews {{review_count_before}} -> "
            f"{{review_count_after}}"
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
        r"\\s+",
        " ",
        value,
    )


def parse_rating(value):

    value = clean_text(value)

    if not value:
        return None

    match = re.search(
        r"(?<!\\d)([0-5](?:\\.\\d+)?)",
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
    blocked = {
        "verified purchase", "helpful", "report abuse", "reply",
        "read more", "read less", "customer review",
    }
    review_words = {
        "good", "nice", "bad", "best", "worst", "product", "quality",
        "excellent", "awesome", "great", "poor", "comfortable", "sound",
        "battery", "worth", "value", "amazing", "super", "love", "hate",
        "delivery", "purchase", "works", "working", "not",
    }

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
        if text.endswith(".") or re.search(r"[!?]{2,}|https?://", text):
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
        f"Review container count: {{count}}"
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
                {{
                    "review_text": review_text,
                    "rating": rating,
                    "reviewer_name": reviewer_name,
                    "date": date,
                }}
            )

        except Exception as exc:

            print(
                f"Extraction error at review "
                f"{{index}}: {{exc}}"
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
            viewport={{
                "width": 1440,
                "height": 900,
            }}
        )

        print(
            f"Navigating to {{TARGET_URL}}"
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
            f"{{initial_count}}"
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
            f"Extracted {{len(reviews)}} "
            f"reviews before deduplication."
        )

        reviews = deduplicate_reviews(
            reviews
        )

        print(
            f"{{len(reviews)}} unique reviews "
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
            f"Saved reviews to: {{OUTPUT_PATH}}"
        )

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
'''

# ============================================================
# SAVE GENERATED DETERMINISTIC SCRAPER
# ============================================================

def save_scraper(
    target_url,
    strategy,
):

    code = build_scraper_code(
        target_url,
        strategy,
    )

    GENERATED_SCRAPER.write_text(
        code,
        encoding="utf-8",
    )

    print(
        f"[SCRAPER] Deterministic scraper saved:\n"
        f"{GENERATED_SCRAPER}"
    )


# ============================================================
# COMPILE
# ============================================================

def compile_scraper():

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "py_compile",
            str(GENERATED_SCRAPER),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:

        error = (
            result.stderr.strip()
            or result.stdout.strip()
            or "Unknown compile error."
        )

        return False, error

    return True, None


# ============================================================
# EXECUTE
# ============================================================

def execute_scraper():

    print(
        "[EXECUTOR] Running deterministic scraper..."
    )

    try:

        result = subprocess.run(
            [
                sys.executable,
                str(GENERATED_SCRAPER),
            ],
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=EXECUTION_TIMEOUT_SECONDS,
        )

    except subprocess.TimeoutExpired:

        return False, (
            f"Scraper exceeded "
            f"{EXECUTION_TIMEOUT_SECONDS} seconds."
        )

    if result.stdout:
        print(result.stdout)

    if result.stderr:
        print(result.stderr)

    if result.returncode != 0:

        return False, (
            f"Scraper exited with code "
            f"{result.returncode}"
        )

    return True, None


# ============================================================
# VALIDATION
# ============================================================

def validate_output(
    evidence,
):

    if not OUTPUT_PATH.exists():

        return False, [
            "reviews.json was not created."
        ]

    try:

        with open(
            OUTPUT_PATH,
            "r",
            encoding="utf-8",
        ) as f:

            reviews = json.load(f)

    except Exception as exc:

        return False, [
            f"Invalid JSON: {exc}"
        ]

    errors = []

    if not isinstance(reviews, list):

        return False, [
            "reviews.json is not a JSON array."
        ]

    if not reviews:

        errors.append(
            "No reviews extracted."
        )

    best_fields = evidence.get(
        "best_fields",
        {},
    )

    # This scraper must return the complete review record.
    # Reviewer/date are extracted with same-card fallbacks when inspection
    # does not expose stable selectors.
    reviewer_expected = True
    date_expected = True

    seen = set()

    for index, review in enumerate(
        reviews
    ):

        if not isinstance(review, dict):

            errors.append(
                f"Review {index} is not an object."
            )

            continue

        text = clean_value(
            review.get("review_text")
        )

        rating = review.get(
            "rating"
        )

        reviewer = clean_value(
            review.get(
                "reviewer_name"
            )
        )

        date = clean_value(
            review.get("date")
        )

        if not text:

            errors.append(
                f"Review {index} has empty "
                f"review_text."
            )

        if rating is None:

            errors.append(
                f"Review {index} is missing "
                f"rating."
            )

        if reviewer_expected and not reviewer:

            errors.append(
                f"Review {index} is missing "
                f"reviewer_name."
            )

        if date_expected and not date:

            errors.append(
                f"Review {index} is missing "
                f"date."
            )

        key = (
            text,
            rating,
            reviewer,
            date,
        )

        if key in seen:

            errors.append(
                f"Duplicate review at index "
                f"{index}."
            )

        seen.add(key)

    if errors:

        return False, errors

    return True, []


def clean_value(value):

    if value is None:
        return ""

    return re.sub(
        r"\\s+",
        " ",
        str(value),
    ).strip()


# ============================================================
# MAIN
# ============================================================

def main():

    if len(sys.argv) < 2:

        print(
            "Usage:\n"
            "python generated/scraper_generator.py "
            "\"<review_url>\""
        )

        sys.exit(1)

    target_url = sys.argv[1]

    print("=" * 70)
    print("AI REVIEW SCRAPER")
    print("=" * 70)

    print(
        f"[INPUT] {target_url}"
    )

    # --------------------------------------------------------
    # STEP 1: Always inspect the live page first.
    # STEP 2: Load the fresh evidence.
    # STEP 3: Ask Groq for a strategy, then generate and run
    #         the deterministic scraper.
    # --------------------------------------------------------
    try:
        run_inspection(target_url)
    except Exception as exc:
        print(f"[INSPECTOR] FAILED: {exc}")
        sys.exit(1)

    print()
    print("=" * 70)
    print("STEP 2/3 - LOAD INSPECTION EVIDENCE")
    print("=" * 70)

    try:
        evidence = load_evidence()
    except Exception as exc:
        print(f"[EVIDENCE] FAILED: {exc}")
        sys.exit(1)

    print(
        f"[EVIDENCE] Strategy: "
        f"{evidence.get('review_strategy')}"
    )

    print(
        f"[EVIDENCE] Review candidates: "
        f"{len(evidence.get('review_candidates', []))}"
    )

    print(
        f"[EVIDENCE] Scroll containers: "
        f"{len(evidence.get('scroll_containers', []))}"
    )

    print(
        f"[EVIDENCE] Best fields: "
        f"{list(evidence.get('best_fields', {}).keys())}"
    )

    client = get_groq_client()

    previous_error = None

    print()
    print("=" * 70)
    print("STEP 3/3 - AI STRATEGY -> CODE GENERATION -> EXECUTION")
    print("=" * 70)

    for attempt in range(
        1,
        MAX_ATTEMPTS + 1,
    ):

        print()
        print("=" * 70)
        print(
            f"ATTEMPT {attempt}/{MAX_ATTEMPTS}"
        )
        print("=" * 70)

        if attempt > 1:

            print(
                f"[REGENERATION] Waiting "
                f"{REGENERATION_WAIT_SECONDS} seconds..."
            )

            time.sleep(
                REGENERATION_WAIT_SECONDS
            )

        # ----------------------------------------------------
        # Ask LLM only for strategy
        # ----------------------------------------------------

        try:

            strategy = generate_strategy(
                client,
                evidence,
                previous_error,
            )

        except Exception as exc:

            previous_error = str(exc)

            print(
                f"[STRATEGY] Failed: {exc}"
            )

            continue

        # ----------------------------------------------------
        # Verify every selector came from evidence
        # ----------------------------------------------------

        valid, error = validate_strategy(
            strategy,
            evidence,
        )

        if not valid:

            previous_error = error

            print(
                f"[STRATEGY] REJECTED: {error}"
            )

            continue

        print(
            "[STRATEGY] Evidence validation OK."
        )

        # ----------------------------------------------------
        # Generate deterministic executor
        # ----------------------------------------------------

        save_scraper(
            target_url,
            strategy,
        )

        # ----------------------------------------------------
        # Compile
        # ----------------------------------------------------

        compile_ok, compile_error = (
            compile_scraper()
        )

        if not compile_ok:

            previous_error = compile_error

            print(
                "[CHECK] Compilation failed:"
            )

            print(
                compile_error
            )

            continue

        print(
            "[CHECK] Compilation OK."
        )

        # ----------------------------------------------------
        # Delete stale output
        # ----------------------------------------------------

        if OUTPUT_PATH.exists():

            try:
                OUTPUT_PATH.unlink()
            except Exception:
                pass

        # ----------------------------------------------------
        # Execute
        # ----------------------------------------------------

        execution_ok, execution_error = (
            execute_scraper()
        )

        if not execution_ok:

            previous_error = execution_error

            print(
                "[EXECUTOR] FAILED:"
            )

            print(
                execution_error
            )

            continue

        # ----------------------------------------------------
        # Validate
        # ----------------------------------------------------

        valid, errors = validate_output(
            evidence
        )

        if valid:

            print()
            print("=" * 70)
            print("SUCCESS")
            print("=" * 70)

            print(
                f"Generated executor:\n"
                f"{GENERATED_SCRAPER}"
            )

            print(
                f"Output:\n"
                f"{OUTPUT_PATH}"
            )

            return

        previous_error = (
            "\n".join(errors[:20])
        )

        print(
            "[VALIDATOR] INVALID"
        )

        for error in errors[:20]:
            print(
                f"- {error}"
            )

    print()
    print("=" * 70)
    print("FINAL FAILURE")
    print("=" * 70)

    if previous_error:
        print(previous_error)

    sys.exit(1)


if __name__ == "__main__":
    main()