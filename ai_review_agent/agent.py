import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

from ipython_inspector import inspect_site


# =========================================================
# Configuration
# =========================================================

GENERATOR_MODEL = "openai/gpt-oss-120b"  # Groq generation only


# =========================================================
# AI Review Scraper Agent
# =========================================================

class AIReviewAgent:

    def __init__(self):

        self.project_root = Path(__file__).resolve().parent

        self.generated_dir = (
            self.project_root / "generated"
        )

        self.output_dir = (
            self.project_root / "output"
        )

        self.generated_dir.mkdir(
            exist_ok=True
        )

        self.output_dir.mkdir(
            exist_ok=True
        )


    # -----------------------------------------------------
    # Direct IPython / Playwright inspection
    # -----------------------------------------------------

    def inspect(self, url):

        evidence_file = (
            self.generated_dir /
            "inspection_evidence.json"
        )

        if evidence_file.exists():
            evidence_file.unlink()

        print()
        print("[INSPECTOR] Starting direct IPython inspector...")
        print("[INSPECTOR] No Groq/LLM call is used for inspection.")
        print("[INSPECTOR] Playwright will collect browser evidence.")
        print("[INSPECTOR] Opening:", url, flush=True)

        try:
            evidence = asyncio.run(
                inspect_site(url)
            )
        except Exception as exc:
            print("[INSPECTOR] Inspection failed:", flush=True)
            print(exc, flush=True)
            return False

        if not evidence_file.exists():
            print("[INSPECTOR] Evidence file was not created.")
            return False

        try:
            with evidence_file.open(
                "r",
                encoding="utf-8"
            ) as f:
                evidence = json.load(f)

            print("[INSPECTOR] Evidence created:", evidence_file)
            print(
                "[INSPECTOR] Candidate containers:",
                len(evidence.get("candidate_review_containers", [])),
            )
            print(
                "[INSPECTOR] Pagination controls:",
                len(evidence.get("pagination", [])),
            )
            print(
                "[INSPECTOR] Dynamic network events:",
                len(evidence.get("dynamic_network", [])),
            )
            print(
                "[INSPECTOR] Review strategy:",
                evidence.get("review_strategy", {}).get("type"),
            )
            print("[INSPECTOR] Inspection completed.", flush=True)

            return evidence

        except Exception as exc:
            print("[INSPECTOR] Could not read evidence:", flush=True)
            print(exc, flush=True)
            return False

    # -----------------------------------------------------
    # Compact evidence for LLM
    # -----------------------------------------------------

    def compact_evidence(self, evidence):
        """
        Build a deliberately small, structured LLM evidence packet.

        Important: never truncate serialized JSON blindly. A blind character
        cutoff can remove reviewer/rating/date fields and leave the model with
        incomplete evidence. The full inspection_evidence.json remains on disk.
        """

        def s(value, limit):
            if value is None:
                return None
            return str(value)[:limit]

        def field_summary(fields):
            if not isinstance(fields, dict):
                return {}
            out = {}
            for name in ("review_text", "rating", "reviewer_name", "date"):
                value = fields.get(name)
                if isinstance(value, dict):
                    out[name] = {
                        "selector": s(value.get("selector"), 500),
                        "examples": [
                            s(x, 180) for x in (value.get("examples") or [])[:3]
                        ],
                    }
                elif value is not None:
                    out[name] = s(value, 500)
            return out

        def candidate_summary(items, limit=3):
            result = []
            for item in (items or [])[:limit]:
                if not isinstance(item, dict):
                    continue
                result.append({
                    "selector": s(
                        item.get("selector")
                        or item.get("container_selector")
                        or item.get("css_selector"),
                        700,
                    ),
                    "count": item.get("count"),
                    "fields": field_summary(item.get("fields")),
                    "text_preview": s(
                        item.get("text_preview")
                        or item.get("text")
                        or item.get("preview"),
                        700,
                    ),
                })
            return result

        strategy = evidence.get("review_strategy") or {}
        compact_strategy = {
            k: strategy.get(k)
            for k in (
                "type",
                "confidence",
                "api_pagination_detected",
                "paginated_fetch",
                "infinite_page",
                "has_more_pages",
                "cursor_pagination_detected",
                "graphql_detected",
                "page_numbers_detected",
                "dynamic_scroll_detected",
                "network_evidence_count",
                "candidate_count",
            )
            if k in strategy
        }

        # Preserve the field mapping explicitly. This is more important than
        # large DOM/network samples for scraper generation.
        field_examples = evidence.get("field_examples") or {}
        compact_fields = field_summary(field_examples)

        if not compact_fields:
            # Some inspector versions store field evidence inside candidates.
            for candidate in (
                evidence.get("candidate_review_containers")
                or evidence.get("review_containers")
                or []
            )[:3]:
                for name, value in field_summary(candidate.get("fields")).items():
                    compact_fields.setdefault(name, value)

        # Rank network evidence instead of taking the first requests.
        # The first requests are often captcha/analytics traffic. For
        # api_pagination pages, the review-loading request is the most
        # important evidence for scraper generation.
        raw_network = []
        for item in evidence.get("dynamic_network", []) or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            low = url.lower()
            if any(x in low for x in (
                "nr-data.net", "newrelic", "bam.nr-data.net",
                "collector/business",
            )):
                continue

            body = str(item.get("request_body") or "")
            response = str(item.get("response_preview") or "")
            combined = (url + " " + body + " " + response).lower()

            score = 0
            for marker, weight in (
                ("review", 8),
                ("allreviews", 8),
                ("paginatedfetch", 8),
                ("pagenumber", 7),
                ("hasmorepages", 7),
                ("infinitepage", 6),
                ("cursor", 5),
                ("nextpage", 5),
                ("reviewcontext", 5),
                ("pagefetch", 4),
            ):
                if marker in combined:
                    score += weight

            raw_network.append((score, item))

        raw_network.sort(key=lambda pair: pair[0], reverse=True)

        network = []
        for score, item in raw_network[:4]:
            network.append({
                "relevance_score": score,
                "url": str(item.get("url") or "")[:900],
                "method": item.get("method"),
                "status": item.get("status"),
                "content_type": s(item.get("content_type"), 160),
                "request_body": s(item.get("request_body"), 1000),
                "response_preview": s(item.get("response_preview"), 1200),
            })

        api_schema = []
        for item in evidence.get("graphql_or_api_schema", []) or []:
            if not isinstance(item, dict):
                continue
            api_schema.append({
                k: s(item.get(k), 500)
                for k in (
                    "type", "url", "method", "query", "variables",
                    "response_path", "review_node", "cursor_field",
                    "has_next_page_field",
                )
                if item.get(k) is not None
            })
            if len(api_schema) >= 2:
                break

        return {
            "url": s(evidence.get("url"), 1200),
            "title": s(evidence.get("title"), 250),
            "final_url": s(evidence.get("final_url"), 700),
            "review_strategy": compact_strategy,
            "review_fields": compact_fields,
            "review_containers": candidate_summary(
                evidence.get("review_containers"), 3
            ),
            "candidate_review_containers": candidate_summary(
                evidence.get("candidate_review_containers"), 3
            ),
            "rating_examples": [
                s(x, 180)
                for x in (evidence.get("rating_examples") or [])[:3]
            ],
            "pagination": candidate_summary(
                evidence.get("pagination"), 2
            ),
            "pagination_controls_count": len(evidence.get("pagination") or []),
            "dynamic_network": network,
            "graphql_or_api_schema": api_schema,
        }

    def validate_generated_code_contract(self, code, evidence):
        """
        Static safety/quality gate before executing LLM-generated code.
        This catches common generic-agent failures early and lets the single
        delayed regeneration attempt receive precise feedback.
        """
        problems = []
        low = code.lower()

        if "evaluate_handle(" in low:
            problems.append("Generated scraper uses Locator.evaluate_handle(); use Locator APIs instead.")

        if "page.wait_for_selector(" in low:
            strategy = evidence.get("review_strategy") or {}
            if evidence.get("pagination") == [] or len(evidence.get("pagination") or []) == 0:
                # Do not forbid all wait_for_selector calls, only obvious use as
                # the primary review-container discovery mechanism.
                if "wait_for_selector(" in low and (
                    "fwi7j_" in low or "review_container_selector" in low
                ):
                    problems.append(
                        "Generated scraper waits on a repeated review selector; "
                        "use count-based repeated-container discovery instead."
                    )

        strategy = evidence.get("review_strategy") or {}
        if (
            (strategy.get("api_pagination_detected") or strategy.get("paginated_fetch"))
            and strategy.get("has_more_pages")
            and len(evidence.get("pagination") or []) == 0
        ):
            if "button:has-text(\"next\")" in low or "has-text('next')" in low:
                problems.append(
                    "Inspector found zero pagination controls; generated scraper invented a Next button."
                )

        if 'locator("")' in low or "locator('')" in low:
            problems.append("Generated scraper constructs an empty Playwright selector.")

        if problems:
            print("[GROQ] Generated scraper failed the pre-execution contract:")
            for problem in problems:
                print(f"[GROQ] - {problem}")
            return False

        return True

    # -----------------------------------------------------
    # Generate scraper using GPT-OSS 120B
    # -----------------------------------------------------

    def generate_scraper(self, evidence, extra_context=None, allow_retry=True):

        api_key = os.environ.get(
            "GROQ_API_KEY"
        )

        if not api_key:
            print(
                "[GROQ] GROQ_API_KEY is not set."
            )
            return False

        original_evidence = evidence

        evidence = self.compact_evidence(
            evidence
        )

        generated_file = (
            self.generated_dir /
            "review_scraper.py"
        )

        output_file = (
            self.output_dir /
            "reviews.json"
        )

        retry_context = ""
        if extra_context:
            retry_context = f"""
IMPORTANT PREVIOUS-ATTEMPT FEEDBACK:
{extra_context}

Generate a fresh scraper and specifically address the previous
failure. Do not blindly repeat the failed implementation.
"""

        prompt = f"""
Generate a standalone Python Playwright review scraper
from the browser evidence below.

BROWSER EVIDENCE:
{json.dumps(
    evidence,
    ensure_ascii=False,
    indent=2
)}

TARGET URL:
{evidence["url"]}

OUTPUT JSON:
{output_file}

{retry_context}

Requirements:

- Use playwright.async_api.
- Import:
  from playwright.async_api import TimeoutError as PlaywrightTimeoutError
- Launch Chromium headless.
- Extract actual individual reviews with ALL FOUR logical fields:
- review_text
- rating
- reviewer_name
- date
- reviewer_name and date are required whenever the inspection
  evidence shows those fields exist.
- Never silently omit reviewer_name or date.
- Use null only after genuinely attempting field-level extraction.
- Keep all four fields aligned to the SAME review container/record.
- Handle the actual loading/pagination mechanism shown
  by the evidence.
- The inspection evidence is authoritative about the navigation
  mechanism. DO NOT invent a "Next" button, page-number control,
  or click-based pagination when pagination_controls_count is 0.
- If review_strategy.type is "api_pagination" or the evidence says
  api_pagination_detected/has_more_pages is true, inspect the
  supplied dynamic_network and graphql_or_api_schema FIRST.
  Prefer the observed review-loading request and its real page/cursor
  parameters. Do not replace an observed API pagination flow with
  a fictional DOM button.
- If an observed API request has a page number, offset, cursor,
  paginatedFetch, hasMorePages, or similar continuation evidence,
  continue the same observed mechanism until the evidence indicates
  no more reviews or no new unique review records are returned.
- If API replay is not sufficiently supported by the evidence,
  use the observed browser scroll/container behavior as the fallback.
- Treat review_strategy, dynamic_network, and
  graphql_or_api_schema as first-class browser evidence.
- If review_strategy says API/GraphQL pagination is detected
  AND dynamic_network contains a successful review-loading
  request, prefer reproducing that observed request over
  scraping rendered review cards.
- For GraphQL, use ONLY the captured endpoint, HTTP method,
  query shape, variable names, response path, review node
  fields, cursor field, and pageInfo fields shown in the
  evidence. Do not invent a GraphQL schema or endpoint.
- For cursor pagination, continue using the observed cursor
  flow (for example first/after, edge cursor, endCursor) until
  the observed hasNextPage/hasMorePages value is false or no
  new review records are returned. If the boolean is not
  captured, use repeated cursor/new-record stopping logic.
- Map structured API fields directly when the evidence proves
  their meaning. Do not manufacture reviewer names from an ID
  unless the evidence identifies that ID as a reviewer field.
- Use dynamic_network evidence only when it actually
  describes review loading.
- Do not invent API endpoints.
- Do not assume GraphQL merely because a script contains the
  word GraphQL; require matching successful network behavior.
- If a captured response is HTML, parse the HTML through
  Playwright/browser DOM mechanisms.
- Reproduce browser actions such as scrolling or clicking
  when those actions load more reviews.
- Use DOM growth and newly extracted unique reviews as
  fallback stopping signals.
- Response wait timeouts must be non-fatal when DOM growth
  proves that more reviews loaded.
- Every website-specific selector must be supported by
  review_containers, field_examples, or
  candidate_review_containers.
- Do not use selectors remembered from another website.
- Do not use broad selectors such as:
  div:has-text('Verified Purchase')
  when they can match ancestor containers containing
  multiple reviews.
- Candidate review containers are evidence of observed
  repeated structures. Infer the smallest safe traversal
  from them.
- Extract reviewer_name and date RELATIVE TO EACH individual
  review container.
- Prefer selectors explicitly present in the candidate
  review field evidence.
- IMPORTANT: an evidence selector is NOT guaranteed to be unique.
- NEVER use a global field selector such as page.locator(field_selector)
  when the same selector can occur in ratings summaries, product
  specifications, navigation, or multiple reviews. First identify the
  repeated review container, then extract each field RELATIVE to that
  one container.
- Do not use a rating selector as a proxy for finding reviews unless
  the evidence explicitly proves that every matching element belongs
  to one review record. Rating widgets elsewhere on the page must be
  excluded.
- The selector shown in evidence may match repeated review cards,
  ancestor containers, summary widgets, or multiple field elements.
- NEVER use an evidence selector directly with inner_text() unless
  you first inspect locator.count() and confirm it identifies the
  intended element.
- For the REVIEW CONTAINER selector, locator.count() > 1 is normal.
  Treat repeated matches as candidate cards and iterate through them;
  do not wait_for_selector() expecting the container selector to be
  unique.
- For FIELD selectors such as review_text, rating, reviewer_name,
  and date, use them relative to ONE candidate review container.
  If the field locator count is 0, try another evidence-supported
  field selector or a structural/semantic fallback before giving up.
  If count > 1, disambiguate rather than calling inner_text() on the
  multi-match locator.
- Create safe field extraction helpers that use short timeouts and
  locator.count() before reading text. A missing field must not crash
  the entire scraper.
- NEVER use wait_for_selector() with a selector merely because it
  appears in evidence. Wait for a stable page/review root or use a
  count-based check instead.
- Do not let Playwright strict-mode violations or a missing field
  selector terminate the scraper.
- Prefer structural relationships visible in the evidence over CSS
  class names alone. CSS classes may be reused by unrelated Flipkart
  widgets.
- When a candidate review selector resolves to several elements,
  inspect their text previews/structure and choose the repeated
  review-card pattern, not the first element blindly.
- Before extraction, verify that a candidate card contains review-like
  text and at least one of the observed rating/reviewer/date signals.
- If the observed field selector is stale or absent at runtime, use
  DOM traversal within the candidate card to locate the corresponding
  field by the evidence's text examples, attributes, relative position,
  or semantic structure.
- If reviewer_name/date have no direct selector, derive them
  from the same review container using the supplied structural
  evidence.
- Never extract reviewer names or dates globally across the page.
- For reviewer_name, reject labels such as "more", "Helpful",
  "Verified Purchase", product metadata, navigation labels,
  and location-only text.
- Never invent reviewer names.
- Do not include page-level, navigation, footer, cart,
  login, or product information as review_text.
- Deduplicate reviews.
- Use Playwright and Python standard library only.
- Do not install packages.
- Do not hard-code machine-specific absolute paths.
- Save the final JSON list EXACTLY to:
  Path(__file__).resolve().parent.parent
  / "output"
  / "reviews.json"
- The scraper must run independently from Prime Agent.
- Do not execute the scraper during generation.

Return ONLY complete Python source code.
Do not use Markdown fences.

ADDITIONAL HARD REQUIREMENTS FOR PLAYWRIGHT CODE:
- NEVER use Locator.evaluate_handle() or ElementHandle-based traversal. Do not use evaluate_handle anywhere in the generated scraper.
- Prefer Playwright Locator APIs: locator(), count(), nth(), all_inner_texts(), get_attribute(), evaluate() only when absolutely necessary and always await it.
- Never construct a Locator from an empty selector. Validate selector strings before calling locator().
- Never use a broad page-level selector as the review-record iterator when the evidence provides a repeated review container. The review container is the unit of one record.
- Before accepting a candidate review container, require a review-like text signal and at least one additional field signal (rating, reviewer, or date) when those fields are detected by the inspector.
- Deduplicate only after extracting complete candidate records; do not turn arbitrary DOM nodes into records merely because they contain text.
- If API pagination is detected, do not invent a Next button. Use the observed request/schema when sufficient; otherwise use the observed review scroll container as the fallback and stop only after repeated scrolls produce no new unique review records.
- Do not generate a scraper that merely loops over every element matching a field selector such as a rating class.
- A field selector is NEVER the record selector.
- If the evidence selector is too broad to identify a record safely, use the candidate review container and locate fields inside it.
- The generated scraper must save a JSON list of review objects even when some optional fields are null.
- Avoid decorative Unicode characters in console print statements; use ASCII output only for Windows compatibility.

FINAL SAFETY REQUIREMENTS:
- The scraper must tolerate selectors that match 0, 1, or many nodes.
- It must never fail solely because a selector is non-unique.
- It must never fail solely because one optional field selector is
  missing.
- Use small per-field timeouts (for example 2-5 seconds) and continue
  extraction when an optional field is unavailable.
- Before returning each review, normalize missing optional values to
  null rather than raising an exception.
- The review container itself must be selected as a collection and
  iterated; do not assume a unique review container.
"""

        # Groq request-size guard.
        # Never truncate JSON at an arbitrary character boundary: doing so can
        # cut a field mapping in half and deprive the model of rating/reviewer/date.
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > 14000:
            print(
                f"[GROQ] Compact prompt exceeded 14 KB ({prompt_bytes / 1024:.1f} KB); "
                "using field-priority evidence."
            )

            priority_evidence = {
                "url": evidence.get("url"),
                "review_strategy": evidence.get("review_strategy"),
                "review_fields": evidence.get("review_fields"),
                "review_containers": evidence.get("review_containers"),
                "candidate_review_containers": evidence.get(
                    "candidate_review_containers"
                ),
                "dynamic_network": evidence.get("dynamic_network", [])[:1],
                "graphql_or_api_schema": evidence.get(
                    "graphql_or_api_schema", []
                )[:1],
            }

            priority_json = json.dumps(
                priority_evidence,
                ensure_ascii=False,
                separators=(",", ":"),
            )

            # Keep the evidence structured. If still large, progressively
            # remove low-value samples rather than cutting through JSON.
            if len(priority_json.encode("utf-8")) > 8500:
                priority_evidence["candidate_review_containers"] = (
                    priority_evidence["candidate_review_containers"][:1]
                )
                priority_evidence["review_containers"] = (
                    priority_evidence["review_containers"][:1]
                )
                priority_evidence["dynamic_network"] = []
                priority_evidence["graphql_or_api_schema"] = []
                priority_json = json.dumps(
                    priority_evidence,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

            prompt = f"""
Generate a standalone Python Playwright review scraper from the browser
evidence below.

BROWSER EVIDENCE:
{priority_json}

TARGET URL:
{evidence.get("url")}

OUTPUT JSON:
{output_file}

{retry_context[:2500]}

Requirements:
- Use playwright.async_api.
- Extract review_text, rating, reviewer_name, and date when the evidence
  shows those fields exist.
- Preserve all four fields on every output record; use null only after a
  genuine field-level attempt.
- Scope every field to the SAME individual review container.
- Treat CSS selectors as evidence, not guarantees of uniqueness.
- NEVER use wait_for_selector() as proof that a selector identifies a review.
- Select review containers as a collection and iterate them.
- Before field extraction, safely handle 0, 1, or multiple matches.
- Never call inner_text() or text_content() on a locator that may match
  multiple elements unless it is explicitly narrowed to one element.
- Prefer DOM relationships and field semantics over brittle class names.
- If a selector is missing or ambiguous, try another selector supported by
  the evidence before returning null.
- Handle the pagination/scroll/API strategy shown in the evidence.
- Do not invent selectors, API endpoints, or schemas.
- Save JSON to the requested output path.
- Return only complete Python source code, with no Markdown fences.
"""
            prompt_bytes = len(prompt.encode("utf-8"))

        print(
            f"[GROQ] Prompt size: {prompt_bytes / 1024:.1f} KB",
            flush=True,
        )

        payload = {
            "model": GENERATOR_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "temperature": 0,
            "max_tokens": 3000,
        }

        prompt_bytes = len(
            prompt.encode("utf-8")
        )

        print(
            f"[GROQ] Prompt size: {prompt_bytes / 1024:.1f} KB",
            flush=True,
        )

        request = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps(
                payload
            ).encode("utf-8"),
            headers={
                "Authorization":
                    f"Bearer {api_key}",
                "Content-Type":
                    "application/json",
                "User-Agent":
                    "AIReviewAgent/1.0",
            },
            method="POST",
        )

        try:

            # Groq can return HTTP 429 when the rolling token window is
            # still active. Keep the generation request small and retry
            # after the server-provided Retry-After value when available.
            result = None

            for attempt in range(3):

                try:
                    with urllib.request.urlopen(
                        request,
                        timeout=300
                    ) as response:
                        result = json.loads(
                            response.read().decode(
                                "utf-8"
                            )
                        )
                    break

                except urllib.error.HTTPError as http_error:

                    if http_error.code != 429:
                        raise

                    retry_after = http_error.headers.get(
                        "Retry-After"
                    )

                    try:
                        wait_seconds = max(
                            20,
                            int(float(retry_after))
                        ) if retry_after else 20
                    except (TypeError, ValueError):
                        wait_seconds = 20

                    if attempt == 2:
                        raise

                    print(
                        f"[GROQ] Rate limit reached. "
                        f"Retrying in {wait_seconds}s..."
                    )
                    time.sleep(wait_seconds)

            if result is None:
                print("[GROQ] No response from Groq")
                return False

            code = (
                result["choices"][0]
                ["message"]["content"]
            ).strip()

            fenced = re.search(
                r"```(?:python)?\s*(.*?)```",
                code,
                re.DOTALL |
                re.IGNORECASE
            )

            if fenced:
                code = fenced.group(1).strip()

            if not code:
                print(
                    "[GROQ] Empty scraper code."
                )
                return False

            compile(
                code,
                str(generated_file),
                "exec"
            )

            if not self.validate_generated_code_contract(
                code,
                original_evidence,
            ):
                print(
                    "[GROQ] Treating contract violation as a failed generation."
                )
                return False

            generated_file.write_text(
                code + "\n",
                encoding="utf-8"
            )

            print(
                "[GROQ] Scraper generated:",
                generated_file
            )

            return True

        except urllib.error.HTTPError as exc:

            if exc.code == 413:
                print(
                    "[GROQ] HTTP 413: evidence payload is still too large."
                )
                print(
                    "[GROQ] The inspector evidence is safe; only the "
                    "LLM payload needs further reduction."
                )
            else:
                print("[GROQ] HTTP error:", exc)

            return False

        except SyntaxError as exc:

            print(
                "[GROQ] Generated scraper contains invalid Python:"
            )
            print(
                f"[GROQ] {exc}"
            )
            print(
                "[GROQ] Treating invalid code as a failed generation."
            )
            print(
                "[GROQ] No automatic nested retry will occur."
            )

            return False

        except Exception as exc:

            print(
                "[GROQ] Generation failed:"
            )
            print(exc)

            return False

    # -----------------------------------------------------
    # Full generation stage
    # -----------------------------------------------------

    def scrape(self, url):

        generated_file = (
            self.generated_dir /
            "review_scraper.py"
        )

        output_file = (
            self.output_dir /
            "reviews.json"
        )

        for path in (
            generated_file,
            output_file,
        ):

            if path.exists():
                path.unlink()

        evidence = self.inspect(url)

        if evidence is False:
            return False

        return self.generate_scraper(
            evidence
        )

    # -----------------------------------------------------
    # Execute generated scraper
    # -----------------------------------------------------

    def execute_generated_scraper(self):

        generated_file = (
            self.generated_dir /
            "review_scraper.py"
        )

        if not generated_file.exists():

            print(
                "[EXECUTOR] Generated scraper not found."
            )

            return False

        print(
            "[EXECUTOR] Running generated scraper..."
        )

        try:

            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            child_env["PYTHONUTF8"] = "1"

            result = subprocess.run(
                [
                    sys.executable,
                    str(generated_file)
                ],
                cwd=str(
                    self.project_root
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
                timeout=180,
            )

        except subprocess.TimeoutExpired:

            print(
                "[EXECUTOR] Scraper timed out."
            )

            return False

        if result.stdout:

            print(
                "[SCRAPER STDOUT]"
            )
            print(
                result.stdout
            )

        if result.stderr:

            print(
                "[SCRAPER STDERR]"
            )
            print(
                result.stderr
            )

        if result.returncode != 0:

            print(
                "[EXECUTOR] Scraper failed "
                f"with exit code {result.returncode}"
            )

            return False

        print(
            "[EXECUTOR] Generated scraper "
            "completed successfully"
        )

        return True

    # -----------------------------------------------------
    # Validation
    # -----------------------------------------------------

    def validate_output(self):

        output_file = (
            self.output_dir /
            "reviews.json"
        )

        report = {
            "valid": False,
            "total_reviews": 0,
            "nonempty_review_text": 0,
            "missing_ratings": 0,
            "missing_reviewer_names": 0,
            "missing_dates": 0,
            "short_or_empty": 0,
            "page_level_contamination": 0,
            "duplicate_reviews": 0,
            "errors": [],
        }

        if not output_file.exists():

            report["errors"].append(
                "output/reviews.json does not exist"
            )

            return report

        try:

            with output_file.open(
                "r",
                encoding="utf-8"
            ) as f:
                data = json.load(f)

        except Exception as exc:

            report["errors"].append(
                f"Invalid JSON: {exc}"
            )

            return report

        if not isinstance(data, list):

            report["errors"].append(
                "Output JSON is not a list"
            )

            return report

        report["total_reviews"] = len(data)

        if not data:

            report["errors"].append(
                "No reviews extracted"
            )

            return report

        seen = set()

        contamination_markers = (
            "Login",
            "Cart",
            "ABOUT Contact",
            "GROUP COMPANIES",
            "3,063 ratings and 350 reviews",
        )

        for review in data:

            if not isinstance(
                review,
                dict
            ):
                continue

            text = str(
                review.get(
                    "review_text"
                ) or ""
            ).strip()

            rating = review.get(
                "rating"
            )

            reviewer_name = (
                review.get("reviewer_name")
                or review.get("reviewer")
                or review.get("user_name")
            )

            date = (
                review.get("date")
                or review.get("review_date")
            )

            if text:
                report[
                    "nonempty_review_text"
                ] += 1

            if len(text) < 20:
                report[
                    "short_or_empty"
                ] += 1

            if rating is None:
                report[
                    "missing_ratings"
                ] += 1

            if (
                reviewer_name is None
                or not str(reviewer_name).strip()
            ):
                report[
                    "missing_reviewer_names"
                ] += 1

            if (
                date is None
                or not str(date).strip()
            ):
                report[
                    "missing_dates"
                ] += 1

            if any(
                marker.lower()
                in text.lower()
                for marker in contamination_markers
            ):
                report[
                    "page_level_contamination"
                ] += 1

            key = (
                text,
                str(rating),
                str(date),
            )

            if key in seen:
                report[
                    "duplicate_reviews"
                ] += 1
            else:
                seen.add(key)

        total = report[
            "total_reviews"
        ]

        if total == 0:
            report["errors"].append(
                "No review records"
            )

        if report[
            "nonempty_review_text"
        ] == 0:
            report["errors"].append(
                "No non-empty review_text values"
            )

        if report[
            "page_level_contamination"
        ] > 0:
            report["errors"].append(
                "Page-level content detected"
            )

        if report[
            "short_or_empty"
        ] > total * 0.20:
            report["errors"].append(
                "Too many short/empty reviews"
            )

        if report[
            "missing_ratings"
        ] > total * 0.50:
            report["errors"].append(
                "More than 50% of reviews "
                "are missing ratings"
            )

        # The inspector tells us whether reviewer_name/date were
        # actually detected on the target page. Only require those
        # fields when the evidence says they exist.
        evidence_file = (
            self.project_root
            / "generated"
            / "inspection_evidence.json"
        )

        detected_fields = set()

        try:
            if evidence_file.exists():
                inspection = json.loads(
                    evidence_file.read_text(
                        encoding="utf-8"
                    )
                )

                candidates = (
                    inspection.get("candidate_review_containers", [])
                    or inspection.get("review_containers", [])
                )

                for candidate in candidates:
                    fields = candidate.get(
                        "fields",
                        {}
                    )

                    if "rating" in fields:
                        detected_fields.add("rating")

                    if "reviewer_name" in fields:
                        detected_fields.add("reviewer_name")

                    if "date" in fields:
                        detected_fields.add("date")
        except Exception:
            pass

        if (
            "rating" in detected_fields
            and report["missing_ratings"] > 0
        ):
            report["errors"].append(
                "Missing rating values"
            )

        if (
            "reviewer_name" in detected_fields
            and report["missing_reviewer_names"] > 0
        ):
            report["errors"].append(
                "Missing reviewer_name values"
            )

        if (
            "date" in detected_fields
            and report["missing_dates"] > 0
        ):
            report["errors"].append(
                "Missing date values"
            )

        if report[
            "duplicate_reviews"
        ] > total * 0.20:
            report["errors"].append(
                "Too many duplicate reviews"
            )

        report["valid"] = (
            total > 0
            and report["nonempty_review_text"] > 0
            and not report["errors"]
        )

        print(
            f"Reviews extracted: {total}"
        )
        print(
            "Non-empty review_text:",
            report["nonempty_review_text"]
        )
        print(
            "Missing ratings:",
            report["missing_ratings"]
        )
        print(
            "Missing reviewer names:",
            report["missing_reviewer_names"]
        )
        print(
            "Missing dates:",
            report["missing_dates"]
        )
        print(
            "Short/empty reviews:",
            report["short_or_empty"]
        )
        print(
            "Page-level contamination:",
            report["page_level_contamination"]
        )
        print(
            "Duplicate reviews:",
            report["duplicate_reviews"]
        )
        print(
            "VALID:",
            report["valid"]
        )

        return report

    # -----------------------------------------------------
    # Delayed regeneration
    # -----------------------------------------------------

    def wait_before_regeneration(self, seconds=90):
        """Wait before the second Groq generation request."""
        import time

        print()
        print("[RETRY] Previous scraper attempt failed; starting delayed regeneration.")
        print(
            f"[RETRY] Waiting {seconds} seconds before regeneration..."
        )

        for remaining in range(seconds, 0, -1):
            if remaining == seconds or remaining % 10 == 0:
                print(
                    f"[RETRY] Regeneration starts in {remaining} seconds..."
                )
            time.sleep(1)

        print("[RETRY] Wait completed.")

    def regenerate_scraper(self, url, validation_report):
        """
        Generate a fresh scraper from the existing inspection evidence.

        The website is NOT inspected again. The normal Groq generation
        function is reused, so there is only one implementation of the
        Groq request and syntax validation.
        """

        evidence_file = (
            self.project_root
            / "generated"
            / "inspection_evidence.json"
        )

        if not evidence_file.exists():
            print("[RETRY] Inspection evidence not found.")
            return False

        try:
            evidence = json.loads(
                evidence_file.read_text(
                    encoding="utf-8"
                )
            )

            safe_validation_report = validation_report
            try:
                safe_validation_report = json.loads(
                    json.dumps(validation_report, ensure_ascii=False, default=str)
                )
            except Exception:
                safe_validation_report = str(validation_report)

            failure_context = json.dumps(
                {
                    "initial_attempt_failed": True,
                    "validation_report": safe_validation_report,
                    "instruction": (
                        "The first generated scraper failed. Generate a "
                        "fresh scraper from the same inspection evidence. "
                        "Pay special attention to the supplied failure "
                        "report. If the failure is a strict-mode or "
                        "non-unique-selector error, do not blindly reuse "
                        "that selector; scope the field to the individual "
                        "review container and disambiguate it using DOM "
                        "relationships, semantics, attributes, nearby "
                        "labels/text, or a more-specific selector. "
                        "If the inspector reports api_pagination and zero "
                        "pagination controls, do not invent or click a "
                        "Next button; use the observed API continuation "
                        "request or observed scroll behavior. Preserve "
                        "reviewer_name and date when the evidence shows "
                        "those fields exist and keep all fields aligned "
                        "within each individual review record."
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )

            # Never let validation feedback dominate the next LLM request.
            if len(failure_context) > 3500:
                failure_context = failure_context[:3500] + "\n[validation feedback truncated]"

            print(
                "[RETRY] Reusing existing inspection evidence."
            )
            print(
                "[RETRY] No second Playwright inspection will run."
            )

            return self.generate_scraper(
                evidence,
                extra_context=f"""
TARGET URL:
{url}

{failure_context}
""",
                allow_retry=False,
            )

        except Exception as exc:
            print("[RETRY] Scraper regeneration failed:")
            print(exc)
            return False

    # -----------------------------------------------------
    # Close
    # -----------------------------------------------------

    def close(self):
        # No Prime Agent subprocess is used for inspection.
        pass


# =========================================================
# Main
# =========================================================

def main():

    agent = AIReviewAgent()

    url = input(
        "Enter review page URL: "
    ).strip()

    if not url:

        print(
            "URL is required."
        )

        agent.close()
        return

    try:

        print()
        print("=" * 70)
        print("STEP 1: IPYTHON INSPECTION + SCRAPER GENERATION")
        print("=" * 70)

        generated = agent.scrape(
            url
        )

        if not generated:

            print(
                "[MAIN] Initial scraper generation failed."
            )

            print()
            print("=" * 70)
            print("STEP 2: WAIT 90 SECONDS + REGENERATE")
            print("=" * 70)

            generation_failure_report = {
                "valid": False,
                "failure_type": "generation_error",
                "message": (
                    "The first Groq generation failed or returned invalid "
                    "Python. Generate a fresh scraper from the same "
                    "inspection evidence. Do not perform a second website "
                    "inspection."
                ),
            }

            agent.wait_before_regeneration(90)

            regenerated = agent.regenerate_scraper(
                url,
                generation_failure_report
            )

            if not regenerated:
                print(
                    "[MAIN] Single regeneration attempt failed."
                )
                return

            print()
            print("=" * 70)
            print("STEP 3: EXECUTE REGENERATED SCRAPER")
            print("=" * 70)

            regenerated_execution = agent.execute_generated_scraper()

            if not regenerated_execution:
                print(
                    "[MAIN] Regenerated scraper execution failed."
                )
                return

            print()
            print("=" * 70)
            print("STEP 4: FINAL VALIDATION")
            print("=" * 70)

            final_validation = agent.validate_output()

            if final_validation["valid"]:
                print()
                print("=" * 70)
                print("SUCCESS: REGENERATED SCRAPER PASSED VALIDATION")
                print("=" * 70)
            else:
                print()
                print("=" * 70)
                print("FINAL VALIDATION FAILED")
                print("=" * 70)
                print(
                    "[MAIN] The single regeneration attempt also failed."
                )

            return

        print()
        print("=" * 70)
        print("STEP 2: EXECUTE GENERATED SCRAPER")
        print("=" * 70)

        scraper_ok = (
            agent.execute_generated_scraper()
        )

        if not scraper_ok:

            print(
                "[MAIN] Initial scraper execution failed."
            )

            print()
            print("=" * 70)
            print("STEP 3: WAIT 90 SECONDS + REGENERATE")
            print("=" * 70)

            # Execution failure is also a failed first attempt.
            # Reuse the SAME inspection evidence; do not inspect again.
            agent.wait_before_regeneration(90)

            execution_failure_report = {
                "valid": False,
                "failure_type": "execution_error",
                "message": (
                    "The first generated scraper failed during execution. "
                    "The failure may be caused by a Playwright strict-mode "
                    "violation from a non-unique field selector. Generate "
                    "fresh code from the same inspection evidence. Never "
                    "blindly call inner_text() on a locator that can match "
                    "multiple elements. Scope every field to the individual "
                    "review container and disambiguate the intended field "
                    "using DOM relationships, semantics, attributes, "
                    "nearby labels/text, or a more-specific selector."
                ),
            }

            regenerated = agent.regenerate_scraper(
                url,
                execution_failure_report
            )

            if not regenerated:

                print(
                    "[MAIN] Scraper regeneration failed."
                )

                return

            print()
            print("=" * 70)
            print("STEP 4: EXECUTE REGENERATED SCRAPER")
            print("=" * 70)

            regenerated_execution = (
                agent.execute_generated_scraper()
            )

            if not regenerated_execution:

                print(
                    "[MAIN] Regenerated scraper execution failed."
                )

                return

            print()
            print("=" * 70)
            print("STEP 5: FINAL VALIDATION")
            print("=" * 70)

            final_validation = agent.validate_output()

            if final_validation["valid"]:

                print()
                print("=" * 70)
                print("SUCCESS: REGENERATED SCRAPER PASSED VALIDATION")
                print("=" * 70)

            else:

                print()
                print("=" * 70)
                print("FINAL VALIDATION FAILED")
                print("=" * 70)

                print(
                    "[MAIN] The single regeneration attempt also failed."
                )

            return

        print()
        print("=" * 70)
        print("STEP 3: VALIDATE GENERATED RESULTS")
        print("=" * 70)

        validation_report = (
            agent.validate_output()
        )

        if validation_report["valid"]:

            print()
            print("=" * 70)
            print("SUCCESS: SCRAPER PASSED VALIDATION")
            print("=" * 70)

            return

        print()
        print("=" * 70)
        print("STEP 4: WAIT 90 SECONDS + REGENERATE")
        print("=" * 70)

        # Reuse the existing inspection evidence.
        # No second Playwright inspection is performed.
        agent.wait_before_regeneration(90)

        regenerated = (
            agent.regenerate_scraper(
                url,
                validation_report
            )
        )

        if not regenerated:

            print(
                "[MAIN] Scraper regeneration failed."
            )

            return

        print()
        print("=" * 70)
        print("STEP 5: EXECUTE REGENERATED SCRAPER")
        print("=" * 70)

        regenerated_execution = (
            agent.execute_generated_scraper()
        )

        if not regenerated_execution:

            print(
                "[MAIN] Regenerated scraper execution failed."
            )

            return

        print()
        print("=" * 70)
        print("STEP 6: FINAL VALIDATION")
        print("=" * 70)

        final_validation = (
            agent.validate_output()
        )

        if final_validation["valid"]:

            print()
            print("=" * 70)
            print("SUCCESS: REGENERATED SCRAPER PASSED VALIDATION")
            print("=" * 70)

        else:

            print()
            print("=" * 70)
            print("FINAL VALIDATION FAILED")
            print("=" * 70)

            print(
                "[MAIN] The single regeneration attempt also failed."
            )

    finally:

        agent.close()


if __name__ == "__main__":
    main()
