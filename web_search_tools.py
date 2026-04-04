"""Lightweight web and image search helpers for the Telegram bot."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from google.genai import types

from gemini_pool import GeminiClientPool


LOGGER = logging.getLogger(__name__)
DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0"}
GOOGLE_WEB_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "snippet": {"type": "string"},
                },
                "required": ["title", "url", "snippet"],
            },
        }
    },
    "required": ["results"],
}


@dataclass(slots=True)
class WebSearchResult:
    """One compact web-search result."""

    title: str
    url: str
    snippet: str


@dataclass(slots=True)
class ImageSearchResult:
    """One image-search result."""

    image_url: str
    thumbnail_url: str
    source_url: str
    title: str


def _clean_text(value: object, *, limit: int) -> str:
    """Normalize whitespace and cap the final display length."""
    cleaned = " ".join(str(value or "").strip().split())
    if len(cleaned) <= limit:
        return cleaned
    truncated = cleaned[: limit - 1].rsplit(" ", 1)[0].strip()
    return f"{truncated or cleaned[: limit - 1].strip()}…"


def _normalize_url(url: object) -> str:
    """Return a safe direct HTTP(S) URL or an empty string."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return parsed._replace(fragment="").geturl()


def _host_title(url: str) -> str:
    """Build a fallback title from the result host."""
    host = urlparse(url).netloc.removeprefix("www.").strip()
    return host or "Result"


def _parse_json_payload(response: object) -> dict[str, Any] | None:
    """Extract a JSON object from a Gemini response."""
    payload = getattr(response, "parsed", None)
    if isinstance(payload, dict):
        return payload

    raw_text = (getattr(response, "text", "") or "").strip()
    if not raw_text:
        return None
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class GoogleWebSearchService:
    """Use Gemini Google grounding to build compact search-style results."""

    _DDG_RESULT_PATTERN = re.compile(
        r'<div class="result results_links.*?'
        r'<a rel="nofollow" class="result__a" href="(?P<href>[^"]+)".*?>(?P<title>.*?)</a>'
        r'(?:.*?<a class="result__snippet"[^>]*>(?P<snippet>.*?)</a>)?',
        flags=re.S | re.I,
    )
    _HTML_TAG_PATTERN = re.compile(r"<[^>]+>")

    def __init__(
        self,
        *,
        pool: GeminiClientPool,
        model_names: list[str],
    ) -> None:
        self._pool = pool
        self._model_names = [name for name in model_names if str(name or "").strip()]
        self._resolved_grounding_urls: dict[str, str] = {}

    async def search(self, query: str, *, limit: int = 18) -> list[WebSearchResult]:
        """Return grounded search results for the requested query."""
        cleaned_query = " ".join(query.strip().split())
        if not cleaned_query:
            return []

        prompt = (
            "Use Google Search grounding.\n"
            "Search the user's exact query and return ONLY JSON matching the schema.\n"
            "Do not answer the query.\n"
            f"Return at most {limit} results.\n"
            "Requirements for each result:\n"
            "- title: the page title, not a rewritten answer.\n"
            "- url: the direct destination URL, not a Google redirect.\n"
            "- snippet: a short Google-style snippet, plain text, max 220 characters.\n"
            "- Never invent a URL. If a direct URL is not available, omit that result.\n\n"
            f"User query: {cleaned_query}"
        )

        response: object | None = None
        results: list[WebSearchResult] = []
        try:
            response = await self._pool.generate_content(
                model_names=self._model_names,
                contents=prompt,
                system_instruction=(
                    "You format search engine results into structured JSON. "
                    "Be precise, compact, and do not add commentary."
                ),
                tools=[types.Tool(google_search=types.GoogleSearch())],
                response_mime_type="application/json",
                response_schema=GOOGLE_WEB_SEARCH_SCHEMA,
            )
            results = self._parse_results_from_payload(_parse_json_payload(response), limit=limit)
        except Exception:
            LOGGER.info("Gemini grounded search failed; using HTML fallback.")

        if response is not None and len(results) < limit:
            fallback_results = await self._build_grounding_fallback_results(response, limit=limit)
            seen_urls = {item.url for item in results}
            for item in fallback_results:
                if item.url in seen_urls:
                    continue
                results.append(item)
                seen_urls.add(item.url)
                if len(results) >= limit:
                    break

        if len(results) >= limit:
            return results[:limit]

        fallback_results = await self._search_html_fallback(cleaned_query, limit=limit)
        seen_urls = {item.url for item in results}
        for item in fallback_results:
            if item.url in seen_urls:
                continue
            results.append(item)
            seen_urls.add(item.url)
            if len(results) >= limit:
                break
        return results[:limit]

    def _parse_results_from_payload(
        self,
        payload: dict[str, Any] | None,
        *,
        limit: int,
    ) -> list[WebSearchResult]:
        """Normalize parsed Gemini JSON into result objects."""
        if not isinstance(payload, dict):
            return []

        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            return []

        results: list[WebSearchResult] = []
        seen_urls: set[str] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            url = _normalize_url(item.get("url"))
            if not url or url in seen_urls:
                continue
            title = _clean_text(item.get("title"), limit=140) or _host_title(url)
            snippet = _clean_text(item.get("snippet"), limit=220)
            results.append(WebSearchResult(title=title, url=url, snippet=snippet))
            seen_urls.add(url)
            if len(results) >= limit:
                break
        return results

    async def _build_grounding_fallback_results(
        self,
        response: object,
        *,
        limit: int,
    ) -> list[WebSearchResult]:
        """Fallback to raw grounding chunks if structured parsing is sparse."""
        fallback_items: list[WebSearchResult] = []
        seen_urls: set[str] = set()
        for raw_title, raw_url in self._extract_grounding_sources(response):
            resolved_url = await self._resolve_grounding_url(raw_url)
            url = _normalize_url(resolved_url)
            if not url or url in seen_urls:
                continue
            title = _clean_text(raw_title, limit=140) or _host_title(url)
            fallback_items.append(WebSearchResult(title=title, url=url, snippet=""))
            seen_urls.add(url)
            if len(fallback_items) >= limit:
                break
        return fallback_items

    @staticmethod
    def _extract_grounding_sources(response: object) -> list[tuple[str, str]]:
        """Return basic title/URL pairs from Gemini grounding metadata."""
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return []
        grounding_metadata = getattr(candidates[0], "grounding_metadata", None)
        chunks = getattr(grounding_metadata, "grounding_chunks", None) or []
        results: list[tuple[str, str]] = []
        for chunk in chunks:
            web = getattr(chunk, "web", None)
            uri = _normalize_url(getattr(web, "uri", ""))
            if not uri:
                continue
            title = _clean_text(getattr(web, "title", ""), limit=140)
            results.append((title, uri))
        return results

    async def _resolve_grounding_url(self, uri: str) -> str:
        """Resolve Gemini redirect URLs when grounding returns an indirection host."""
        cached = self._resolved_grounding_urls.get(uri)
        if cached:
            return cached

        host = urlparse(uri).netloc.casefold()
        if host != "vertexaisearch.cloud.google.com":
            self._resolved_grounding_urls[uri] = uri
            return uri

        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=15.0,
                headers=DEFAULT_HEADERS,
            ) as client:
                response = await client.get(uri)
            location = (response.headers.get("location") or "").strip()
            if location:
                resolved = str(httpx.URL(urljoin(uri, location)))
                self._resolved_grounding_urls[uri] = resolved
                return resolved
        except Exception:
            LOGGER.debug("Failed to resolve grounding redirect %s", uri, exc_info=True)

        self._resolved_grounding_urls[uri] = uri
        return uri

    async def _search_html_fallback(self, query: str, *, limit: int) -> list[WebSearchResult]:
        """Use DuckDuckGo HTML results as a quota-safe search fallback."""
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            headers=DEFAULT_HEADERS,
        ) as client:
            response = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
            )
            response.raise_for_status()

        results: list[WebSearchResult] = []
        seen_urls: set[str] = set()
        for match in self._DDG_RESULT_PATTERN.finditer(response.text):
            url = self._resolve_duckduckgo_result_url(match.group("href"))
            if not url or url in seen_urls:
                continue
            title = self._clean_html_text(match.group("title"), limit=140) or _host_title(url)
            snippet = self._clean_html_text(match.group("snippet"), limit=220)
            results.append(WebSearchResult(title=title, url=url, snippet=snippet))
            seen_urls.add(url)
            if len(results) >= limit:
                break
        return results

    def _resolve_duckduckgo_result_url(self, href: str) -> str:
        """Unwrap DuckDuckGo result links into their destination URL."""
        cleaned_href = html.unescape(str(href or "").strip())
        if cleaned_href.startswith("//"):
            cleaned_href = f"https:{cleaned_href}"
        parsed = urlparse(cleaned_href)
        if parsed.netloc.endswith("duckduckgo.com") and parsed.path == "/l/":
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            return _normalize_url(target)
        return _normalize_url(cleaned_href)

    def _clean_html_text(self, value: str, *, limit: int) -> str:
        """Strip tags from HTML search results."""
        without_tags = self._HTML_TAG_PATTERN.sub(" ", value or "")
        return _clean_text(html.unescape(without_tags), limit=limit)


