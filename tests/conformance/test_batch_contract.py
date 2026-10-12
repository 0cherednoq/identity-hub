"""C0.2: пакетный контракт `BatchResult` — точки сохранения, частичный успех и `atomic`."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from identity_hub import BatchRejection, BatchResult, NotFound, SpaceHandle, ValidationFailed
from identity_hub.api.batch import MAX_BATCH
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


async def _proxies(space: SpaceHandle, count: int = 3) -> list[UUID]:
    return [(await space.proxies.create(f"http://10.9.0.{n}:80", group="res")).id for n in range(count)]


async def _groups(space: SpaceHandle, ids: list[UUID]) -> list[str]:
    return [(await space.proxies.get(proxy_id)).group for proxy_id in ids]


async def _updated(space: SpaceHandle) -> list[Any]:
    return list((await space.admin.event_page(filters=EventFilters(type="proxy.updated"))).items)


def _mover(bad: UUID | None = None, error: Exception | None = None, calls: list[UUID] | None = None) -> Any:
    """Команда пачки: переносит прокси в группу `dc`, а на `bad` падает уже после записи."""

    async def move(handle: SpaceHandle, proxy_id: UUID) -> None:
        if calls is not None:
            calls.append(proxy_id)
        await handle.proxies.move(proxy_id, group="dc", actor="test")
        if proxy_id == bad:
            raise error or ValidationFailed("нельзя")

    return move


async def test_savepoint_rolls_back_rows_and_events(space: SpaceHandle) -> None:
    """C0.2: откат точки сохранения убирает и строки, и события; остальное в транзакции остаётся."""
    first, second = await _proxies(space, 2)
    async with space._tx() as tx:
        await tx.update_proxy(first, {"pool": "kept"})
        with pytest.raises(RuntimeError):
            async with tx.savepoint():
                await tx.update_proxy(second, {"pool": "lost"})
                raise RuntimeError("сбой")
        async with tx.savepoint():
            assert (await tx.get_proxy(second)).pool == "res"  # type: ignore[union-attr]
    assert await _groups(space, [first, second]) == ["kept", "res"]


async def test_partial_success_keeps_other_items(space: SpaceHandle) -> None:
    """C0.2: отказ одной сущности откатывает только её; события отклонённой не записаны."""
    ids = await _proxies(space)
    missing = uuid4()

    result = await space._run_batch([*ids, missing], _mover(bad=ids[1]))

    assert isinstance(result, BatchResult)
    assert sorted(result.applied) == sorted([ids[0], ids[2]])
    assert {r.key: r.code for r in result.rejected} == {ids[1]: "validation_error", missing: "not_found"}
    assert all(isinstance(r, BatchRejection) and r.message for r in result.rejected)
    assert await _groups(space, ids) == ["dc", "res", "dc"]
    assert sorted(e.proxy_id for e in await _updated(space)) == sorted([ids[0], ids[2]])


async def test_atomic_rolls_back_whole_batch(space: SpaceHandle) -> None:
    """C0.2: `atomic=True` — первый отказ отменяет всю пачку и пробрасывается как есть."""
    ids = await _proxies(space)

    with pytest.raises(ValidationFailed):
        await space._run_batch(ids, _mover(bad=sorted(ids)[-1]), atomic=True)

    assert await _groups(space, ids) == ["res", "res", "res"]
    assert await _updated(space) == []
    with pytest.raises(NotFound):
        await space._run_batch([uuid4()], _mover(), atomic=True)


async def test_foreign_error_rolls_back_everything(space: SpaceHandle) -> None:
    """C0.2: чужое исключение — не отказ сущности: пачка откатывается целиком."""
    ids = await _proxies(space)

    with pytest.raises(RuntimeError):
        await space._run_batch(ids, _mover(bad=sorted(ids)[-1], error=RuntimeError("сбой")))

    assert await _groups(space, ids) == ["res", "res", "res"]
    assert await _updated(space) == []


async def test_keys_are_deduplicated_and_sorted(space: SpaceHandle) -> None:
    """C0.2: повтор ключа выполняется один раз; порядок выполнения — по ключу (порядок блокировок)."""
    ids = await _proxies(space)
    calls: list[UUID] = []

    result = await space._run_batch([ids[2], ids[0], ids[2], ids[1], ids[0]], _mover(calls=calls))

    assert calls == sorted(ids)
    assert result.applied == sorted(ids) and result.rejected == []


async def test_batch_limit_rejects_without_changes(space: SpaceHandle) -> None:
    """C0.2: пачка больше потолка отклоняется до первой записи; пустая пачка — пустой результат."""
    calls: list[UUID] = []

    with pytest.raises(ValidationFailed):
        await space._run_batch([uuid4() for _ in range(MAX_BATCH + 1)], _mover(calls=calls))

    assert calls == []
    empty = await space._run_batch([], _mover(calls=calls))
    assert empty.applied == [] and empty.rejected == []
