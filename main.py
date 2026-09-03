"""AstrBot command integration for ZVVQuest."""

from __future__ import annotations

import asyncio
import math

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Node, Nodes
from astrbot.api.star import Context, Star, register

from .client import API_BASE_URL, ZvvQuestClient, ZvvQuestError, ZvvQuestRateLimitError
from .logic import (
    COOLDOWN_SECONDS,
    CommandUsageError,
    CooldownTracker,
    extract_command_tail,
    normalize_count_settings,
    parse_search_request,
)

PLUGIN_REPO = "https://github.com/LYB926/astrbot_plugin_zvv_quest"
QUERY_CONCURRENCY = 2
COOLDOWN_MESSAGE = "操作太快，请在 %d 秒后再试。"
RATE_LIMIT_MESSAGE = "查询过于频繁，请稍后再试。"
SERVICE_UNAVAILABLE_MESSAGE = "ZVVQuest 服务暂时不可用，请稍后再试。"
EMPTY_RESULT_MESSAGE = "没有找到相关表情包。"
IMAGE_LOAD_FAILED_MESSAGE = "搜索成功，但图片加载失败，请稍后再试。"


@register(
    "astrbot_plugin_zvv_quest",
    "LYB926",
    "通过 /zvv 查询张维为表情包",
    "1.0.0",
    PLUGIN_REPO,
)
class ZvvQuestPlugin(Star):
    """Explicit `/zvv` command backed by the legacy public search endpoint."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._http: httpx.AsyncClient | None = None
        self._client: ZvvQuestClient | None = None
        self._cooldowns = CooldownTracker()
        self._query_semaphore = asyncio.Semaphore(QUERY_CONCURRENCY)
        self._default_count = 3
        self._max_count = 10

    async def initialize(self) -> None:
        """Validate configuration and create one shared HTTP client."""

        settings = normalize_count_settings(self.config, logger)
        self._default_count = settings.default_count
        self._max_count = settings.max_count
        self._http = httpx.AsyncClient(
            base_url=API_BASE_URL,
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        self._client = ZvvQuestClient(self._http, logger=logger)

    @filter.command("zvv")
    async def search_zvv(self, event: AstrMessageEvent):
        """Search ZVVQuest and return image bytes as one image or a forward message."""

        tail = extract_command_tail(event.get_message_str())
        try:
            request = parse_search_request(tail, self._default_count, self._max_count)
        except CommandUsageError as exc:
            yield event.plain_result(str(exc))
            return

        remaining = self._cooldowns.acquire(str(event.get_sender_id()), COOLDOWN_SECONDS)
        if remaining > 0:
            yield event.plain_result(COOLDOWN_MESSAGE % math.ceil(remaining))
            return

        try:
            async with self._query_semaphore:
                client = self._require_client()
                urls = await client.search(request.query, request.count)
                if not urls:
                    yield event.plain_result(EMPTY_RESULT_MESSAGE)
                    return
                images = await client.download_images(urls)
        except ZvvQuestRateLimitError:
            yield event.plain_result(RATE_LIMIT_MESSAGE)
            return
        except ZvvQuestError:
            logger.warning("ZVVQuest query failed", exc_info=True)
            yield event.plain_result(SERVICE_UNAVAILABLE_MESSAGE)
            return
        except Exception:
            logger.exception("Unexpected ZVVQuest plugin failure")
            yield event.plain_result(SERVICE_UNAVAILABLE_MESSAGE)
            return

        if not images:
            yield event.plain_result(IMAGE_LOAD_FAILED_MESSAGE)
        elif len(images) == 1:
            yield event.chain_result([Image.fromBytes(images[0])])
        else:
            nodes = [Node(name="ZVVQuest", content=[Image.fromBytes(image)]) for image in images]
            yield event.chain_result([Nodes(nodes)])

    async def terminate(self) -> None:
        """Close the shared HTTP client when the plugin reloads."""

        if self._http is not None:
            await self._http.aclose()
        self._http = None
        self._client = None

    def _require_client(self) -> ZvvQuestClient:
        if self._client is None:
            raise RuntimeError("ZVVQuest client has not been initialized")
        return self._client
