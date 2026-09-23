import asyncio
import json
import os
import re
import sys
import urllib.request

from ipython_inspector import inspect_site


MODEL = "openai/gpt-oss-120b"

def compact_evidence(evidence):
    compact = {
        "url": evidence.get("url"),
        "title": evidence.get("title"),
        "final_url": evidence.get("final_url"),
        "review_containers": evidence.get("review_containers", []),
        "field_examples": evidence.get("field_examples", {}),
        "pagination": evidence.get("pagination", []),
        "rating_examples": evidence.get("rating_examples", []),
        "candidate_review_containers": evidence.get(
            "candidate_review_containers", []
        ),
        "dynamic_network": []
    }

    # Keep only useful network evidence.
    # Avoid sending telemetry, analytics, and huge request bodies.
    for item in evidence.get("dynamic_network", []):
        url = item.get("url", "")

        if any(
            blocked in url.lower()
            for blocked in (
                "nr-data.net",
                "newrelic",
                "bam.nr-data.net",
                "collector/business"
            )
        ):
            continue

        compact["dynamic_network"].append({
            "url": url,
            "method": item.get("method"),
            "status": item.get("status"),
            "content_type": item.get("content_type"),
            "request_body": (
                item.get("request_body") or ""
            )[:1000],
            "response_preview": (
                item.get("response_preview") or ""
            )[:2000],
        })

    return compact


def call_groq(evidence):
    evidence = compact_evidence(evidence)

    api_key = os.environ.get("GROQ_API_KEY")

    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set")

    prompt = f"""
Generate a standalone Python Playwright scraper from the browser evidence below.

BROWSER EVIDENCE:
{json.dumps(evidence, ensure_ascii=False)}

TARGET URL:
{evidence["url"]}

Requirements:
- Use playwright.async_api.
- Import the Playwright timeout exception exactly as:
  from playwright.async_api import TimeoutError as PlaywrightTimeoutError
- Do NOT import PlaywrightTimeoutError directly from playwright.async_api.
- Launch Chromium headless.
- Extract reviews from the actual selectors in the evidence.
- Handle the actual pagination/loading mechanism shown in the evidence.
- Extract review_text, rating, and date.
- If dynamic_network evidence is present, inspect it carefully and use the actual captured request URL, response type, and response HTML structure when determining how reviews load.
- Prefer the observed browser network behavior over assumptions about GraphQL, REST, or static HTML.
- NEVER invent or guess CSS selectors, XPath selectors, class names, data attributes, endpoints, or field selectors.
- Every selector used in the generated scraper MUST be directly supported by review_containers, field_examples, or candidate_review_containers in the provided evidence.
- If review_containers is empty, candidate_review_containers is the primary evidence for discovering the repeated review structure.
- Candidate evidence contains observed DOM text and field structure, not guaranteed selectors. Infer the smallest generic DOM traversal needed from the observed structure; do not substitute selectors from prior knowledge of the target website.
- Do not use remembered selectors from Flipkart, Amazon, Google, or any other website.
- Do NOT use GraphQL merely because graphql_or_api_schema contains a token, schema text, or query example. Use GraphQL only if the evidence shows that the page actually loads reviews through GraphQL.
- If the observed network response is an HTML fragment, parse that HTML with Playwright or an equivalent HTML parser instead of inventing a JSON or GraphQL API.
- If the observed request depends on browser context, referrer, cookies, or other page state, trigger or reproduce that request from the existing Playwright page/context rather than navigating directly to the API URL.
- When the evidence shows that scrolling or another browser action triggers review-loading requests, implement that same browser action and capture the resulting response.
- When waiting for a response triggered by an action, the response waiter MUST wrap the triggering action. Prefer this exact Playwright pattern: async with page.expect_response(lambda r: <matching condition>, timeout=10000) as response_info: await <triggering action such as page.evaluate(scroll)>; response = await response_info.value. Do not call page.wait_for_response before the action unless a separate listener is already registered and the action is still performed afterward.
- Do not make the response matcher depend on an assumed page number such as page=2 or page=N unless the evidence explicitly proves that exact value is required. Match the observed review-loading endpoint/pattern instead, then inspect the actual response URL after capture.
- For dynamically paginated infinite-scroll sites, use the number of newly extracted unique reviews and/or unchanged scroll height as the stopping condition rather than relying only on hard-coded page numbers.
- For infinite-scroll sites, do not make successful scraping depend exclusively on page.expect_response succeeding. A scroll may trigger DOM growth even if the response waiter misses or times out. Use DOM review-count/scroll-height growth as a fallback signal and parse the current page DOM after scrolling when appropriate.
- A robust infinite-scroll loop should tolerate a response-wait timeout when the page has visibly loaded additional review elements, rather than immediately failing.
- When using page.expect_response, the Playwright timeout exception can be raised when exiting the async-with context. Therefore the ENTIRE async-with block MUST be inside the try/except that catches PlaywrightTimeoutError. Never put the try/except only around response_info.value.
- A response timeout must never terminate the scraper when DOM growth or newly loaded review elements can still be detected. Treat it as a non-fatal signal and continue with DOM-based validation.
- After each scroll, compare the number of review containers and/or scroll height before and after the action. Continue when new review content appears; stop only after repeated lack of new content or when the site's loading mechanism clearly indicates completion.
- If custom request headers such as Referer are required, actually apply them to the request/context or reproduce the original browser action that automatically supplies them; never create an unused headers dictionary.
- Do not assume an API endpoint can be opened directly unless the evidence shows it works independently of browser context.
- When a captured request has a required Referer or other browser-specific header, preserve the observed header values and send the request through the existing Playwright page/context with those headers.
- Prefer reproducing the site's observed scroll/click/load action in the browser when that is the mechanism that triggers the request, rather than constructing the API request manually.
- Reuse the existing Playwright browser/context/page whenever possible; do not launch an unnecessary second browser just to parse an HTML fragment.
- Use Playwright DOM APIs or Python standard-library modules for parsing. Do not import BeautifulSoup, requests, lxml, pandas, or any other third-party package unless that package is explicitly confirmed as available in the evidence or project environment.
- The generated scraper must run with the project's existing Playwright installation without requiring additional package installation.
- If pagination/loading adds reviews to an existing page, do not append the same review multiple times.
- Deduplicate reviews using a stable combination of available fields such as review_text, date, rating, and any observed review identifier.
- Continue loading using the actual observed mechanism until the pagination control disappears or no new reviews are added.
- Use null for fields that are not available.
- Do not invent selectors, endpoints, headers, tokens, API schemas, or review data.
- Save a JSON list to generated/reviews.json.
- The scraper must run independently.

Return ONLY the complete Python source code.
Do not use Markdown fences.
"""

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0,
        "max_tokens": 5000,
    }

    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "AIReviewAgent/1.0",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=300) as response:
        result = json.loads(response.read().decode("utf-8"))

    print("[OPENROUTER] Finish reason:", result["choices"][0].get("finish_reason"))
    return result["choices"][0]["message"]["content"]


