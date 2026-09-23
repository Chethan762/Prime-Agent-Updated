"""
Generic Review Inspector v4
---------------------------
Website-independent Playwright inspection layer.

v4 focuses on:
1. Finding real review arrays inside dynamic JSON responses.
2. Recording exact JSON paths and representative objects.
3. Discovering pagination fields from the same review response.
4. Rejecting analytics/telemetry responses even when the page URL contains
   words such as "review".
5. Detecting DOM review candidates using structural/text heuristics.
6. Recording scroll growth and pagination-control evidence.

No website/domain-specific rules are used.
No Groq/LLM call is made here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page, Response, async_playwright


ROOT = Path(__file__).resolve().parent
GENERATED = ROOT / "generated"
EVIDENCE_FILE = GENERATED / "inspection_evidence.json"

MAX_NETWORK_EVENTS = 100
MAX_REVIEW_API = 20
MAX_REVIEW_SAMPLES = 3
MAX_SCROLL_ROUNDS = 18
MAX_TEXT = 900

# Generic extraction patterns used to interpret observed combined DOM metadata.
RELATIVE_DATE_RE = re.compile(r"\b(?:today|yesterday|\d+\s+(?:day|days|week|weeks|month|months|year|years)\s+ago)\b", re.I)
ABSOLUTE_DATE_RE = re.compile(r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s*,?\s+\d{4})\b", re.I)


REVIEW_TERMS = {
    "review", "reviews", "rating", "ratings", "reviewtext",
    "reviewer", "reviewers", "reviewbody", "reviewcontent",
    "comment", "comments", "feedback", "testimonial",
}

PAGINATION_TERMS = {
    "page", "pageno", "page_no", "pagenumber", "page_number",
    "offset", "cursor", "nextpage", "next_page", "hasmore",
    "has_more", "loadmore", "load_more", "infinite",
}

TELEMETRY_TERMS = {
    "analytics", "telemetry", "collector", "newrelic", "nrjs",
    "jserrors", "web-vitals", "webvitals", "performance",
    "tracking", "events", "beacon", "metrics", "error-report",
    "errorreport", "datacollector",
}

PAGINATION_LABELS = {
    "next", "previous", "prev", "load more", "show more",
    "view more", "more reviews", "older", "newer",
}


def clean(value: Any, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value[:limit]


def safe_request_post_data(request) -> str:
    """Safely read POST request data without crashing on binary payloads."""
    try:
        data = request.post_data
        if data:
            return data[:10000]
    except Exception:
        pass

    # Playwright exposes raw request bytes on some versions.
    try:
        raw = request.post_data_buffer
        if raw:
            return raw[:10000].decode("utf-8", errors="replace")
    except Exception:
        pass

    return ""


def norm(value: Any) -> str:
    return clean(value, 500).lower()


def path_to_string(path: List[Any]) -> str:
    out = "$"
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            safe = re.sub(r"[^A-Za-z0-9_$]", "_", str(part))
            out += f".{safe}"
    return out


def walk_json(
    value: Any,
    path: Optional[List[Any]] = None,
    depth: int = 0,
    max_depth: int = 12,
):
    if path is None:
        path = []

    if depth > max_depth:
        return

    yield path, value

    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk_json(
                child, path + [key], depth + 1, max_depth
            )

    elif isinstance(value, list):
        for i, child in enumerate(value[:100]):
            yield from walk_json(
                child, path + [i], depth + 1, max_depth
            )


def key_review_score(keys: List[str]) -> int:
    text = " ".join(norm(k) for k in keys)
    score = 0

    for term in REVIEW_TERMS:
        if term in text:
            score += 3

    return score


def object_review_score(obj: Dict[str, Any]) -> int:
    keys = [str(k) for k in obj.keys()]
    score = key_review_score(keys)

    values = " ".join(
        clean(v, 300)
        for v in obj.values()
        if isinstance(v, (str, int, float, bool))
    ).lower()

    if re.search(r"\b[1-5](?:\.\d)?\b", values):
        score += 1

    if any(
        re.search(pattern, values)
        for pattern in [
            r"\bverified purchase\b",
            r"\breview\b",
            r"\brated\b",
            r"\bhelpful\b",
        ]
    ):
        score += 2

    text_lengths = [
        len(str(v))
        for v in obj.values()
        if isinstance(v, str)
    ]

    if any(length >= 25 for length in text_lengths):
        score += 1

    return score


def array_review_score(items: List[Any]) -> int:
    if not items or not all(isinstance(x, dict) for x in items[:10]):
        return 0

    sample = items[:10]
    scores = [object_review_score(x) for x in sample]

    strong = sum(score >= 4 for score in scores)
    moderate = sum(score >= 2 for score in scores)

    if strong >= 2:
        return 10 + strong

    if moderate >= 3:
        return 6 + moderate

    return 0


def schema_for_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counter = {}

    for item in items[:20]:
        for key, value in item.items():
            k = str(key)
            info = counter.setdefault(
                k,
                {
                    "key": k,
                    "observed_count": 0,
                    "value_types": set(),
                    "examples": [],
                },
            )

            info["observed_count"] += 1

            if value is None:
                info["value_types"].add("null")
            elif isinstance(value, bool):
                info["value_types"].add("boolean")
            elif isinstance(value, (int, float)):
                info["value_types"].add("number")
            elif isinstance(value, list):
                info["value_types"].add("array")
            elif isinstance(value, dict):
                info["value_types"].add("object")
            else:
                info["value_types"].add("string")

            if len(info["examples"]) < 2:
                example = clean(value, 250)
                if example and example not in info["examples"]:
                    info["examples"].append(example)

    result = []

    for info in counter.values():
        result.append(
            {
                "key": info["key"],
                "observed_count": info["observed_count"],
                "value_types": sorted(info["value_types"]),
                "examples": info["examples"],
            }
        )

    return sorted(
        result,
        key=lambda x: x["observed_count"],
        reverse=True,
    )


def find_review_arrays(data: Any) -> List[Dict[str, Any]]:
    results = []

    for path, value in walk_json(data):
        if not isinstance(value, list):
            continue

        if len(value) < 2:
            continue

        score = array_review_score(value)

        if score <= 0:
            continue

        items = [
            x for x in value[:20]
            if isinstance(x, dict)
        ]

        if len(items) < 2:
            continue

        results.append(
            {
                "json_path": path_to_string(path),
                "score": score,
                "item_count_observed": len(value),
                "sample_items": items[:MAX_REVIEW_SAMPLES],
                "schema": schema_for_items(items),
            }
        )

    results.sort(
        key=lambda x: (
            x["score"],
            x["item_count_observed"],
        ),
        reverse=True,
    )

    # Deduplicate identical paths.
    seen = set()
    unique = []

    for result in results:
        if result["json_path"] in seen:
            continue
        seen.add(result["json_path"])
        unique.append(result)

    return unique[:10]


def find_review_array_paths(data: Any) -> List[Dict[str, Any]]:
    paths = []
    for path, value in walk_json(data):
        if not isinstance(value, list) or len(value) < 2:
            continue
        score = array_review_score(value)
        if score <= 0:
            continue
        if not all(isinstance(x, dict) for x in value[:2]):
            continue
        paths.append({"path": path, "score": score})
    paths.sort(key=lambda x: x["score"], reverse=True)
    return paths[:10]


def find_pagination_fields(data: Any) -> List[Dict[str, Any]]:
    """Find pagination fields only in/near observed review arrays.

    This avoids promoting unrelated page/user/experiment pagination fields
    to review pagination evidence.
    """
    found = []
    review_paths = [r["path"] for r in find_review_array_paths(data)]

    if not review_paths:
        return []

    def relevant_path(candidate):
        for rp in review_paths:
            # Same object, an ancestor, or a direct child of the review-array
            # parent is relevant to pagination for that array.
            if candidate[:len(rp)-1] == rp[:-1]:
                return True
            if len(candidate) >= len(rp) and candidate[:len(rp)-1] == rp[:-1]:
                return True
            if len(candidate) <= len(rp) and rp[:len(candidate)] == candidate:
                return True
        return False

    for path, value in walk_json(data):
        if not isinstance(value, dict):
            continue

        for key, child in value.items():
            key_norm = norm(key).replace(" ", "_")
            candidate = path + [key]

            if not (
                key_norm in PAGINATION_TERMS
                or any(term in key_norm for term in PAGINATION_TERMS)
            ):
                continue

            if not relevant_path(candidate):
                continue

            found.append(
                {
                    "json_path": path_to_string(candidate),
                    "key": str(key),
                    "value": clean(child, 500),
                }
            )

    # Deduplicate exact paths.
    unique = []
    seen = set()
    for item in found:
        if item["json_path"] in seen:
            continue
        seen.add(item["json_path"])
        unique.append(item)

    return unique[:40]


def contains_telemetry(url: str, body: str, preview: str) -> bool:
    text = norm(f"{url} {body} {preview}")
    return any(term in text for term in TELEMETRY_TERMS)


def contains_review_signal(url: str, body: str, preview: str) -> bool:
    text = norm(f"{url} {body} {preview}")
    return any(term in text for term in REVIEW_TERMS)


def url_pagination(url: str) -> Dict[str, Any]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    keys = {
        k: v[:3]
        for k, v in query.items()
        if k.lower() in {
            "page", "pageno", "page_no", "offset", "cursor",
            "start", "from", "skip", "limit", "count",
        }
    }

    return {
        "query_parameters": keys,
        "detected": bool(keys),
    }


class Inspector:
    def __init__(self, url: str):
        self.url = url
        self.network_events = []
        self.review_api_evidence = []
        self.scroll_containers = []
        self.scroll_observations = []
        self.review_candidates = []
        self.review_counts = []
        self.pagination_candidates = []
        self.best_field_schema = {}

    async def response_handler(self, response: Response):
        request = response.request

        if request.resource_type not in {"xhr", "fetch"}:
            return

        # Never access request.post_data directly here: some sites send
        # binary/compressed POST bodies and Playwright can raise UnicodeDecodeError.
        body = safe_request_post_data(request)

        content_type = response.headers.get("content-type", "")

        parsed = None
        preview = ""

        if "json" in content_type.lower():
            try:
                parsed = await response.json()
                preview = clean(
                    json.dumps(
                        parsed,
                        ensure_ascii=False,
                    ),
                    2000,
                )
            except Exception:
                pass

        if not preview and "text" in content_type.lower():
            try:
                preview = clean(await response.text(), 2000)
            except Exception:
                pass

        review_arrays = []
        pagination_fields = []

        if parsed is not None:
            review_arrays = find_review_arrays(parsed)
            pagination_fields = find_pagination_fields(parsed)

        # Strong evidence must come from the RESPONSE JSON itself.
        is_review_api = bool(review_arrays)

        # If no review array exists, do not promote a request merely because
        # the current page URL contains "review".
        telemetry = contains_telemetry(
            response.url, body, preview
        )

        event = {
            "type": "response",
            "method": request.method,
            "url": response.url,
            "status": response.status,
            "resource_type": request.resource_type,
            "content_type": content_type,
            "request_body_preview": clean(body, 1800),
            "response_preview": preview,
            "review_array_detected": is_review_api,
            "review_arrays": review_arrays,
            "pagination_fields": pagination_fields,
            "url_pagination": url_pagination(response.url),
            "telemetry_like": telemetry,
        }

        self.network_events.append(event)

        if is_review_api and not telemetry:
            self.review_api_evidence.append(event)

    async def request_handler(self, request):
        if request.resource_type not in {"xhr", "fetch"}:
            return

        self.network_events.append(
            {
                "type": "request",
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
                "post_data_preview": clean(
                    safe_request_post_data(request),
                    1800,
                ),
                "url_pagination": url_pagination(request.url),
            }
        )

    async def safe_evaluate(self, page: Page, expression: str, arg=None, retries: int = 4):
        """Run page.evaluate safely across SPA/navigation context changes."""
        last_error = None
        for attempt in range(retries):
            try:
                if arg is None:
                    return await page.evaluate(expression)
                return await page.evaluate(expression, arg)
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                transient = (
                    "execution context was destroyed" in message
                    or "most likely because of a navigation" in message
                    or "target page, context or browser has been closed" in message
                )
                if not transient:
                    raise
                await page.wait_for_timeout(700 + attempt * 500)
        raise last_error

    async def wait_for_stable_page(self, page: Page):
        """Give client-side redirects/hydration a chance to settle."""
        for _ in range(3):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(700)

    async def discover_scroll_containers(self, page: Page):
        self.scroll_containers = await self.safe_evaluate(
            page,
            """
            () => Array.from(document.querySelectorAll('*'))
              .map((el, index) => {
                const s = getComputedStyle(el);
                const r = el.getBoundingClientRect();

                if (
                  el.scrollHeight <= el.clientHeight + 20 ||
                  el.clientHeight < 120 ||
                  r.width < 250 ||
                  r.height < 120
                ) return null;

                return {
                  index,
                  tag: el.tagName.toLowerCase(),
                  id: el.id || '',
                  class_name:
                    typeof el.className === 'string'
                      ? el.className.slice(0, 300)
                      : '',
                  aria_label: el.getAttribute('aria-label') || '',
                  role: el.getAttribute('role') || '',
                  data_testid: el.getAttribute('data-testid') || '',
                  client_height: el.clientHeight,
                  scroll_height: el.scrollHeight,
                  scroll_top: el.scrollTop,
                  width: Math.round(r.width),
                  height: Math.round(r.height)
                };
              })
              .filter(Boolean)
              .sort((a,b) => b.scrollHeight - a.scrollHeight)
              .slice(0, 15)
            """
        )

    async def dom_reviews(self, page: Page):
        raw = await self.safe_evaluate(
            page,
            r"""
        () => {
          const nodes = Array.from(document.querySelectorAll('*'));
          const output = [];

          const normaliseText = (value) =>
            (value || '').replace(/\s+/g, ' ').trim();

          /*
           * Keep every regex on ONE physical JavaScript line.
           * This avoids the "Invalid regular expression: missing /"
           * error caused by multiline regex literals.
           */
          const ratingRe = /^\s*[1-5](?:\.\d)?\s*(?:\/5|out of 5|stars?)?\s*$/i;

          const dateRe = /^(?:today|yesterday|\d+\s+(?:day|days|week|weeks|month|months|year|years)\s+ago|\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s*,?\s+\d{4})$/i;

          const extractDate = (value) => {
            const t = normaliseText(value);
            const match = t.match(/(?:today|yesterday|\d+\s+(?:day|days|week|weeks|month|months|year|years)\s+ago|\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s*,?\s+\d{4})/i);
            return match ? match[0].trim() : '';
          };

          const extractReviewerName = (value) => {
            const t = normaliseText(value);
            if (!t) return '';
            const separators = [/\s*,\s*/, /\s+·\s+/, /\s+\|\s+/];
            for (const re of separators) {
              const parts = t.split(re).map(x => x.trim()).filter(Boolean);
              if (parts.length >= 2) {
                const first = parts[0];
                if (first.length >= 2 && first.length <= 60 && !/\d/.test(first) &&
                    /^[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,5}$/.test(first) &&
                    !/verified purchase|helpful|ago|today|yesterday/i.test(first)) {
                  return first;
                }
              }
            }
            return '';
          };

          const metadataWords = [
            'verified purchase',
            'helpful',
            'today',
            'yesterday',
            'ago'
          ];

          /*
           * Generic review-text heuristic.
           * No website-specific selector is used.
           */
          const isLikelyReviewText = (value) => {
            const t = normaliseText(value);
            const lower = t.toLowerCase();

            if (t.length < 25 || t.length > 500)
              return false;

            if (ratingRe.test(t))
              return false;

            if (/^verified purchase$/i.test(t))
              return false;

            if (/^helpful(?:\s+for\s+\d+(?:\s+\d+)?)?$/i.test(t))
              return false;

            if (dateRe.test(t))
              return false;

            if (metadataWords.some(word => lower === word))
              return false;

            const words = t
              .split(/\s+/)
              .filter(Boolean);

            if (words.length < 5)
              return false;

            /*
             * Review text usually contains punctuation
             * or enough words to represent a sentence.
             */
            return /[.!?,]/.test(t) || words.length >= 8;
          };

          /*
           * Generic reviewer-name heuristic.
           */
          const isLikelyReviewerName = (value) => {
            const t = normaliseText(value);

            if (t.length < 2 || t.length > 80)
              return false;

            // A reviewer-name field should be a compact name, not a
            // name-plus-location or a metadata container.
            if (t.includes(','))
              return false;

            if (/\d/.test(t))
              return false;

            if (/verified purchase|helpful/i.test(t))
              return false;

            if (dateRe.test(t))
              return false;

            if (ratingRe.test(t))
              return false;

            const words = t
              .split(/\s+/)
              .filter(Boolean);

            if (words.length < 1 || words.length > 6)
              return false;

            return words.every(word =>
              /^[A-Za-z][A-Za-z.'-]*$/.test(word)
            );
          };

          for (const el of nodes) {

            const text = normaliseText(
              el.innerText || el.textContent || ''
            );

            if (text.length < 35 || text.length > 2500)
              continue;

            const r = el.getBoundingClientRect();

            if (r.width < 120 || r.height < 20)
              continue;

            const attrs = [
              el.getAttribute('aria-label') || '',
              el.getAttribute('data-testid') || '',
              el.getAttribute('itemtype') || '',
              el.getAttribute('class') || '',
              el.id || ''
            ].join(' ');

            const lower =
              (text + ' ' + attrs).toLowerCase();

            let score = 0;

            if (/\breview\b|\breviews\b/.test(lower))
              score += 4;

            if (/\brating\b|\bstars?\b/.test(lower))
              score += 3;

            if (/\bverified purchase\b/.test(lower))
              score += 3;

            if (/\bhelpful\b/.test(lower))
              score += 2;

            if (/\b\d(?:\.\d)?\s*(?:\/5|out of 5|stars?)\b/.test(lower))
              score += 3;

            if (/\b(?:ago|yesterday|today|month|months|year|years)\b/.test(lower))
              score += 1;

            if (score < 5)
              continue;

            /*
             * Evidence-backed selector for review container.
             */
            let selector = '';

            if (el.id) {

              selector =
                '#' + CSS.escape(el.id);

            } else if (el.getAttribute('data-testid')) {

              selector =
                '[data-testid="' +
                CSS.escape(
                  el.getAttribute('data-testid')
                ) +
                '"]';

            } else {

              const classes =
                Array.from(el.classList || [])
                  .filter(Boolean)
                  .slice(0, 3)
                  .map(
                    c => '.' + CSS.escape(c)
                  )
                  .join('');

              selector =
                el.tagName.toLowerCase() +
                classes;
            }

            /*
             * Build selector relative to the
             * detected review container.
             */
            const relativeSelector = (root, node) => {

              const parts = [];
              let cur = node;

              while (
                cur &&
                cur !== root &&
                cur.nodeType === 1
              ) {

                let part =
                  cur.tagName.toLowerCase();

                if (cur.id) {

                  part +=
                    '#' +
                    CSS.escape(cur.id);

                  parts.unshift(part);
                  break;
                }

                const parent =
                  cur.parentElement;

                if (!parent)
                  break;

                const siblings =
                  Array.from(parent.children)
                    .filter(
                      x =>
                        x.tagName ===
                        cur.tagName
                    );

                if (siblings.length > 1) {

                  part +=
                    ':nth-of-type(' +
                    (siblings.indexOf(cur) + 1) +
                    ')';
                }

                parts.unshift(part);
                cur = parent;
              }

              return parts.join(' > ');
            };

            const fieldCandidates = [];

            /*
             * Inspect child elements for actual
             * review fields.
             */
            for (
              const child of Array.from(
                el.querySelectorAll('*')
              ).slice(0, 120)
            ) {

              const childText =
                normaliseText(
                  child.innerText ||
                  child.textContent ||
                  ''
                );

              if (
                !childText ||
                childText.length > 500
              )
                continue;

              const childLower =
                childText.toLowerCase();

              // Prefer leaf-like elements. Ancestors often contain the
              // reviewer name, location, date and review text together.
              const meaningfulDescendants = Array.from(child.querySelectorAll('*'))
                .map(node => normaliseText(node.innerText || node.textContent || ''))
                .filter(value => value && value !== childText);

              const hasNearDuplicateDescendant = meaningfulDescendants.some(value =>
                value.length >= 2 &&
                (value === childText || childText.includes(value))
              );

              const fieldTypes = [];

              /*
               * Rating
               */
              if (ratingRe.test(childText)) {
                fieldTypes.push('rating');
              }

              /*
               * Verified purchase
               */
              if (
                /^verified purchase$/i.test(childText)
              ) {
                fieldTypes.push(
                  'verified_purchase'
                );
              }

              /*
               * Date
               */
              const extractedDate = extractDate(childText);
              if (extractedDate) {
                fieldTypes.push('date');
              }

              /*
               * Helpful
               */
              if (
                /^helpful(?:\s+for\s+\d+(?:\s+\d+)?)?$/i.test(childText)
              ) {
                fieldTypes.push('helpful');
              }

              /*
               * Review text
               */
              if (
                isLikelyReviewText(
                  childText
                )
              ) {
                fieldTypes.push(
                  'review_text'
                );
              }

              /*
               * Reviewer name
               */
              const extractedReviewer = extractReviewerName(childText);
              if (
                isLikelyReviewerName(childText) ||
                extractedReviewer
              ) {
                fieldTypes.push(
                  'reviewer_name'
                );
              }

              // Ancestor elements frequently contain several metadata fields
              // together. Keep only leaf-like matches for field extraction.
              if (fieldTypes.length && !hasNearDuplicateDescendant) {

                const semanticAttrs = {
                  aria_label: child.getAttribute('aria-label') || '',
                  itemprop: child.getAttribute('itemprop') || '',
                  data_testid: child.getAttribute('data-testid') || '',
                  class_name: child.getAttribute('class') || '',
                  id: child.id || '',
                  title: child.getAttribute('title') || ''
                };

                fieldCandidates.push({
                  types: [
                    ...new Set(fieldTypes)
                  ],
                  selector:
                    relativeSelector(
                      el,
                      child
                    ),
                  tag:
                    child.tagName.toLowerCase(),
                  has_meaningful_descendants:
                    hasNearDuplicateDescendant,
                  semantic_attrs: semanticAttrs,
                  text:
                    childText.slice(0, 300),
                  extracted_text: {
                    reviewer_name: extractedReviewer || '',
                    date: extractedDate || ''
                  },
                  extraction: {
                    reviewer_name: extractedReviewer ? 'first_name_segment' : '',
                    date: extractedDate ? 'date_phrase' : ''
                  }
                });
              }
            }

            /*
             * Remove duplicate field candidates.
             */
            const uniqueFields = [];
            const seenFields = new Set();

            for (
              const field of fieldCandidates
            ) {

              const key =
                field.types.join('|') +
                '|' +
                field.selector +
                '|' +
                field.text;

              if (seenFields.has(key))
                continue;

              seenFields.add(key);
              uniqueFields.push(field);
            }

            /*
             * List of observed field types.
             */
            const observedTypes = [
              ...new Set(
                uniqueFields.flatMap(
                  field => field.types
                )
              )
            ];

            /*
             * Keep field_hints because the
             * evidence builder already expects it.
             */
            const fieldHints = {
              rating:
                observedTypes.includes(
                  'rating'
                ),

              review_text:
                observedTypes.includes(
                  'review_text'
                ),

              reviewer_name:
                observedTypes.includes(
                  'reviewer_name'
                ),

              verified_purchase:
                observedTypes.includes(
                  'verified_purchase'
                ),

              date:
                observedTypes.includes(
                  'date'
                ),

              helpful:
                observedTypes.includes(
                  'helpful'
                )
            };

            output.push({

              tag:
                el.tagName.toLowerCase(),

              selector,

              text:
                text.slice(0, 1200),

              score,

              field_candidates:
                uniqueFields.slice(0, 30),

              observed_field_types:
                observedTypes,

              field_hints:
                fieldHints
            });
          }

          /*
           * Remove duplicate review containers.
           */
          const unique = [];
          const seen = new Set();

          for (
            const item of output
          ) {

            const key =
              item.selector +
              '|' +
              item.text.slice(0, 250);

            if (seen.has(key))
              continue;

            seen.add(key);
            unique.push(item);
          }

          /*
           * Prefer stronger candidates.
           */
          unique.sort((a, b) => {

            if (b.score !== a.score) {
              return b.score - a.score;
            }

            return (
              b.observed_field_types.length -
              a.observed_field_types.length
            );
          });

          return unique.slice(0, 100);
        }
        """
      )
        return raw or []

    def build_best_field_schema(self):
        """
        Build a compact, evidence-backed field schema from raw DOM candidates.

        Raw field_candidates remain available for debugging. This method
        selects the strongest observed candidate for each field type and
        normalizes the DOM inspector's {types, text} representation into
        {field_type, selector, example}.
        """

        field_types = [
            "review_text",
            "reviewer_name",
            "rating",
            "date",
            "verified_purchase",
            "helpful",
        ]

        best = {}

        def selector_depth(selector):
            if not selector:
                return 999
            return selector.count(">") + 1

        def text_quality(text):
            if not text:
                return 0

            value = " ".join(str(text).split())
            if not value:
                return 0

            score = 0
            if 2 <= len(value) <= 500:
                score += 5
            if len(value) <= 100:
                score += 2
            if len(value) > 800:
                score -= 8
            return score

        def looks_like_location_metadata(text):
            """Generic rejection for location-only fragments."""
            if not text:
                return False

            value = " ".join(str(text).split()).strip()
            return value.startswith(",") and len(value) < 100

        def score_candidate(candidate, field_type):
            selector = candidate.get("selector", "")
            example = (
                candidate.get("example")
                or candidate.get("text")
                or ""
            )

            depth = selector_depth(selector)
            value = " ".join(str(example).split()).strip()
            lower = value.lower()

            score = max(0, 12 - depth)
            score += text_quality(value)

            if looks_like_location_metadata(value):
                score -= 20

            if selector:
                last_part = selector.split(">")[-1].strip()
                if last_part not in ("div", "span", "p"):
                    score += 3

            if field_type == "rating":
                if re.search(r"\d", value):
                    score += 5

            elif field_type == "date":
                if any(
                    token in lower
                    for token in (
                        "today", "yesterday", "day", "days",
                        "week", "weeks", "month", "months",
                        "year", "years", "ago",
                    )
                ):
                    score += 5

            elif field_type == "verified_purchase":
                if "verified" in lower:
                    score += 5

            elif field_type == "helpful":
                if "helpful" in lower:
                    score += 5

            elif field_type == "review_text":
                if len(value) >= 20:
                    score += 5
                if value.startswith(","):
                    score -= 20
                if re.fullmatch(r"[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,5}", value):
                    score -= 18

            elif field_type == "reviewer_name":
                # Reviewer names require POSITIVE semantic/structural evidence.
                # A generic deep CSS selector plus name-like text is not enough,
                # because review text such as "Pretty good" can look name-like.
                attrs = candidate.get("semantic_attrs") or {}
                extraction = candidate.get("extraction", "")
                semantic = " ".join(
                    str(attrs.get(key, ""))
                    for key in (
                        "aria_label", "itemprop", "data_testid",
                        "class_name", "id", "title"
                    )
                ).lower()

                strong_signals = (
                    r"\b(?:author|reviewer|reviewer[-_ ]?name|username|user[-_ ]?name|"
                    r"profile[-_ ]?name|customer[-_ ]?name|display[-_ ]?name)\b"
                )
                author_aria = re.search(
                    r"\b(?:reviewed|review|written|posted)\s+by\b|\b(?:author|reviewer|user|customer)\b",
                    str(attrs.get("aria_label", "")).lower()
                )
                itemprop_author = re.search(
                    r"\b(?:author|name)\b",
                    str(attrs.get("itemprop", "")).lower()
                )

                semantic_match = re.search(strong_signals, semantic)

                if semantic_match:
                    score += 14
                if author_aria:
                    score += 12
                if itemprop_author:
                    score += 12

                # Generic selectors are weak evidence and must not win alone.
                if extraction == "first_name_segment":
                    score += 18
                elif not (semantic_match or author_aria or itemprop_author):
                    score -= 30

                if 2 <= len(value) <= 80:
                    score += 2
                if "," in value or value.startswith(","):
                    score -= 25
                if re.search(r"\bverified\s+purchase\b|\bhelpful\b", lower):
                    score -= 15
                if candidate.get("has_meaningful_descendants"):
                    score -= 8

            return score

        # dom_reviews() stores field candidates as:
        #   {"types": [...], "selector": "...", "text": "..."}
        # Normalize them into one candidate list per field type.
        candidates_by_type = {
            field_type: []
            for field_type in field_types
        }

        for review in self.review_candidates:
            for field_candidate in review.get("field_candidates", []):
                types = field_candidate.get("types", [])

                # Backward compatibility with a possible single field_type.
                if not types and field_candidate.get("field_type"):
                    types = [field_candidate["field_type"]]

                for field_type in types:
                    if field_type not in candidates_by_type:
                        continue

                    candidates_by_type[field_type].append({
                        "field_type": field_type,
                        "selector": field_candidate.get("selector", ""),
                        "example": (
                            (field_candidate.get("extracted_text", {}) or {}).get(field_type)
                            or field_candidate.get("example")
                            or field_candidate.get("text")
                            or ""
                        ),
                        "extraction": (field_candidate.get("extraction", {}) or {}).get(field_type, ""),
                        "tag": field_candidate.get("tag", ""),
                        "has_meaningful_descendants": field_candidate.get("has_meaningful_descendants", False),
                        "semantic_attrs": field_candidate.get("semantic_attrs", {}),
                    })

        for field_type, candidates in candidates_by_type.items():
            if not candidates:
                continue

            ranked = sorted(
                candidates,
                key=lambda item: score_candidate(item, field_type),
                reverse=True,
            )

            selected = ranked[0]

            # Never publish a reviewer_name based only on text shape. If the
            # DOM provides no positive author/reviewer semantics, omit it.
            if field_type == "reviewer_name":
                attrs = selected.get("semantic_attrs") or {}
                semantic = " ".join(
                    str(attrs.get(key, ""))
                    for key in (
                        "aria_label", "itemprop", "data_testid",
                        "class_name", "id", "title"
                    )
                ).lower()
                has_author_signal = bool(re.search(
                    r"\b(?:author|reviewer|reviewer[-_ ]?name|username|user[-_ ]?name|"
                    r"profile[-_ ]?name|customer[-_ ]?name|display[-_ ]?name)\b",
                    semantic
                ))
                has_aria_signal = bool(re.search(
                    r"\b(?:reviewed|review|written|posted)\s+by\b|\b(?:author|reviewer|user|customer)\b",
                    str(attrs.get("aria_label", "")).lower()
                ))
                has_itemprop_signal = bool(re.search(
                    r"\b(?:author|name)\b",
                    str(attrs.get("itemprop", "")).lower()
                ))
                has_extraction_signal = selected.get("extraction") == "first_name_segment"

                if not (has_author_signal or has_aria_signal or has_itemprop_signal or has_extraction_signal):
                    continue

            best[field_type] = {
                "selector": selected.get("selector"),
                "example": selected.get("example"),
                "score": score_candidate(selected, field_type),
                "tag": selected.get("tag"),
                "extraction": selected.get("extraction", ""),
            }

        return best

    async def discover_pagination_controls(self, page: Page):
        raw = await self.safe_evaluate(
            page,
            """
            () => Array.from(
              document.querySelectorAll(
                'button, a, [role="button"], [aria-label], [title]'
              )
            ).map((el, index) => {
              const text = (el.innerText || el.textContent || '')
                .replace(/\\s+/g, ' ')
                .trim();

              const aria = el.getAttribute('aria-label') || '';
              const title = el.getAttribute('title') || '';

              const combined = (text + ' ' + aria + ' ' + title).toLowerCase();

              const r = el.getBoundingClientRect();

              return {
                index,
                tag: el.tagName.toLowerCase(),
                text: text.slice(0, 200),
                aria_label: aria.slice(0, 200),
                title: title.slice(0, 200),
                href: el.getAttribute('href') || '',
                disabled:
                  el.hasAttribute('disabled') ||
                  el.getAttribute('aria-disabled') === 'true',
                visible: r.width > 0 && r.height > 0,
                combined
              };
            })
            """
        )

        output = []

        for item in raw:
            if not item["visible"]:
                continue

            score = sum(
                2 for label in PAGINATION_LABELS
                if label in item["combined"]
            )

            if score >= 2:
                item["evidence_score"] = score
                item.pop("combined", None)
                output.append(item)

        self.pagination_candidates = output[:30]

    async def capture_state(
        self,
        page: Page,
        phase: str,
        round_no: int,
    ):
        candidates = await self.dom_reviews(page)
        self.review_candidates = candidates
        self.review_counts.append(len(candidates))

        return {
            "phase": phase,
            "round": round_no,
            "review_candidate_count": len(candidates),
            "representative_candidates": candidates[:8],
        }

    async def scroll_internal(
        self,
        page: Page,
        before: Dict[str, Any],
        container_index: int,
        round_no: int,
    ):
        result = await self.safe_evaluate(
            page,
            """
            ({index}) => {
              const all = Array.from(document.querySelectorAll('*'));

              const list = all.filter(el => {
                const r = el.getBoundingClientRect();
                return (
                  el.scrollHeight > el.clientHeight + 20 &&
                  el.clientHeight >= 120 &&
                  r.width >= 250 &&
                  r.height >= 120
                );
              }).sort((a,b) => b.scrollHeight - a.scrollHeight);

              const el = list[index];

              if (!el) return {error: 'container_missing'};

              const before = {
                scrollTop: el.scrollTop,
                scrollHeight: el.scrollHeight,
                clientHeight: el.clientHeight
              };

              el.scrollTop = Math.min(
                el.scrollHeight - el.clientHeight,
                el.scrollTop + 710
              );

              return {
                before,
                after: {
                  scrollTop: el.scrollTop,
                  scrollHeight: el.scrollHeight,
                  clientHeight: el.clientHeight
                }
              };
            }
            """,
            {"index": container_index},
        )

        await page.wait_for_timeout(1800)

        after = await self.capture_state(
            page,
            "after_scroll",
            round_no,
        )

        if result.get("error"):
            return result

        observation = {
            "round": round_no,
            "mode": "internal",
            "before_scroll": result["before"],
            "after_scroll": result["after"],
            "scroll_top_changed":
                result["before"]["scrollTop"]
                != result["after"]["scrollTop"],
            "scroll_height_changed":
                result["before"]["scrollHeight"]
                != result["after"]["scrollHeight"],
            "review_count_before":
                before["review_candidate_count"],
            "review_count_after":
                after["review_candidate_count"],
            "review_count_delta":
                after["review_candidate_count"]
                - before["review_candidate_count"],
            "representative_candidates":
                after["representative_candidates"][:5],
        }

        self.scroll_observations.append(observation)

        return observation

    async def scroll_page_fallback(
        self,
        page: Page,
        before: Dict[str, Any],
        round_no: int,
    ):
        result = await self.safe_evaluate(
            page,
            """
            () => {
              const before = {
                scrollY: window.scrollY,
                scrollHeight: document.documentElement.scrollHeight,
                innerHeight: window.innerHeight
              };

              window.scrollBy(
                0,
                Math.max(600, Math.floor(window.innerHeight * 0.8))
              );

              return before;
            }
            """
        )

        await page.wait_for_timeout(1800)

        after = await self.safe_evaluate(
            page,
            """
            () => ({
              scrollY: window.scrollY,
              scrollHeight: document.documentElement.scrollHeight,
              innerHeight: window.innerHeight
            })
            """
        )

        state = await self.capture_state(
            page,
            "after_page_scroll",
            round_no,
        )

        observation = {
            "round": round_no,
            "mode": "page",
            "before_scroll": result,
            "after_scroll": after,
            "scroll_top_changed":
                result["scrollY"] != after["scrollY"],
            "scroll_height_changed":
                result["scrollHeight"] != after["scrollHeight"],
            "review_count_before":
                before["review_candidate_count"],
            "review_count_after":
                state["review_candidate_count"],
            "review_count_delta":
                state["review_candidate_count"]
                - before["review_candidate_count"],
            "representative_candidates":
                state["representative_candidates"][:5],
        }

        self.scroll_observations.append(observation)
        return observation

    def strategy(self):
        internal = any(
            x["scroll_top_changed"]
            for x in self.scroll_observations
            if x.get("mode") == "internal"
        )

        growth = any(
            x["scroll_height_changed"]
            or x["review_count_delta"] > 0
            for x in self.scroll_observations
        )

        api = bool(self.review_api_evidence)

        api_pagination = False
        has_more_pages = False

        strong_pagination_terms = {
            "hasmore", "has_more", "nextpage", "next_page",
            "nextcursor", "next_cursor", "cursor",
            "loadmore", "load_more", "infinite",
        }

        for event in self.review_api_evidence:
            for field in event.get("pagination_fields", []):
                key_norm = norm(field.get("key", "")).replace(" ", "_")
                if key_norm in strong_pagination_terms or any(
                    term in key_norm for term in strong_pagination_terms
                ):
                    api_pagination = True

                    value = field.get("value", "")
                    if str(value).lower() in {"true", "1", "yes"}:
                        has_more_pages = True

            if event.get("url_pagination", {}).get("detected"):
                api_pagination = True

        if api_pagination:
            typ = "api_pagination"
        elif api and internal:
            typ = "infinite_scroll_api"
        elif internal:
            typ = "infinite_scroll_dom"
        elif self.pagination_candidates:
            typ = "button_or_link_pagination"
        elif growth:
            typ = "dynamic_scroll_or_load_more"
        else:
            typ = "static_dom"

        return {
            "type": typ,
            "api_detected": api,
            "api_pagination_detected": api_pagination,
            "scroll_container_detected":
                bool(self.scroll_containers),
            "scroll_container_actually_scrolled":
                internal,
            "review_container_detected":
                bool(self.review_candidates)
                or bool(self.review_api_evidence),
            "content_growth_detected": growth,
            "pagination_control_candidates_detected":
                bool(self.pagination_candidates),
            "has_more_pages": has_more_pages,
        }

    async def run(self):
        GENERATED.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)

            page = await browser.new_page(
                viewport={"width": 1440, "height": 900}
            )

            page.on("request", self.request_handler)
            page.on("response", self.response_handler)

            print("[INSPECTOR] Opening:", self.url)

            try:
                await page.goto(
                    self.url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except Exception as exc:
                print("[INSPECTOR] Navigation warning:", exc)

            await page.wait_for_timeout(2500)
            await self.wait_for_stable_page(page)

            # The target may perform a client-side redirect after domcontentloaded.
            # Retry inspection if the execution context changes during that transition.
            await self.discover_scroll_containers(page)
            await self.discover_pagination_controls(page)

            initial = await self.capture_state(
                page, "initial", 0
            )

            print(
                "[INSPECTOR] Initial DOM reviews:",
                initial["review_candidate_count"],
            )
            print(
                "[INSPECTOR] Scroll containers:",
                len(self.scroll_containers),
            )
            print(
                "[INSPECTOR] Pagination candidates:",
                len(self.pagination_candidates),
            )

            # First try the largest internal scroll container.
            # If none exists, fall back to normal page scrolling.
            if self.scroll_containers:
                previous_height = 0
                stagnant = 0

                for round_no in range(1, MAX_SCROLL_ROUNDS + 1):
                    before = await self.capture_state(
                        page,
                        "before_scroll",
                        round_no,
                    )

                    obs = await self.scroll_internal(
                        page,
                        before,
                        0,
                        round_no,
                    )

                    print(
                        f"[INSPECTOR] Internal scroll {round_no}: "
                        f"top "
                        f"{obs['before_scroll']['scrollTop']} -> "
                        f"{obs['after_scroll']['scrollTop']}; "
                        f"height "
                        f"{obs['before_scroll']['scrollHeight']} -> "
                        f"{obs['after_scroll']['scrollHeight']}; "
                        f"DOM reviews "
                        f"{obs['review_count_before']} -> "
                        f"{obs['review_count_after']}"
                    )

                    height = obs["after_scroll"]["scrollHeight"]

                    if (
                        height == previous_height
                        and obs["review_count_delta"] <= 0
                    ):
                        stagnant += 1
                    else:
                        stagnant = 0

                    previous_height = height

                    max_top = max(
                        0,
                        height - obs["after_scroll"]["clientHeight"],
                    )

                    if (
                        obs["after_scroll"]["scrollTop"]
                        >= max_top - 5
                        and stagnant >= 2
                    ):
                        break

            else:
                print(
                    "[INSPECTOR] No internal scroll container; "
                    "using generic page-scroll fallback"
                )

                previous_height = 0
                stagnant = 0

                for round_no in range(1, 13):
                    before = await self.capture_state(
                        page,
                        "before_page_scroll",
                        round_no,
                    )

                    obs = await self.scroll_page_fallback(
                        page,
                        before,
                        round_no,
                    )

                    print(
                        f"[INSPECTOR] Page scroll {round_no}: "
                        f"Y "
                        f"{obs['before_scroll']['scrollY']} -> "
                        f"{obs['after_scroll']['scrollY']}; "
                        f"height "
                        f"{obs['before_scroll']['scrollHeight']} -> "
                        f"{obs['after_scroll']['scrollHeight']}; "
                        f"DOM reviews "
                        f"{obs['review_count_before']} -> "
                        f"{obs['review_count_after']}"
                    )

                    height = obs["after_scroll"]["scrollHeight"]

                    if (
                        height == previous_height
                        and obs["review_count_delta"] <= 0
                    ):
                        stagnant += 1
                    else:
                        stagnant = 0

                    previous_height = height

                    max_top = max(
                        0,
                        height - obs["after_scroll"]["innerHeight"],
                    )

                    if (
                        obs["after_scroll"]["scrollY"]
                        >= max_top - 5
                        and stagnant >= 2
                    ):
                        break

                    if (
                        not obs["scroll_top_changed"]
                        and not obs["scroll_height_changed"]
                        and obs["review_count_delta"] <= 0
                    ):
                        stagnant += 1

                    if stagnant >= 3:
                        break

            await self.discover_scroll_containers(page)
            await self.discover_pagination_controls(page)

            # Convert noisy raw DOM field candidates into a compact,
            # Build the generator-facing field schema only after all
            # scrolling/inspection is complete.
            self.best_field_schema = self.build_best_field_schema()

            strategy = self.strategy()

            # Only keep the most useful network evidence in the final contract.
            network = self.network_events[-MAX_NETWORK_EVENTS:]

            evidence = {
                "evidence_contract": {
                    "version": "5.1",
                    "source": "playwright_direct_inspection",
                    "site_specific_rules_used": False,
                    "observed_review_fields": sorted({
                        hint
                        for candidate in self.review_candidates
                        for hint, observed in candidate.get("field_hints", {}).items()
                        if observed
                    }),
                    "generator_rule": (
                        "Generate the scraper only from observed evidence. "
                        "Do not invent selectors, API endpoints, JSON paths, "
                        "pagination controls, or fields. When an observed field "
                        "contains combined metadata, apply only the recorded extraction "
                        "rule from best_fields."
                    ),
                },

                "url": self.url,
                "final_url": page.url,
                "title": await page.title(),

                "page_diagnostics": {
                    "body_text_preview": clean(
                        await page.locator("body").inner_text(),
                        2500,
                    ),
                    "body_html_length": await page.locator("body").evaluate(
                        "(el) => el.innerHTML.length"
                    ),
                    "document_scroll_height": await page.evaluate(
                        "() => document.documentElement.scrollHeight"
                    ),
                    "document_scroll_y": await page.evaluate(
                        "() => window.scrollY"
                    ),
                },

                "review_strategy": strategy,

                "review_candidates": self.review_candidates[:40],

                # Generator-facing field schema. Raw review_candidates above
                # remain available for diagnostics and debugging.
                "best_fields": self.best_field_schema,

                "review_count_observations":
                    self.review_counts,

                "scroll_containers":
                    self.scroll_containers,

                "scroll_observations":
                    self.scroll_observations,

                "pagination_evidence":
                    self.pagination_candidates,

                "dynamic_network":
                    network,

                "review_api_evidence":
                    self.review_api_evidence[:MAX_REVIEW_API],

                "review_api_schema_evidence": [
                    {
                        "method": e["method"],
                        "url": e["url"],
                        "status": e["status"],
                        "review_arrays": e["review_arrays"],
                        "pagination_fields": e[
                            "pagination_fields"
                        ],
                        "url_pagination": e[
                            "url_pagination"
                        ],
                    }
                    for e in self.review_api_evidence[
                        :MAX_REVIEW_API
                    ]
                ],

                "evidence_summary": {
                    "dom_review_candidates":
                        len(self.review_candidates),
                    "network_events":
                        len(network),
                    "review_api_responses":
                        len(self.review_api_evidence),
                    "review_arrays_observed":
                        sum(
                            len(e["review_arrays"])
                            for e in self.review_api_evidence
                        ),
                    "pagination_fields_observed":
                        sum(
                            len(e["pagination_fields"])
                            for e in self.review_api_evidence
                        ),
                    "scroll_rounds":
                        len(self.scroll_observations),
                },
            }

            with EVIDENCE_FILE.open(
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(
                    evidence,
                    f,
                    indent=2,
                    ensure_ascii=False,
                )

            print()
            print("[INSPECTOR] ===== RESULT =====")
            print(
                "[INSPECTOR] DOM review candidates:",
                len(self.review_candidates),
            )
            print(
                "[INSPECTOR] Review API responses:",
                len(self.review_api_evidence),
            )
            print(
                "[INSPECTOR] Review arrays:",
                sum(
                    len(e["review_arrays"])
                    for e in self.review_api_evidence
                ),
            )
            print(
                "[INSPECTOR] Pagination fields:",
                sum(
                    len(e["pagination_fields"])
                    for e in self.review_api_evidence
                ),
            )
            print(
                "[INSPECTOR] Strategy:",
                strategy["type"],
            )
            print(
                "[INSPECTOR] API pagination:",
                strategy["api_pagination_detected"],
            )
            print(
                "[INSPECTOR] Best fields:",
                list(self.best_field_schema.keys()),
            )
            print(
                "[INSPECTOR] Evidence:",
                EVIDENCE_FILE,
            )

            await browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "url",
        help="Unknown review page URL",
    )
    args = parser.parse_args()

    asyncio.run(
        Inspector(args.url).run()
    )


if __name__ == "__main__":
    main()
