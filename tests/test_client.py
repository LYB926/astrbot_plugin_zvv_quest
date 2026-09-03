from __future__ import annotations

import asyncio

import httpx
from astrbot_plugin_zvv_quest.client import (
    IMAGE_BODY_LIMIT,
    ZvvQuestClient,
    ZvvQuestError,
    ZvvQuestRateLimitError,
)


def make_http(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://api.zvv.quest",
        transport=httpx.MockTransport(handler),
    )


async def no_sleep(_: float) -> None:
    return None


def test_search_sends_params_and_deduplicates_urls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.url.params["q"] == "我们的 网民"
        assert request.url.params["n"] == "3"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": [
                    "https://images.example/one.png",
                    "https://images.example/one.png",
                    "http://images.example/no.png",
                ],
            },
        )

    async def scenario() -> None:
        async with make_http(handler) as http:
            urls = await ZvvQuestClient(http, sleep=no_sleep).search("我们的 网民", 3)
        assert urls == ("https://images.example/one.png",)

    asyncio.run(scenario())


def test_search_retries_bad_gateway_once() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(502)
        return httpx.Response(200, json={"code": 200, "data": []})

    async def scenario() -> None:
        async with make_http(handler) as http:
            assert await ZvvQuestClient(http, sleep=no_sleep).search("test", 1) == ()

    asyncio.run(scenario())
    assert calls == 2


def test_search_handles_rate_limit_and_invalid_envelopes() -> None:
    async def rate_limited() -> None:
        async with make_http(lambda _: httpx.Response(429)) as http:
            try:
                await ZvvQuestClient(http, sleep=no_sleep).search("test", 1)
            except ZvvQuestRateLimitError:
                return
            raise AssertionError("expected rate-limit error")

    async def bad_envelope() -> None:
        async with make_http(lambda _: httpx.Response(200, content=b"not json")) as http:
            try:
                await ZvvQuestClient(http, sleep=no_sleep).search("test", 1)
            except ZvvQuestError:
                return
            raise AssertionError("expected service error")

    asyncio.run(rate_limited())
    asyncio.run(bad_envelope())


def test_downloads_valid_magic_despite_generic_mime_and_skips_invalid_data() -> None:
    png = b"\x89PNG\r\n\x1a\nvalid"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/one":
            return httpx.Response(
                200,
                content=png,
                headers={"content-type": "application/octet-stream"},
            )
        return httpx.Response(200, content=b"not an image")

    async def scenario() -> None:
        async with make_http(handler) as http:
            result = await ZvvQuestClient(http, sleep=no_sleep).download_images(
                ("https://images.example/one", "https://images.example/bad")
            )
        assert result == (png,)

    asyncio.run(scenario())


def test_download_rejects_oversized_response() -> None:
    oversized = b"\x89PNG\r\n\x1a\n" + b"x" * IMAGE_BODY_LIMIT

    async def scenario() -> None:
        async with make_http(lambda _: httpx.Response(200, content=oversized)) as http:
            result = await ZvvQuestClient(http, sleep=no_sleep).download_images(
                ("https://images.example/a",)
            )
        assert result == ()

    asyncio.run(scenario())


def test_search_rejects_plaintext_http_error_and_business_error() -> None:
    async def plaintext_error() -> None:
        async with make_http(lambda _: httpx.Response(400, text="bad query")) as http:
            try:
                await ZvvQuestClient(http, sleep=no_sleep).search("test", 1)
            except ZvvQuestError:
                return
            raise AssertionError("expected service error")

    async def business_error() -> None:
        response = httpx.Response(200, json={"code": 400, "data": None})
        async with make_http(lambda _: response) as http:
            try:
                await ZvvQuestClient(http, sleep=no_sleep).search("test", 1)
            except ZvvQuestError:
                return
            raise AssertionError("expected service error")

    asyncio.run(plaintext_error())
    asyncio.run(business_error())
