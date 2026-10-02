"""Диспетчер событий (ТЗ FR-13.5, 13.6, G-5.2–5.5).

Читает журнал ih_events по курсору каждого потребителя и раздаёт события: Python-колбэкам,
вебхукам (P8.3), уведомлениям (P8.4). Будится `LISTEN/NOTIFY`; без него — опрос с интервалом.
Доставка at-least-once: курсор продвигается только после успешной обработки.

Колбэки `subscribe` получают события по одному: курсор встаёт перед событием, на котором колбэк
упал, обработанные до него не повторяются. После `max_attempts` неудач событие пропускается с
журнальной записью `dispatcher.event.skipped` — одно «ядовитое» событие не останавливает
подписчика навсегда. Пакетные потребители (вебхуки, уведомления, rollups) и pump-ы изолированы
друг от друга: сбой одного не мешает остальным.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from identity_hub._space import logger
from identity_hub.errors import describe_error
from identity_hub.events import Event, EventDraft, matches_any
from identity_hub.metrics import record_metric
from identity_hub.store.base import NOTIFY_CHANNEL

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

__all__ = ["BatchHandler", "Consumer", "Dispatcher", "EventCallback"]

EventCallback = Callable[[Event], Awaitable[None]]
BatchHandler = Callable[[list[Event]], Awaitable[int | None]]
"""Обработчик пачки. Может вернуть число обработанных событий с начала пачки (`None` — все)."""

DEFAULT_MAX_ATTEMPTS = 5


@dataclass
class Consumer:
    name: str  # имя курсора в ih_event_cursors
    handler: BatchHandler
    external_only: bool = True


@dataclass(frozen=True, slots=True)
class ConsumerRuntime:
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    retrying: bool = False


class Dispatcher:
    def __init__(self, hub: IdentityHub, *, poll_interval: float = 2.0, batch_size: int = 100) -> None:
        self.hub = hub
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._consumers: dict[str, Consumer] = {}
        self._runtime: dict[str, ConsumerRuntime] = {}
        self._pumps: list[Callable[[], Awaitable[int]]] = []
        # потребитель → (id события, число неудач): счётчик живёт в процессе и сбрасывается
        # при успехе, пропуске или перезапуске
        self._failures: dict[str, tuple[int, int]] = {}
        self._stalled: set[str] = set()

    # --------------------------------------------------------- регистрация

    def add_consumer(self, name: str, handler: BatchHandler, *, include_internal: bool = False) -> None:
        if name in self._consumers:
            raise ValueError(f"потребитель {name} уже зарегистрирован")
        self._consumers[name] = Consumer(name, handler, not include_internal)
        self._runtime[name] = ConsumerRuntime()

    def add_pump(self, handler: Callable[[], Awaitable[int]]) -> None:
        """Добавить обработчик due-очереди, запускаемый даже без новых событий."""
        self._pumps.append(handler)

    def subscribe(
        self,
        name: str,
        callback: EventCallback,
        *,
        types: Iterable[str] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        """Python-колбэк на события после коммита (FR-13.5); `types` — типы или маски (`pool.capacity.*`).

        Событие, на котором колбэк бросил исключение, приходит снова на следующем круге; события
        до него не повторяются. После `max_attempts` неудач подряд оно пропускается: в журнал
        пишется `dispatcher.event.skipped`, подписчик получает следующие события.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts должен быть положительным")
        patterns = frozenset(types) if types is not None else frozenset({"*"})
        consumer = f"callbacks:{name}"

        async def handle(events: list[Event]) -> int:
            self._stalled.discard(consumer)
            for index, event in enumerate(events):
                if not matches_any(patterns, event.type):
                    continue
                try:
                    await callback(event)
                except Exception as exc:
                    failed_id, count = self._failures.get(consumer, (event.id, 0))
                    count = count + 1 if failed_id == event.id else 1
                    if count < max_attempts or not await self._skip(consumer, event, count, exc):
                        self._failures[consumer] = (event.id, count)
                        self._stalled.add(consumer)
                        logger.warning(
                            "подписчик %s не обработал событие %s (%s), попытка %d из %d",
                            name,
                            event.event_id,
                            describe_error(exc),
                            count,
                            max_attempts,
                        )
                        return index  # курсор встаёт перед сбойным событием
                self._failures.pop(consumer, None)
            return len(events)

        self.add_consumer(consumer, handle)

    async def _skip(self, consumer: str, event: Event, attempts: int, exc: Exception) -> bool:
        """Записать пропуск события; `False` — запись не удалась, событие остаётся на повтор."""
        space = self.hub.space(event.space)
        try:
            async with space._tx(own=True) as tx:
                await space._emit(
                    tx,
                    EventDraft(
                        "dispatcher.event.skipped",
                        actor="dispatcher",
                        service=event.service,
                        data={
                            "consumer": consumer,
                            "event_id": str(event.event_id),
                            "type": event.type,
                            "attempts": attempts,
                            "error": type(exc).__name__,  # только имя типа: текст бывает с секретами
                        },
                    ),
                )
        except Exception as failure:
            logger.warning(
                "не удалось записать пропуск события %s: %s", event.event_id, describe_error(failure)
            )
            return False
        logger.error(
            "подписчик %s: событие %s (%s) пропущено после %d попыток",
            consumer,
            event.event_id,
            event.type,
            attempts,
        )
        record_metric(
            self.hub.metrics,
            "counter",
            "identity_hub_failures_total",
            space=event.space,
            service=event.service or "",
            scope="subscriber",
            reason="skipped",
        )
        return True

    @property
    def consumers(self) -> tuple[str, ...]:
        return tuple(self._consumers)

    @property
    def runtime(self) -> dict[str, ConsumerRuntime]:
        return dict(self._runtime)

    # --------------------------------------------------------- работа

    async def run_once(self) -> int:
        """Одна пачка на каждого потребителя; возвращает число обработанных событий."""
        total = 0
        for consumer in list(self._consumers.values()):
            attempted_at = self.hub.clock.now()
            self._runtime[consumer.name] = replace(self._runtime[consumer.name], last_attempt_at=attempted_at)
            try:
                handled = await self.hub._store.process_events(
                    consumer.name,
                    consumer.handler,
                    limit=self.batch_size,
                    external_only=consumer.external_only,
                )
            except Exception:
                self._runtime[consumer.name] = replace(self._runtime[consumer.name], retrying=True)
                logger.exception("потребитель %s упал; пачка будет повторена", consumer.name)
                continue
            if consumer.name in self._stalled:  # колбэк упал: событие придёт снова
                self._runtime[consumer.name] = replace(self._runtime[consumer.name], retrying=True)
            else:
                self._runtime[consumer.name] = ConsumerRuntime(attempted_at, self.hub.clock.now(), False)
            total += handled or 0
        # pump-ы независимы и идут одновременно: медленная доставка вебхуков не задерживает
        # уведомления, импорт и обслуживание (R1-05)
        results = await asyncio.gather(*(pump() for pump in self._pumps), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error(
                    "обработчик due-очереди упал; элементы будут повторены: %s", describe_error(result)
                )
            elif isinstance(result, BaseException):
                raise result
            else:
                total += result
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
        async with self.hub._store.listen(NOTIFY_CHANNEL) as signal:
            if signal is None:
                logger.info("LISTEN недоступен — диспетчер работает в режиме опроса")
            while not stop.is_set():
                if signal is not None:
                    signal.clear()
                while await self.run_once() >= self.batch_size and not stop.is_set():
                    pass  # полная пачка — сразу следующая
                # пауза опроса идёт по часам хаба: под FakeClock её заканчивает advance()
                waiters = [
                    asyncio.ensure_future(stop.wait()),
                    asyncio.ensure_future(self.hub.clock.idle(self.poll_interval)),
                ]
                if signal is not None:
                    waiters.append(asyncio.ensure_future(signal.wait()))
                try:
                    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
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
