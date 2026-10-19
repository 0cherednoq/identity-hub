"""Диспетчер событий (ТЗ FR-13.5, 13.6, G-5.2–5.5).

Читает журнал ih_events по курсору каждого потребителя и раздаёт события: Python-колбэкам,
вебхукам (P8.3), уведомлениям (P8.4). Будится `LISTEN/NOTIFY`; без него — опрос с интервалом.
Доставка at-least-once: курсор продвигается только после успешной обработки.

Пакетные колбэки `subscribe_batch` получают пачку и при сбое делят её пополам до одного события.
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
from typing import TYPE_CHECKING, Literal

from identity_hub._space import logger
from identity_hub.errors import ConsumerCursorExpired, ValidationFailed, describe_error
from identity_hub.events import Event, EventDraft, matches_any
from identity_hub.metrics import record_metric
from identity_hub.store.base import NOTIFY_CHANNEL

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub, SpaceHandle

__all__ = ["BatchCallback", "BatchHandler", "Consumer", "Dispatcher", "EventCallback", "FailurePolicy"]

EventCallback = Callable[[Event], Awaitable[None]]
BatchCallback = Callable[[list[Event]], Awaitable[None]]
"""Обработчик `subscribe_batch`: получает пачку событий в порядке журнала."""
BatchHandler = Callable[[list[Event]], Awaitable[int | None]]
"""Обработчик пачки. Может вернуть число обработанных событий с начала пачки (`None` — все)."""

DEFAULT_MAX_ATTEMPTS = 5
FailurePolicy = Literal["skip", "block"]


def _attempt_limit(failure_policy: FailurePolicy, max_attempts: int | None) -> int | None:
    if failure_policy not in ("skip", "block"):
        raise ValidationFailed("failure_policy: skip или block")
    if failure_policy == "block":
        if max_attempts is not None:
            raise ValidationFailed("max_attempts нельзя задавать при failure_policy='block'")
        return None
    limit = DEFAULT_MAX_ATTEMPTS if max_attempts is None else max_attempts
    if limit < 1:
        raise ValueError("max_attempts должен быть положительным")
    return limit


def _log_blocked_attempt(attempt: int) -> bool:
    """Не раздувать лог постоянно застрявшего потребителя: 1, 2, 4, 8, ..."""
    return attempt & (attempt - 1) == 0


@dataclass
class Consumer:
    name: str  # имя курсора в ih_event_cursors
    handler: BatchHandler
    external_only: bool = True
    start_now: bool = False  # новый курсор встаёт на текущий горизонт журнала, а не в начало


@dataclass(frozen=True, slots=True)
class ConsumerRuntime:
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    retrying: bool = False
    stalled: bool = False
    failure_attempts: int = 0


def _start_now(start: str) -> bool:
    if start not in ("beginning", "now"):
        raise ValueError("start: beginning или now")
    return start == "now"


def _event_filter(
    types: Iterable[str] | None, space: SpaceHandle | str | None, service: str | None
) -> Callable[[Event], bool]:
    """Условие подписки: маски типов, пространство (хэндл или slug) и сервис."""
    patterns = frozenset(types) if types is not None else frozenset({"*"})
    slug = space if space is None or isinstance(space, str) else space.slug

    def wanted(event: Event) -> bool:
        return (
            matches_any(patterns, event.type)
            and (slug is None or event.space == slug)
            and (service is None or event.service == service)
        )

    return wanted


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

    def add_consumer(
        self, name: str, handler: BatchHandler, *, include_internal: bool = False, start_now: bool = False
    ) -> None:
        if name in self._consumers:
            raise ValueError(f"потребитель {name} уже зарегистрирован")
        self._consumers[name] = Consumer(name, handler, not include_internal, start_now)
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
        space: SpaceHandle | str | None = None,
        service: str | None = None,
        max_attempts: int | None = None,
        failure_policy: FailurePolicy = "skip",
        start: str = "beginning",
    ) -> None:
        """Python-колбэк на события после коммита (FR-13.5); `types` — типы или маски (`pool.capacity.*`).

        `space` и `service` оставляют события одного пространства и одного сервиса. Событие, на
        котором колбэк бросил исключение, приходит снова на следующем круге; события до него не
        повторяются. После `max_attempts` неудач подряд оно пропускается: в журнал пишется
        `dispatcher.event.skipped`, подписчик получает следующие события. При
        `failure_policy="block"` сбойное событие повторяется без лимита и курсор не двигается;
        `max_attempts` в этом режиме задавать нельзя.

        `start="now"` — новый подписчик начинает с текущего момента, а не с начала хранимого
        журнала; у подписчика, который уже работал, параметр ничего не меняет.
        """
        attempt_limit = _attempt_limit(failure_policy, max_attempts)
        start_now = _start_now(start)
        wanted = _event_filter(types, space, service)
        consumer = f"callbacks:{name}"

        async def handle(events: list[Event]) -> int:
            self._stalled.discard(consumer)
            for index, event in enumerate(events):
                if not wanted(event):
                    continue
                try:
                    await callback(event)
                except Exception as exc:
                    failed_id, count = self._failures.get(consumer, (event.id, 0))
                    count = count + 1 if failed_id == event.id else 1
                    retry = failure_policy == "block"
                    if not retry:
                        assert attempt_limit is not None
                        retry = count < attempt_limit
                    if retry or not await self._skip(consumer, event, count, exc):
                        self._failures[consumer] = (event.id, count)
                        self._stalled.add(consumer)
                        self._record_failure_attempt(consumer, event, count)
                        if failure_policy == "skip" or _log_blocked_attempt(count):
                            logger.warning(
                                "подписчик %s не обработал событие %s (%s), попытка %d%s",
                                name,
                                event.event_id,
                                describe_error(exc),
                                count,
                                f" из {attempt_limit}" if attempt_limit is not None else "",
                            )
                        return index  # курсор встаёт перед сбойным событием
                if self._failures.pop(consumer, None) is not None:
                    self._clear_failure_attempt(consumer, event)
            return len(events)

        self.add_consumer(consumer, handle, start_now=start_now)

    def subscribe_batch(
        self,
        name: str,
        handler: BatchCallback,
        *,
        types: Iterable[str] | None = None,
        space: SpaceHandle | str | None = None,
        service: str | None = None,
        max_attempts: int | None = None,
        failure_policy: FailurePolicy = "skip",
        start: str = "beginning",
    ) -> None:
        """Подписка, которая отдаёт события пачкой: `handler(events)` в порядке журнала.

        Фильтры — как у `subscribe`. Если обработчик бросил исключение, пачка делится пополам, пока
        не останется одно событие: события до сбойного доставляются и не повторяются, сбойное
        приходит снова на следующем круге и после `max_attempts` неудач пропускается с записью
        `dispatcher.event.skipped`. Доставка at-least-once: при сбое пачки её части приходят
        повторно, обработчик должен быть идемпотентным. При `failure_policy="block"` одиночное
        сбойное событие не пропускается, а остаётся перед курсором до успешной обработки.
        """
        attempt_limit = _attempt_limit(failure_policy, max_attempts)
        start_now = _start_now(start)
        wanted = _event_filter(types, space, service)
        consumer = f"callbacks:{name}"

        async def deliver(chunk: list[Event]) -> int:
            """Доставить события подряд; вернуть, сколько доставлено или пропущено с начала."""
            try:
                await handler(chunk)
            except Exception as exc:
                if len(chunk) > 1:
                    half = len(chunk) // 2
                    done = await deliver(chunk[:half])
                    return done if done < half else half + await deliver(chunk[half:])
                (event,) = chunk
                failed_id, count = self._failures.get(consumer, (event.id, 0))
                count = count + 1 if failed_id == event.id else 1
                retry = failure_policy == "block"
                if not retry:
                    assert attempt_limit is not None
                    retry = count < attempt_limit
                if retry or not await self._skip(consumer, event, count, exc):
                    self._failures[consumer] = (event.id, count)
                    self._stalled.add(consumer)
                    self._record_failure_attempt(consumer, event, count)
                    if failure_policy == "skip" or _log_blocked_attempt(count):
                        logger.warning(
                            "подписчик %s не обработал событие %s (%s), попытка %d%s",
                            name,
                            event.event_id,
                            describe_error(exc),
                            count,
                            f" из {attempt_limit}" if attempt_limit is not None else "",
                        )
                    return 0
                if self._failures.pop(consumer, None) is not None:
                    self._clear_failure_attempt(consumer, event)
                return 1
            if len(chunk) == 1 and self._failures.pop(consumer, None) is not None:
                self._clear_failure_attempt(consumer, chunk[0])
            return len(chunk)

        async def handle(events: list[Event]) -> int:
            self._stalled.discard(consumer)
            selected = [event for event in events if wanted(event)]
            if not selected:
                return len(events)
            done = await deliver(selected)
            if done == len(selected):
                return len(events)
            return events.index(selected[done])  # курсор встаёт перед сбойным событием

        self.add_consumer(consumer, handle, start_now=start_now)

    def _record_failure_attempt(self, consumer: str, event: Event, attempts: int) -> None:
        record_metric(
            self.hub.metrics,
            "gauge",
            "identity_hub_dispatcher_failure_attempts",
            attempts,
            consumer=consumer,
        )

    def _clear_failure_attempt(self, consumer: str, event: Event) -> None:
        record_metric(
            self.hub.metrics,
            "gauge",
            "identity_hub_dispatcher_failure_attempts",
            0,
            consumer=consumer,
        )

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
                    start_now=consumer.start_now,
                )
            except ConsumerCursorExpired:
                self._runtime[consumer.name] = replace(self._runtime[consumer.name], retrying=True)
                raise
            except Exception:
                self._runtime[consumer.name] = replace(self._runtime[consumer.name], retrying=True)
                logger.exception("потребитель %s упал; пачка будет повторена", consumer.name)
                continue
            if consumer.name in self._stalled:  # колбэк упал: событие придёт снова
                attempts = self._failures.get(consumer.name, (0, 0))[1]
                self._runtime[consumer.name] = replace(
                    self._runtime[consumer.name],
                    retrying=True,
                    stalled=True,
                    failure_attempts=attempts,
                )
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
