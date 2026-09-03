"""Pure command parsing, configuration and cooldown logic."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

MIN_COUNT = 1
MAX_CONFIG_COUNT = 50
DEFAULT_COUNT = 3
DEFAULT_MAX_COUNT = 10
COOLDOWN_SECONDS = 3.0
USAGE = "用法：/zvv <描述> [数量]（数量范围：1-%d）"


class WarningLogger(Protocol):
    def warning(self, message: str, *args: object) -> None: ...


class CommandUsageError(ValueError):
    """Raised when a `/zvv` command cannot be interpreted safely."""


@dataclass(frozen=True)
class CountSettings:
    """Validated effective count settings used for one plugin lifetime."""

    default_count: int
    max_count: int


@dataclass(frozen=True)
class SearchRequest:
    """Validated query and requested result count."""

    query: str
    count: int


def extract_command_tail(message: str, command: str = "zvv") -> str:
    """Return normalized text after either AstrBot or raw command text."""

    normalized = re.sub(r"\s+", " ", message).strip()
    match = re.match(rf"^/?{re.escape(command)}(?:\s+|$)", normalized)
    if match is None:
        return ""
    return normalized[match.end() :].strip()


def parse_search_request(tail: str, default_count: int, max_count: int) -> SearchRequest:
    """Parse `/zvv <description> [count]` without mistaking a lone number for count."""

    normalized = re.sub(r"\s+", " ", tail).strip()
    if not normalized:
        raise CommandUsageError(USAGE % max_count)

    tokens = normalized.split(" ")
    count = default_count
    if len(tokens) >= 2 and re.fullmatch(r"[+-]?\d+", tokens[-1]):
        count = int(tokens.pop())
        if not MIN_COUNT <= count <= max_count:
            raise CommandUsageError(USAGE % max_count)

    query = " ".join(tokens).strip()
    if not query:
        raise CommandUsageError(USAGE % max_count)
    return SearchRequest(query=query, count=count)


def normalize_count_settings(config: Mapping[str, Any], logger: WarningLogger) -> CountSettings:
    """Read defensive config values and return a coherent pair of effective limits."""

    max_count = _read_count(config, "max_count", DEFAULT_MAX_COUNT, logger)
    default_count = _read_count(config, "default_count", DEFAULT_COUNT, logger)
    if default_count > max_count:
        logger.warning(
            "default_count=%r exceeds max_count=%r; using max_count as effective default.",
            default_count,
            max_count,
        )
        default_count = max_count
    return CountSettings(default_count=default_count, max_count=max_count)


def _read_count(config: Mapping[str, Any], key: str, fallback: int, logger: WarningLogger) -> int:
    raw_value = config.get(key, fallback)
    if isinstance(raw_value, bool):
        logger.warning("Invalid %s=%r; falling back to %d.", key, raw_value, fallback)
        return fallback
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; falling back to %d.", key, raw_value, fallback)
        return fallback
    if isinstance(raw_value, float) and not raw_value.is_integer():
        logger.warning("Invalid %s=%r; falling back to %d.", key, raw_value, fallback)
        return fallback
    clamped = min(max(value, MIN_COUNT), MAX_CONFIG_COUNT)
    if clamped != value:
        logger.warning(
            "%s=%r is outside %d-%d; clamping to %d.",
            key,
            raw_value,
            MIN_COUNT,
            MAX_CONFIG_COUNT,
            clamped,
        )
    return clamped


class CooldownTracker:
    """In-memory per-user cooldown which records accepted attempts immediately."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last_used: dict[str, float] = {}

    def acquire(self, user_id: str, seconds: float = COOLDOWN_SECONDS) -> float:
        """Return remaining seconds, or record and allow this attempt."""

        now = self._clock()
        last_used = self._last_used.get(user_id)
        if last_used is not None:
            remaining = seconds - (now - last_used)
            if remaining > 0:
                return remaining
        self._last_used[user_id] = now
        return 0.0
