"""C2.1: пакетные команды жизненного цикла и удаление аккаунтов на `BatchResult`."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.fixtures.api import register

from identity_hub import (
    BatchResult,
    Lifecycle,
    NotFound,
    Override,
    SpaceHandle,
    Standing,
    ValidationFailed,
)
from identity_hub.api.batch import MAX_BATCH
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


async def _account(space: SpaceHandle, ref: str, **fields: Any) -> UUID:
    reg = await register(space, service="fake_tg", external_ref=ref, session={"auth_key": "k"}, **fields)
    identity_id: UUID = reg.identity_id
    return identity_id


async def _events(space: SpaceHandle, type_: str) -> list[Any]:
    return list((await space.admin.event_page(filters=EventFilters(type=type_))).items)


async def _lifecycles(space: SpaceHandle, ids: list[UUID]) -> list[Lifecycle]:
    return [(await space.identities.get(identity_id)).lifecycle for identity_id in ids]


async def test_activate_many_reports_each_account(space: SpaceHandle) -> None:
    """C2.1: отказ одного аккаунта (из `retired` без причины) не отменяет остальные."""
    fresh, other = await _account(space, "+1"), await _account(space, "+2")
    retired = await _account(space, "+3", lifecycle="ready")
    await space.identities.retire(retired, actor="op", reason="тест")
    missing = uuid4()
    before = {i: (await space.identities.get(i)).revision for i in (fresh, other, retired)}

    result = await space.identities.activate_many([fresh, other, retired, missing], actor="ops:ivan")

    assert isinstance(result, BatchResult)
    assert sorted(result.applied) == sorted([fresh, other])
    assert {r.key: r.code for r in result.rejected} == {retired: "invalid_transition", missing: "not_found"}
    assert await _lifecycles(space, [fresh, other, retired]) == [
        Lifecycle.READY,
        Lifecycle.READY,
        Lifecycle.RETIRED,
    ]
    after = {i: (await space.identities.get(i)).revision for i in (fresh, other, retired)}
    assert (after[fresh], after[other], after[retired]) == (
        before[fresh] + 1,
        before[other] + 1,
        before[retired],
    )
    changed = [e for e in await _events(space, "identity.lifecycle.changed") if e.data["to"] == "ready"]
    assert sorted(e.identity_id for e in changed) == sorted([fresh, other])
    assert {e.actor for e in changed} == {"ops:ivan"}

    again = await space.identities.activate_many([retired], actor="op", reason="вернули в работу")
    assert again.applied == [retired]


async def test_retire_many_atomic_changes_nothing_on_refusal(space: SpaceHandle) -> None:
    """C2.1: `atomic=True` — отказ по одному аккаунту отменяет всю пачку."""
    first, second = sorted([await _account(space, "+1", lifecycle="ready"), await _account(space, "+2")])
    missing = UUID(int=2**128 - 1)  # сортируется последним: первые два уже изменены, когда приходит отказ
    events = len(await _events(space, "identity.lifecycle.changed"))

    with pytest.raises(NotFound):
        await space.identities.retire_many([first, second, missing], actor="op", reason="тест", atomic=True)

    assert Lifecycle.RETIRED not in await _lifecycles(space, [first, second])
    assert len(await _events(space, "identity.lifecycle.changed")) == events

    loose = await space.identities.retire_many([first, second, missing], actor="op", reason="тест")
    assert loose.applied == [first, second] and [r.key for r in loose.rejected] == [missing]
    assert await _lifecycles(space, [first, second]) == [Lifecycle.RETIRED, Lifecycle.RETIRED]
    # повтор для уже выведенных — не отказ: команда идемпотентна, как одиночная `retire`
    again = await space.identities.retire_many([first, second], actor="op", reason="тест")
    assert again.applied == [first, second] and again.rejected == []


async def test_restore_standing_many(space: SpaceHandle) -> None:
    """C2.1: `banned → good` пачкой; аккаунт не в бане — отказ своей строки."""
    banned, good = await _account(space, "+1"), await _account(space, "+2")
    await space.identities.override(banned, Override("standing", "", "banned", reason="тест", actor="op"))

    result = await space.identities.restore_standing_many([banned, good], actor="op", reason="апелляция")

    assert result.applied == [banned] and [r.key for r in result.rejected] == [good]
    assert (await space.identities.get(banned)).standing == Standing.GOOD
    with pytest.raises(ValidationFailed):
        await space.identities.restore_standing_many([banned], actor="op", reason=" ")


async def test_delete_many_keeps_leased_account(space: SpaceHandle) -> None:
    """C2.1: аккаунт с живой арендой остаётся; `revoke=True` отзывает аренду и удаляет."""
    idle = await _account(space, "+1")
    busy = await _account(space, "+2", lifecycle="ready")
    pool = space.pools.define("p", service="fake_tg")
    lease = await pool.acquire()
    assert lease.identity_id == busy

    result = await space.identities.delete_many([idle, busy], actor="ops:ivan")

    assert result.applied == [idle]
    assert [(r.key, r.code) for r in result.rejected] == [(busy, "validation_error")]
    assert await space.identities.find(idle) is None and await space.identities.find(busy) is not None
    assert [e.identity_id for e in await _events(space, "identity.deleted")] == [idle]

    forced = await space.identities.delete_many([busy], actor="ops:ivan", revoke=True)
    assert forced.applied == [busy] and await space.identities.find(busy) is None


async def test_identity_batches_respect_limit(space: SpaceHandle) -> None:
    """C2.1: пачка больше потолка отклоняется без изменений; пустая — пустой результат."""
    ids = [uuid4() for _ in range(MAX_BATCH + 1)]

    with pytest.raises(ValidationFailed):
        await space.identities.activate_many(ids, actor="op")
    with pytest.raises(ValidationFailed):
        await space.identities.delete_many(ids, actor="op")
    empty = await space.identities.retire_many([], actor="op")
    assert empty.applied == [] and empty.rejected == []