async def main():
    if len(sys.argv) != 2:
        print("Usage: python direct_generator.py <url>")
        sys.exit(1)

    url = sys.argv[1]

    print("[INSPECTOR] Inspecting website...")
    evidence = await inspect_site(url)

    print(
        "[INSPECTOR] Evidence size:",
        len(json.dumps(evidence)),
        "characters",
    )

    print("[GROQ] Generating scraper...")

    code = call_groq(evidence)

    # Clean model output: remove Markdown fences and prose before Python source.
    code = code.strip()
    fenced = re.search(r"```(?:python)?\s*(.*?)```", code, re.DOTALL | re.IGNORECASE)
    if fenced:
        code = fenced.group(1).strip()
    else:
        lines = code.splitlines()
        start = None
        for i, line in enumerate(lines):
            if re.match(r"^\s*(?:from\s+\S+\s+import\s+|import\s+\S+)", line):
                start = i
                break
        if start is not None:
            code = "\n".join(lines[start:]).strip()

    output_dir = "generated"
    os.makedirs(output_dir, exist_ok=True)

    scraper_path = os.path.join(output_dir, "review_scraper.py")

    with open(scraper_path, "w", encoding="utf-8") as f:
        f.write(code + "\n")

    print("[GROQ] Scraper generated:", scraper_path)
    print("[GROQ] Generated characters:", len(code))


if __name__ == "__main__":
    asyncio.run(main())
