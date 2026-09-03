"""Opt-in, read-only smoke test against the public legacy ZVVQuest endpoint."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from astrbot_plugin_zvv_quest.client import API_BASE_URL, ZvvQuestClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("ZVV_QUEST_LIVE_TEST") != "1",
        reason="set ZVV_QUEST_LIVE_TEST=1 to run live API checks",
    ),
]


def test_read_only_live_search_and_download() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url=API_BASE_URL,
            headers={"Accept": "application/json"},
        ) as http:
            client = ZvvQuestClient(http)
            urls = await client.search("我们的网民", 1)
            assert urls
            images = await client.download_images(urls[:1])
            assert images

    asyncio.run(scenario())
