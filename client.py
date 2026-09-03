"""HTTP boundary for the legacy public ZVVQuest search API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

API_BASE_URL = "https://api.zvv.quest"
SEARCH_PATH = "/search"
API_CONNECT_TIMEOUT = 5.0
API_TOTAL_TIMEOUT = 15.0
IMAGE_CONNECT_TIMEOUT = 5.0
IMAGE_TOTAL_TIMEOUT = 20.0
API_BODY_LIMIT = 64 * 1024
IMAGE_BODY_LIMIT = 8 * 1024 * 1024
RETRY_DELAY_SECONDS = 0.5
RETRYABLE_STATUS_CODES = frozenset({502, 503, 504})
IMAGE_DOWNLOAD_CONCURRENCY = 3


class ZvvQuestError(RuntimeError):
    """A service, protocol or transport failure that is safe to hide from users."""


class ZvvQuestRateLimitError(ZvvQuestError):
    """The upstream search endpoint declined the query with HTTP 429."""


class _RetryableRequestError(Exception):
    pass


class ZvvQuestClient:
    """Search and download validated image bytes using one shared HTTP client."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: Any | None = None,
    ) -> None:
        self._http = http
        self._sleep = sleep
        self._logger = logger
        self._image_semaphore = asyncio.Semaphore(IMAGE_DOWNLOAD_CONCURRENCY)

    async def search(self, query: str, count: int) -> tuple[str, ...]:
        """Return deduplicated HTTPS result URLs, preserving API order."""

        async def operation() -> tuple[str, ...]:
            return await self._search_once(query, count)

        return await self._retry_search(operation)

    async def download_images(self, urls: Iterable[str]) -> tuple[bytes, ...]:
        """Download usable images concurrently while preserving URL order."""

        downloaded = await asyncio.gather(*(self._download_image(url) for url in urls))
        return tuple(data for data in downloaded if data is not None)

    async def _search_once(self, query: str, count: int) -> tuple[str, ...]:
        try:
            async with self._http.stream(
                "GET",
                SEARCH_PATH,
                params={"q": query, "n": count},
                timeout=httpx.Timeout(API_TOTAL_TIMEOUT, connect=API_CONNECT_TIMEOUT),
                follow_redirects=False,
            ) as response:
                if response.status_code == 429:
                    raise ZvvQuestRateLimitError("ZVVQuest search endpoint returned 429")
                if response.status_code in RETRYABLE_STATUS_CODES:
                    raise _RetryableRequestError(f"HTTP {response.status_code}")
                if not response.is_success:
                    raise ZvvQuestError(f"ZVVQuest search returned HTTP {response.status_code}")
                body = await _read_limited(response, API_BODY_LIMIT)
        except ZvvQuestRateLimitError:
            raise
        except _RetryableRequestError:
            raise
        except httpx.TimeoutException as exc:
            raise _RetryableRequestError("ZVVQuest search timed out") from exc
        except httpx.TransportError as exc:
            raise _RetryableRequestError("ZVVQuest search transport failure") from exc

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ZvvQuestError("ZVVQuest returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ZvvQuestError("ZVVQuest JSON envelope is not an object")
        if payload.get("code") != 200:
            raise ZvvQuestError("ZVVQuest returned an unsuccessful response envelope")
        raw_urls = payload.get("data")
        if not isinstance(raw_urls, list) or not all(isinstance(url, str) for url in raw_urls):
            raise ZvvQuestError("ZVVQuest response data is not a string list")
        return _deduplicate_https_urls(raw_urls)

    async def _retry_search(
        self, operation: Callable[[], Awaitable[tuple[str, ...]]]
    ) -> tuple[str, ...]:
        for attempt in range(2):
            try:
                return await operation()
            except _RetryableRequestError as exc:
                if attempt == 1:
                    raise ZvvQuestError("ZVVQuest search request failed") from exc
                await self._sleep(RETRY_DELAY_SECONDS)
        raise AssertionError("unreachable")

    async def _download_image(self, url: str) -> bytes | None:
        if not _is_valid_https_url(url):
            self._warning("Skipping invalid image URL from ZVVQuest: %r", url)
            return None
        async with self._image_semaphore:
            for attempt in range(2):
                try:
                    return await self._download_image_once(url)
                except _RetryableRequestError as exc:
                    if attempt == 1:
                        self._warning("Unable to download ZVVQuest image %r: %s", url, exc)
                        return None
                    await self._sleep(RETRY_DELAY_SECONDS)
                except ZvvQuestError as exc:
                    self._warning("Skipping invalid ZVVQuest image %r: %s", url, exc)
                    return None
        return None

    async def _download_image_once(self, url: str) -> bytes:
        try:
            async with self._http.stream(
                "GET",
                url,
                timeout=httpx.Timeout(IMAGE_TOTAL_TIMEOUT, connect=IMAGE_CONNECT_TIMEOUT),
                follow_redirects=False,
            ) as response:
                if response.status_code in RETRYABLE_STATUS_CODES:
                    raise _RetryableRequestError(f"HTTP {response.status_code}")
                if not response.is_success:
                    raise ZvvQuestError(f"HTTP {response.status_code}")
                data = await _read_limited(response, IMAGE_BODY_LIMIT)
        except _RetryableRequestError:
            raise
        except httpx.TimeoutException as exc:
            raise _RetryableRequestError("request timed out") from exc
        except httpx.TransportError as exc:
            raise _RetryableRequestError("transport failure") from exc
        if not _has_image_magic(data):
            raise ZvvQuestError("unrecognized image signature")
        return data

    def _warning(self, message: str, *args: object) -> None:
        if self._logger is not None:
            self._logger.warning(message, *args)


async def _read_limited(response: httpx.Response, limit: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > limit:
                raise ZvvQuestError(f"response exceeds {limit} byte limit")
        except ValueError:
            pass

    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            raise ZvvQuestError(f"response exceeds {limit} byte limit")
    return bytes(body)


def _deduplicate_https_urls(urls: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    accepted: list[str] = []
    for url in urls:
        if _is_valid_https_url(url) and url not in seen:
            seen.add(url)
            accepted.append(url)
    return tuple(accepted)


def _is_valid_https_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme.lower() == "https"
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _has_image_magic(data: bytes) -> bool:
    return (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or data.startswith((b"GIF87a", b"GIF89a"))
        or (len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )
