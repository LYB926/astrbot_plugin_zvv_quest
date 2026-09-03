"""AstrBot-facing command and lifecycle tests using small module stubs."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from dataclasses import dataclass

from astrbot_plugin_zvv_quest.client import ZvvQuestError, ZvvQuestRateLimitError


class DummyLogger:
    def warning(self, *args, **kwargs) -> None:
        pass

    def exception(self, *args, **kwargs) -> None:
        pass


class DummyFilter:
    @staticmethod
    def command(name):
        del name

        def decorate(function):
            return function

        return decorate


class DummyStar:
    def __init__(self, context) -> None:
        self.context = context


@dataclass
class DummyImage:
    data: bytes

    @classmethod
    def fromBytes(cls, data: bytes):
        return cls(data)


@dataclass
class DummyNode:
    content: list[DummyImage]
    name: str = ""


@dataclass
class DummyNodes:
    nodes: list[DummyNode]


class DummyEvent:
    def __init__(self, message: str, user_id: str = "user") -> None:
        self._message = message
        self._user_id = user_id

    def get_message_str(self) -> str:
        return self._message

    def get_sender_id(self) -> str:
        return self._user_id

    def plain_result(self, text: str):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)


class FakeClient:
    def __init__(
        self,
        *,
        urls=("https://images.example/a",),
        images=(b"image",),
        error=None,
    ) -> None:
        self.urls = urls
        self.images = images
        self.error = error
        self.search_calls: list[tuple[str, int]] = []

    async def search(self, query: str, count: int):
        self.search_calls.append((query, count))
        if self.error is not None:
            raise self.error
        return self.urls

    async def download_images(self, urls):
        assert tuple(urls) == self.urls
        return self.images


def install_astrbot_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    components = types.ModuleType("astrbot.api.message_components")
    star = types.ModuleType("astrbot.api.star")
    api.AstrBotConfig = dict
    api.logger = DummyLogger()
    event.AstrMessageEvent = object
    event.filter = DummyFilter()
    components.Image = DummyImage
    components.Node = DummyNode
    components.Nodes = DummyNodes
    star.Context = object
    star.Star = DummyStar
    star.register = lambda *args, **kwargs: lambda cls: cls
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.message_components": components,
            "astrbot.api.star": star,
        }
    )


def load_main():
    install_astrbot_stubs()
    sys.modules.pop("astrbot_plugin_zvv_quest.main", None)
    return importlib.import_module("astrbot_plugin_zvv_quest.main")


async def collect(generator) -> list[object]:
    return [item async for item in generator]


def test_lifecycle_creates_and_closes_shared_http_client() -> None:
    main = load_main()
    plugin = main.ZvvQuestPlugin(object(), {})

    async def scenario() -> None:
        await plugin.initialize()
        assert plugin._http is not None
        assert plugin._client is not None
        http = plugin._http
        await plugin.terminate()
        assert http.is_closed
        assert plugin._http is None
        assert plugin._client is None

    asyncio.run(scenario())


def test_single_image_uses_normal_chain() -> None:
    main = load_main()
    plugin = main.ZvvQuestPlugin(object(), {})
    plugin._client = FakeClient(images=(b"one",))

    async def scenario() -> None:
        result = await collect(plugin.search_zvv(DummyEvent("/zvv 我们的网民")))
        assert result == [("chain", [DummyImage(b"one")])]

    asyncio.run(scenario())


def test_multiple_images_use_nodes_with_image_only_content() -> None:
    main = load_main()
    plugin = main.ZvvQuestPlugin(object(), {})
    plugin._client = FakeClient(images=(b"one", b"two"))

    async def scenario() -> None:
        result = await collect(plugin.search_zvv(DummyEvent("/zvv 我们的网民 2")))
        nodes = result[0][1][0]
        assert isinstance(nodes, DummyNodes)
        assert [node.name for node in nodes.nodes] == ["ZVVQuest", "ZVVQuest"]
        assert [node.content for node in nodes.nodes] == [
            [DummyImage(b"one")],
            [DummyImage(b"two")],
        ]

    asyncio.run(scenario())


def test_invalid_count_does_not_call_client() -> None:
    main = load_main()
    plugin = main.ZvvQuestPlugin(object(), {})
    client = FakeClient()
    plugin._client = client

    async def scenario() -> None:
        result = await collect(plugin.search_zvv(DummyEvent("/zvv 测试 11")))
        assert result[0][0] == "plain"
        assert "/zvv" in result[0][1]
        assert client.search_calls == []

    asyncio.run(scenario())


def test_empty_and_image_failure_messages() -> None:
    main = load_main()

    async def scenario() -> None:
        empty = main.ZvvQuestPlugin(object(), {})
        empty._client = FakeClient(urls=())
        assert await collect(empty.search_zvv(DummyEvent("/zvv 测试", "empty"))) == [
            ("plain", main.EMPTY_RESULT_MESSAGE)
        ]

        failed = main.ZvvQuestPlugin(object(), {})
        failed._client = FakeClient(images=())
        assert await collect(failed.search_zvv(DummyEvent("/zvv 测试", "failed"))) == [
            ("plain", main.IMAGE_LOAD_FAILED_MESSAGE)
        ]

    asyncio.run(scenario())


def test_rate_limit_and_service_errors_have_stable_messages() -> None:
    main = load_main()

    async def scenario() -> None:
        rate = main.ZvvQuestPlugin(object(), {})
        rate._client = FakeClient(error=ZvvQuestRateLimitError("429"))
        assert await collect(rate.search_zvv(DummyEvent("/zvv 测试", "rate"))) == [
            ("plain", main.RATE_LIMIT_MESSAGE)
        ]

        unavailable = main.ZvvQuestPlugin(object(), {})
        unavailable._client = FakeClient(error=ZvvQuestError("down"))
        assert await collect(unavailable.search_zvv(DummyEvent("/zvv 测试", "down"))) == [
            ("plain", main.SERVICE_UNAVAILABLE_MESSAGE)
        ]

    asyncio.run(scenario())


def test_query_jobs_are_globally_limited_to_two() -> None:
    main = load_main()

    class ConcurrentClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(urls=())
            self.active = 0
            self.peak_active = 0

        async def search(self, query: str, count: int):
            del query, count
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return ()

    plugin = main.ZvvQuestPlugin(object(), {})
    client = ConcurrentClient()
    plugin._client = client

    async def scenario() -> None:
        await asyncio.gather(
            *(
                collect(plugin.search_zvv(DummyEvent("/zvv 测试", f"user-{index}")))
                for index in range(4)
            )
        )
        assert client.peak_active == 2

    asyncio.run(scenario())
