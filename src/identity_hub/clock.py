"""Часы для ядра без БД.

В хранилище время — всегда `now()` базы данных (ТЗ §8.6); `Clock` нужен чистым машинам
состояний и юнит-тестам. Самоограждение аренды использует `monotonic()`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

__all__ = ["Clock", "FakeClock", "SystemClock"]


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Управляемые часы для тестов: `advance()` двигает и стеночное, и монотонное время."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, delta: timedelta | float) -> None:
        seconds = delta.total_seconds() if isinstance(delta, timedelta) else float(delta)
        self._now += timedelta(seconds=seconds)
        self._mono += seconds