class ImageSearchService:
    """Fetch direct image results that Telegram can turn into an album."""

    _VQD_PATTERN = re.compile(r"""vqd=("|')(?P<token>.+?)(?:\1)""")

    async def search(self, query: str, *, limit: int = 18) -> list[ImageSearchResult]:
        """Return image results for the query."""
        cleaned_query = " ".join(query.strip().split())
        if not cleaned_query:
            return []

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            headers=DEFAULT_HEADERS,
        ) as client:
            token_response = await client.get(
                "https://duckduckgo.com/",
                params={"q": cleaned_query, "iax": "images", "ia": "images"},
            )
            token_response.raise_for_status()
            token_match = self._VQD_PATTERN.search(token_response.text)
            if token_match is None:
                raise RuntimeError("Image search token was not found.")

            feed_response = await client.get(
                "https://duckduckgo.com/i.js",
                params={
                    "l": "us-en",
                    "o": "json",
                    "q": cleaned_query,
                    "vqd": token_match.group("token"),
                    "f": ",,,",
                    "p": "1",
                },
                headers={
                    **DEFAULT_HEADERS,
                    "Referer": "https://duckduckgo.com/",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            feed_response.raise_for_status()

        try:
            payload = feed_response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError("Image search feed could not be parsed.") from exc

        raw_results = payload.get("results", [])
        if not isinstance(raw_results, list):
            return []

        results: list[ImageSearchResult] = []
        seen_urls: set[str] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            image_url = _normalize_url(item.get("image"))
            thumbnail_url = _normalize_url(item.get("thumbnail"))
            if not image_url and not thumbnail_url:
                continue
            chosen_url = image_url or thumbnail_url
            if chosen_url in seen_urls:
                continue
            results.append(
                ImageSearchResult(
                    image_url=chosen_url,
                    thumbnail_url=thumbnail_url or chosen_url,
                    source_url=_normalize_url(item.get("url")),
                    title=_clean_text(item.get("title"), limit=140),
                )
            )
            seen_urls.add(chosen_url)
            if len(results) >= limit:
                break
        return results
