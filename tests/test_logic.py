from __future__ import annotations

import pytest
from astrbot_plugin_zvv_quest.logic import (
    CommandUsageError,
    CooldownTracker,
    extract_command_tail,
    normalize_count_settings,
    parse_search_request,
)


class Logger:
    def __init__(self) -> None:
        self.warnings: list[tuple[object, ...]] = []

    def warning(self, *args: object) -> None:
        self.warnings.append(args)


@pytest.mark.parametrize(
    "message",
    [
        "  /zvv   我们的   网民  5 ",
        "  zvv   我们的   网民  5 ",
    ],
)
def test_extracts_command_tail_from_raw_and_astrbot_messages(message: str) -> None:
    assert extract_command_tail(message) == "我们的 网民 5"


@pytest.mark.parametrize(
    ("tail", "query", "count"),
    [
        ("我们的 网民", "我们的 网民", 3),
        ("我们的 网民 5", "我们的 网民", 5),
        ("2025", "2025", 3),
        ("评价 +2", "评价", 2),
    ],
)
def test_parses_query_and_optional_count(tail: str, query: str, count: int) -> None:
    request = parse_search_request(tail, 3, 10)
    assert request.query == query
    assert request.count == count


@pytest.mark.parametrize("tail", ["", "评价 0", "评价 -1", "评价 11"])
def test_rejects_empty_or_out_of_range_count(tail: str) -> None:
    with pytest.raises(CommandUsageError, match="/zvv"):
        parse_search_request(tail, 3, 10)


def test_normalizes_invalid_and_conflicting_counts() -> None:
    logger = Logger()
    settings = normalize_count_settings({"default_count": 100, "max_count": "2"}, logger)
    assert settings.default_count == 2
    assert settings.max_count == 2
    assert logger.warnings


@pytest.mark.parametrize("value", [True, "bad", 1.5])
def test_rejects_non_integer_config_values(value: object) -> None:
    logger = Logger()
    settings = normalize_count_settings({"default_count": value, "max_count": 10}, logger)
    assert settings.default_count == 3
    assert logger.warnings


def test_cooldown_records_allowed_attempt_and_expires() -> None:
    now = [10.0]
    tracker = CooldownTracker(clock=lambda: now[0])
    assert tracker.acquire("user", 3) == 0
    now[0] = 11.2
    assert tracker.acquire("user", 3) == pytest.approx(1.8)
    now[0] = 13.0
    assert tracker.acquire("user", 3) == 0
