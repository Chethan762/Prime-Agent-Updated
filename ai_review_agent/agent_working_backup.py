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
        Keep the full inspection evidence on disk, but send only a
        bounded, high-value subset to GPT-OSS-120B.

        This prevents HTTP 413 Payload Too Large while preserving:
        - review structure
        - field examples
        - pagination/scroll evidence
        - observed API/GraphQL evidence
        - review-loading network request
        """

        def clip(value, limit):
            if value is None:
                return None

            if isinstance(value, str):
                return value[:limit]

            return value

        def clip_list(items, limit, item_limit):
            if not isinstance(items, list):
                return []

            return [
                clip(item, item_limit)
                for item in items[:limit]
            ]

        compact = {
            "url": evidence.get("url"),
            "title": clip(evidence.get("title"), 300),
            "final_url": clip(evidence.get("final_url"), 500),

            # Only a few representative DOM candidates are needed.
            "review_containers": clip_list(
                evidence.get("review_containers", []),
                3,
                1800,
            ),

            "field_examples": clip(
                evidence.get("field_examples", {}),
                3500,
            ),

            "pagination": clip_list(
                evidence.get("pagination", []),
                5,
                1000,
            ),

            "rating_examples": clip_list(
                evidence.get("rating_examples", []),
                5,
                500,
            ),

            "candidate_review_containers": clip_list(
                evidence.get("candidate_review_containers", []),
                5,
                1800,
            ),

            "review_strategy": evidence.get(
                "review_strategy",
                {}
            ),

            # Keep only the first few schema observations.
            "graphql_or_api_schema": clip_list(
                evidence.get("graphql_or_api_schema", []),
                4,
                2500,
            ),

            "dynamic_network": [],
        }

        # Only send likely review-loading network events.
        for item in evidence.get("dynamic_network", []):
            if not isinstance(item, dict):
                continue

            url = str(item.get("url") or "")

            blocked = (
                "nr-data.net",
                "newrelic",
                "bam.nr-data.net",
                "collector/business",
            )

            if any(x in url.lower() for x in blocked):
                continue

            request_body = str(
                item.get("request_body") or ""
            )

            response_preview = str(
                item.get("response_preview") or ""
            )

            # Keep request/response evidence bounded.
            compact["dynamic_network"].append({
                "url": url[:1000],
                "method": item.get("method"),
                "status": item.get("status"),
                "content_type": clip(
                    item.get("content_type"),
                    300,
                ),
                "request_body": request_body[:1800],
                "response_preview": response_preview[:2500],
            })

            if len(compact["dynamic_network"]) >= 3:
                break

        return compact

    # -----------------------------------------------------
    # Generate scraper using GPT-OSS 120B
    # -----------------------------------------------------

    def generate_scraper(self, evidence):

        api_key = os.environ.get(
            "GROQ_API_KEY"
        )

        if not api_key:
            print(
                "[GROQ] GROQ_API_KEY is not set."
            )
            return False

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

Requirements:

- Use playwright.async_api.
- Import:
  from playwright.async_api import TimeoutError as PlaywrightTimeoutError
- Launch Chromium headless.
- Extract actual individual reviews.
- Extract review_text, rating, and date when available.
- Use null only when a field is genuinely unavailable.
- Handle the actual loading/pagination mechanism shown
  by the evidence.
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
"""

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

            date = review.get(
                "date"
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
    # AI repair
    # -----------------------------------------------------

    def compact_repair_evidence(self, evidence):
        """Keep the repair prompt small enough for Groq payload limits."""
        compact = {
            "url": evidence.get("url"),
            "title": evidence.get("title"),
            "final_url": evidence.get("final_url"),
            "review_containers": evidence.get("review_containers", [])[:8],
            "field_examples": evidence.get("field_examples", {}),
            "pagination": evidence.get("pagination", [])[:8],
            "rating_examples": evidence.get("rating_examples", [])[:8],
            "candidate_review_containers": [],
        }

        # Candidate evidence is the most useful part for fixing selectors.
        # Keep only a few candidates and trim large text fields.
        for item in evidence.get("candidate_review_containers", [])[:4]:
            if not isinstance(item, dict):
                continue
            candidate = {}
            for key in (
                "tag", "class", "data-testid", "id",
                "short_text", "rating", "review_text", "date",
            ):
                value = item.get(key)
                if value is not None:
                    candidate[key] = str(value)[:700]
            compact["candidate_review_containers"].append(candidate)

        return compact

    def repair_generated_scraper(
        self,
        url,
        validation_report
    ):

        generated_file = (
            self.generated_dir /
            "review_scraper.py"
        )

        evidence_file = (
            self.generated_dir /
            "inspection_evidence.json"
        )

        if not generated_file.exists():

            print(
                "[REPAIR] Generated scraper not found"
            )

            return False

        if not evidence_file.exists():

            print(
                "[REPAIR] Inspection evidence not found"
            )

            return False

        api_key = os.environ.get(
            "GROQ_API_KEY"
        )

        if not api_key:

            print(
                "[REPAIR] GROQ_API_KEY is not set"
            )

            return False

        try:

            current_code = (
                generated_file.read_text(
                    encoding="utf-8"
                )
            )

            inspection_evidence = json.loads(
                evidence_file.read_text(
                    encoding="utf-8"
                )
            )

        except Exception as exc:

            print(
                "[REPAIR] Could not load repair inputs:"
            )
            print(exc)

            return False

        evidence = self.compact_evidence(
            inspection_evidence
        )

        prompt = f"""
Repair the existing Playwright review scraper.

TARGET URL:
{url}

INSPECTION EVIDENCE:
{json.dumps(
    evidence,
    ensure_ascii=False,
    indent=2
)}

VALIDATION FAILURE:
{json.dumps(
    validation_report,
    ensure_ascii=False,
    indent=2
)}

CURRENT SCRAPER:
{current_code[:14000]}

Repair requirements:

1. Use the inspection evidence as the authoritative
   description of the website.

2. Diagnose the validation failure.

3. Identify INDIVIDUAL repeated review containers.

4. Never use a broad ancestor selector such as:
   div:has-text('Verified Purchase')
   when it can match a parent containing multiple reviews.

5. Every website-specific selector must be directly
   supported by the inspection evidence.

6. Candidate review containers are observed DOM evidence.
   Use their structure instead of remembered selectors.

7. Do not invent CSS selectors, XPath selectors,
   class names, data attributes, endpoints, headers,
   API schemas, or review data.

8. Extract actual review_text.

9. Extract rating when supported.

10. Extract date when supported.

11. Use null only when the field is genuinely unavailable.

12. Never produce a record where all three are null:
       review_text = null
       rating = null
       date = null

13. Do not include page-level content, navigation,
    header, footer, login, cart, or product information.

14. Deduplicate reviews.

15. Preserve working Playwright logic where possible.

16. Use the observed loading/pagination mechanism.

17. Do not invent selectors, APIs, or endpoints.

18. Use Playwright async API and Python standard library only.

19. Save JSON exactly to Path(__file__).resolve().parent.parent / "output" / "reviews.json".

20. Return ONLY complete repaired Python source code.
"""

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

            # Groq may enforce a rolling TPM window. The generation
            # request and repair request can occur back-to-back, so
            # retry a 429 after the rate-limit window has had time to
            # clear instead of failing the self-correction stage.

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
                            25,
                            int(retry_after)
                        ) if retry_after else 25
                    except (TypeError, ValueError):
                        wait_seconds = 25

                    if attempt == 2:
                        raise

                    print(
                        f"[REPAIR] Groq rate limit reached. "
                        f"Retrying in {wait_seconds}s..."
                    )

                    time.sleep(wait_seconds)

            if result is None:
                print("[REPAIR] No response from Groq")
                return False

            repaired_code = (
                result["choices"][0]
                ["message"]["content"]
            ).strip()

            fenced = re.search(
                r"```(?:python)?\s*(.*?)```",
                repaired_code,
                re.DOTALL |
                re.IGNORECASE
            )

            if fenced:
                repaired_code = (
                    fenced.group(1).strip()
                )

            if not repaired_code:

                print(
                    "[REPAIR] Model returned empty code"
                )

                return False

            compile(
                repaired_code,
                str(generated_file),
                "exec"
            )

            generated_file.write_text(
                repaired_code + "\n",
                encoding="utf-8"
            )

            print(
                "[REPAIR] Scraper repaired successfully"
            )

            return True

        except Exception as exc:

            print(
                "[REPAIR] Groq repair failed:"
            )
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
                "[MAIN] Scraper generation failed."
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
        print("STEP 4: AI SELF-CORRECTION")
        print("=" * 70)

        repair_success = (
            agent.repair_generated_scraper(
                url,
                validation_report
            )
        )

        if not repair_success:

            print(
                "[MAIN] AI repair failed."
            )

            return

        print()
        print("=" * 70)
        print("STEP 5: EXECUTE REPAIRED SCRAPER")
        print("=" * 70)

        repaired_execution = (
            agent.execute_generated_scraper()
        )

        if not repaired_execution:

            print(
                "[MAIN] Repaired scraper execution failed."
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
            print("SUCCESS: AI REPAIR PASSED VALIDATION")
            print("=" * 70)

        else:

            print()
            print("=" * 70)
            print("FINAL VALIDATION FAILED")
            print("=" * 70)

    finally:

        agent.close()


if __name__ == "__main__":
    main()
