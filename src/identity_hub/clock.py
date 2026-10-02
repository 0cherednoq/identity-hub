"""Часы для ядра без БД.

В хранилище время — всегда `now()` базы данных (ТЗ §8.6); `Clock` нужен чистым машинам
состояний и юнит-тестам. Самоограждение аренды использует `monotonic()`.

Ожидание тоже идёт через часы, иначе под `FakeClock` дедлайны считались бы по управляемому
времени, а паузы — по реальному:

- `sleep(seconds)` — пауза вызывающего кода (ожидание свободного аккаунта, повтор команды);
- `idle(seconds)` — ожидание фоновой задачи (heartbeat аренды, опрос диспетчера).

У `SystemClock` оба метода — `asyncio.sleep`. `FakeClock.sleep` сам сдвигает время, а
`FakeClock.idle` ждёт, пока время сдвинет кто-то другой: `advance()` или чей-то `sleep()`.
"""

from __future__ import annotations

import asyncio
import heapq
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

__all__ = ["Clock", "FakeClock", "SystemClock"]


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None:
        """Пауза вызывающего кода."""
        ...

    async def idle(self, seconds: float) -> None:
        """Ожидание фоновой задачи: под управляемыми часами не двигает время само."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    async def idle(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


_SETTLE_TURNS = 10_000  # предел ходов цикла, пока разбуженные фоновые задачи доделывают шаг


class FakeClock:
    """Управляемые часы для тестов: `advance()` двигает и стеночное, и монотонное время.

    `advance()` синхронный: он будит фоновые задачи, ждущие в `idle()` (heartbeat аренд), но
    выполнятся они на следующем ходу цикла событий — дайте им ход (`await asyncio.sleep(0)`).
    `await clock.sleep(seconds)` делает это сам: проходит через каждое пробуждение по порядку
    и ждёт, пока разбуженные задачи закончат шаг, поэтому годится для долгих скачков внутри
    аренды.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._mono = 0.0
        self._sequence = 0
        self._waiters: list[tuple[float, int, asyncio.Future[None], asyncio.Task[Any] | None]] = []
        self._woken: set[asyncio.Task[Any]] = set()

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, delta: timedelta | float) -> None:
        seconds = delta.total_seconds() if isinstance(delta, timedelta) else float(delta)
        self._now += timedelta(seconds=seconds)
        self._mono += seconds
        self._wake_due()

    async def sleep(self, seconds: float) -> None:
        """Сдвинуть время на `seconds`, давая ход фоновым задачам в моменты их пробуждения."""
        target = self._mono + max(0.0, seconds)
        while True:
            due = self._next_wake()
            if due is None or due > target:
                break
            self.advance(max(0.0, due - self._mono))
            await self._settle()
        self.advance(max(0.0, target - self._mono))
        await self._settle()

    async def idle(self, seconds: float) -> None:
        """Ждать, пока управляемое время уйдёт вперёд на `seconds` (его двигает `advance`/`sleep`)."""
        task = asyncio.current_task()
        if task is not None:
            self._woken.discard(task)  # задача снова ждёт: её шаг после пробуждения закончен
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sequence += 1
        heapq.heappush(self._waiters, (self._mono + seconds, self._sequence, future, task))
        try:
            await future
        finally:
            if not future.done():
                future.cancel()

    def _next_wake(self) -> float | None:
        while self._waiters and self._waiters[0][2].done():  # отменённые ожидания
            heapq.heappop(self._waiters)
        return self._waiters[0][0] if self._waiters else None

    def _wake_due(self) -> None:
        while self._waiters and (self._waiters[0][0] <= self._mono or self._waiters[0][2].done()):
            _wake_at, _sequence, future, task = heapq.heappop(self._waiters)
            if future.done():
                continue
            future.set_result(None)
            if task is not None:
                self._woken.add(task)

    async def _settle(self) -> None:
        """Дать разбуженным задачам дойти до следующего `idle()` или завершиться."""
        await asyncio.sleep(0)
        for _ in range(_SETTLE_TURNS):
            self._woken = {task for task in self._woken if not task.done()}
            if not self._woken:
                return
            await asyncio.sleep(0)
        self._woken.clear()  # задача ушла в долгий ввод-вывод: дальше не ждём
