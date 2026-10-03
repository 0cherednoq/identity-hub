"""F2.1: общие сценарии устойчивости аренды для MemoryStore и PostgreSQL (R1-03, R1-12, R1-14, R1-21).

Сбой и задержка подставляются в транзакцию хранилища (`MemoryTx` или `PgTx`): на хранилище без
await-точек часть дефектов не проявляется, поэтому вызовы в сценариях всегда с задержкой.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from identity_hub import IdentityHub, LeaseLost, ProxyPolicy, SpaceHandle, ValidationFailed
from identity_hub.enums import LeaseEndReason
from identity_hub.errors import LeaseUnavailable
from identity_hub.pool import Pool
from identity_hub.read.events import EventFilters
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

SCALE = 0.5  # пропорции по умолчанию 60/20/20 в масштабе: TTL 1,5 с, heartbeat 0,5 с, запас 0,5 с


def tx_class(hub: IdentityHub) -> type:
    """Класс транзакции хранилища хаба: в него подставляются сбой и задержка."""
    from identity_hub.testing import MemoryStore

    if isinstance(hub._store, MemoryStore):
        from identity_hub.testing.memory import MemoryTx

        return MemoryTx
    from identity_hub.postgres.store import PgTx

    return PgTx


async def _pool(hub: IdentityHub, name: str, **options: Any) -> tuple[SpaceHandle, Pool, UUID]:
    space = hub.space(name, name, create=True)
    reg = await register(
        space, service="fake_tg", external_ref="+1", lifecycle="ready", session={"auth_key": "k"}
    )
    return space, space.pools.define("p", service="fake_tg", holder="w", **options), reg.identity_id


async def live_lease(space: SpaceHandle, identity_id: UUID) -> Any:
    async with space._tx(own=True) as tx:
        return await tx.current_lease(identity_id)


async def check_single_heartbeat_failure_keeps_lease(
    hub: IdentityHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-03: короткий сбой БД на одном heartbeat не отменяет аренду — попытка повторяется быстро."""
    _space, pool, _ = await _pool(
        hub,
        "hb-retry",
        ttl=timedelta(seconds=3 * SCALE),
        heartbeat=timedelta(seconds=1 * SCALE),
        safety_margin=timedelta(seconds=1 * SCALE),
    )
    cls = tx_class(hub)
    original = cls.extend_lease
    calls = 0

    async def flaky(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.005)
        if calls == 1:
            raise ConnectionError("simulated transient DB failure")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(cls, "extend_lease", flaky)

    async with pool.lease() as lease:
        await asyncio.sleep(4.5 * SCALE)  # дольше TTL: без продления аренда истекла бы
        assert not lease.lost
        lease.check()
    assert calls >= 3


