"""Диспетчер событий (ТЗ FR-13.5, 13.6, G-5.2–5.5).

Читает журнал ih_events по курсору каждого потребителя и раздаёт события: Python-колбэкам,
вебхукам (P8.3), уведомлениям (P8.4). Будится `LISTEN/NOTIFY`; без него — опрос с интервалом.
Доставка at-least-once: курсор продвигается только после успешной обработки пачки.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from identity_hub._space import logger
from identity_hub.events import Event, matches_any
from identity_hub.store.base import NOTIFY_CHANNEL

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

__all__ = ["BatchHandler", "Consumer", "Dispatcher", "EventCallback"]

EventCallback = Callable[[Event], Awaitable[None]]
BatchHandler = Callable[[list[Event]], Awaitable[None]]


@dataclass
class Consumer:
    name: str  # имя курсора в ih_event_cursors
    handler: BatchHandler


class Dispatcher:
    def __init__(self, hub: IdentityHub, *, poll_interval: float = 2.0, batch_size: int = 100) -> None:
        self.hub = hub
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._consumers: dict[str, Consumer] = {}

    # --------------------------------------------------------- регистрация

    def add_consumer(self, name: str, handler: BatchHandler) -> None:
        if name in self._consumers:
            raise ValueError(f"потребитель {name} уже зарегистрирован")
        self._consumers[name] = Consumer(name, handler)

    def subscribe(self, name: str, callback: EventCallback, *, types: Iterable[str] | None = None) -> None:
        """Python-колбэк на события после коммита (FR-13.5); `types` — типы или маски (`pool.capacity.*`)."""
        patterns = frozenset(types) if types is not None else frozenset({"*"})

        async def handle(events: list[Event]) -> None:
            for event in events:
                if matches_any(patterns, event.type):
                    await callback(event)

        self.add_consumer(f"callbacks:{name}", handle)

    @property
    def consumers(self) -> tuple[str, ...]:
        return tuple(self._consumers)

    # --------------------------------------------------------- работа

    async def run_once(self) -> int:
        """Одна пачка на каждого потребителя; возвращает число обработанных событий."""
        total = 0
        for consumer in list(self._consumers.values()):
            try:
                handled = await self.hub.store.process_events(
                    consumer.name, consumer.handler, limit=self.batch_size
                )
            except Exception:
                logger.exception("потребитель %s упал; пачка будет повторена", consumer.name)
                continue
            total += handled or 0
        return total

    async def drain(self, *, max_rounds: int = 1000) -> int:
        """Обработать всё, что уже видно ниже горизонта (для тестов и `identity-hub dispatcher --once`)."""
        total = 0
        for _ in range(max_rounds):
            handled = await self.run_once()
            total += handled
            if handled == 0:
                break
        return total

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Цикл диспетчера до `stop`. LISTEN будит сразу; без него — опрос `poll_interval` (FR-13.6)."""
        stop = stop or asyncio.Event()
        async with self.hub.store.listen(NOTIFY_CHANNEL) as signal:
            if signal is None:
                logger.info("LISTEN недоступен — диспетчер работает в режиме опроса")
            while not stop.is_set():
                if signal is not None:
                    signal.clear()
                while await self.run_once() >= self.batch_size and not stop.is_set():
                    pass  # полная пачка — сразу следующая
                waiters = [asyncio.ensure_future(stop.wait())]
                if signal is not None:
                    waiters.append(asyncio.ensure_future(signal.wait()))
                try:
                    await asyncio.wait(
                        waiters, timeout=self.poll_interval, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for waiter in waiters:
                        waiter.cancel()

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[Dispatcher]:
        """Встроить диспетчер в процесс приложения (D-005): `async with hub.dispatcher.running(): ...`."""
        stop = asyncio.Event()
        task = asyncio.create_task(self.run(stop), name="identity-hub-dispatcher")
        try:
            yield self
        finally:
            stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await task