async def check_heartbeat_gives_up_at_deadline(hub: IdentityHub, monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторы не продлевают самоограждение: при непрерывном сбое аренда теряется к дедлайну (G-1.2)."""
    _space, pool, _ = await _pool(
        hub,
        "hb-lost",
        ttl=timedelta(seconds=3 * SCALE),
        heartbeat=timedelta(seconds=1 * SCALE),
        safety_margin=timedelta(seconds=1 * SCALE),
    )
    cls = tx_class(hub)
    calls = 0

    async def broken(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.005)
        raise ConnectionError("database is down")

    monkeypatch.setattr(cls, "extend_lease", broken)
    loop = asyncio.get_running_loop()
    marks: dict[str, float] = {}

    with pytest.raises(LeaseLost):
        async with pool.lease(on_lost=lambda: marks.setdefault("lost", loop.time())):
            marks["entered"] = loop.time()
            await asyncio.sleep(10 * SCALE)

    assert calls >= 2  # после неудачи была хотя бы одна повторная попытка
    # Потеря признана до истечения TTL в БД. Отсчёт от входа в блок до on_lost: время захвата и
    # освобождения в БД в интервал не входит, иначе тест зависел бы от загрузки машины.
    assert marks["lost"] - marks["entered"] < 3 * SCALE


async def check_double_cancel_during_release_frees_lease(
    hub: IdentityHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-14: вторая отмена во время освобождения не оставляет аренду висеть до TTL."""
    space, pool, identity_id = await _pool(hub, "double-cancel")
    cls = tx_class(hub)
    original = cls.release_lease
    entered = asyncio.Event()

    async def slow(self: Any, *args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.sleep(0.3)  # round-trip до БД
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(cls, "release_lease", slow)
    started = asyncio.Event()

    async def worker() -> None:
        async with pool.lease():
            started.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(worker())
    await started.wait()
    task.cancel()
    await entered.wait()
    task.cancel()  # нетерпеливый супервизор отменяет ещё раз, пока идёт освобождение
    (result,) = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(result, asyncio.CancelledError)
    assert await live_lease(space, identity_id) is None
    async with pool.lease():  # аккаунт сразу доступен следующему воркеру
        pass


async def check_release_validates_reason_before_touching_lease(hub: IdentityHub) -> None:
    """R1-21: строка вне `LeaseEndReason` отклоняется до остановки heartbeat, аренда остаётся рабочей."""
    space, pool, identity_id = await _pool(hub, "reason")
    lease = await pool.acquire()

    with pytest.raises(ValidationFailed):
        await lease.release(reason="worker-shutdown")

    assert await live_lease(space, identity_id) is not None
    lease.check()  # аренда не помечена освобождённой
    assert await lease.session() is not None
    await lease.release("failed", error="shutdown")  # значение перечисления строкой допустимо
    assert await live_lease(space, identity_id) is None
    (history,) = await space.admin.lease_history(identity_id)
    assert history.end_reason == LeaseEndReason.FAILED


async def check_release_can_be_retried_after_failure(
    hub: IdentityHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-14: `_released` выставляется только после успешной записи — повтор доводит освобождение."""
    space, pool, identity_id = await _pool(hub, "release-retry")
    cls = tx_class(hub)
    original = cls.release_lease
    calls = 0

    async def flaky(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("simulated transient DB failure")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(cls, "release_lease", flaky)
    lease = await pool.acquire()
    async with lease.operation("read_channel"):
        pass

    await lease.release()  # сбой записи: предупреждение в лог, исключения нет
    assert await live_lease(space, identity_id) is not None
    with pytest.raises(LeaseLost):
        lease.check()  # работать под арендой после release() уже нельзя

    await lease.release()
    assert await live_lease(space, identity_id) is None
    (history,) = await space.admin.lease_history(identity_id)
    assert history.operations == {"read_channel": 1}  # счётчики не потеряны при неудачной попытке
    await lease.release()  # дальнейшие повторы — no-op
    assert calls == 2


PROXIED = replace(FAKE_TG, name="proxied_tg", proxy_policy=ProxyPolicy(group="empty-group"))


async def _exhausted_events(space: SpaceHandle) -> int:
    page = await space.admin.event_page(filters=EventFilters(type="proxy.pool.exhausted"))
    return len(page.items)


async def check_proxy_pool_exhausted_is_reported_once_per_acquire(hub: IdentityHub) -> None:
    """R1-12: ожидающий `acquire` пишет одно событие, а не по одному на каждую итерацию ожидания."""
    hub.register_service(PROXIED)
    await hub.start()
    space = hub.space("exhausted", "Exhausted", create=True)
    await register(
        space, service="proxied_tg", external_ref="+1", lifecycle="ready", session={"auth_key": "k"}
    )
    pool = space.pools.define("px", service="proxied_tg", holder="w")

    with pytest.raises(LeaseUnavailable):
        await pool.acquire(wait=1.0)
    assert await _exhausted_events(space) == 1

    for _ in range(3):  # соседние вызовы в ту же минуту событие не повторяют
        with pytest.raises(LeaseUnavailable):
            await pool.acquire()
    assert await _exhausted_events(space) == 1
