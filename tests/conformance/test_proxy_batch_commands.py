"""C1.2: пакетные удаление, смена статуса и отчёты о здоровье прокси."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.fixtures.api import register

from identity_hub import BatchResult, ProxyStatus, SpaceHandle, ValidationFailed
from identity_hub.api.batch import MAX_BATCH
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


async def _proxies(space: SpaceHandle, count: int = 3) -> list[UUID]:
    result = await space.proxies.create_many([f"http://10.11.0.{n}:80" for n in range(count)], group="res")
    return [item.id for item in result.items]


async def _events(space: SpaceHandle, type_: str) -> list[Any]:
    return list((await space.admin.event_page(filters=EventFilters(type=type_))).items)


async def _bind(space: SpaceHandle, proxy_id: UUID, ref: str) -> UUID:
    identity_id: UUID = (await register(space, service="fake_tg", external_ref=ref)).identity_id
    await space.proxies.rebind(identity_id, proxy_id, reason="тест", actor="test")
    return identity_id


async def test_delete_many_reports_each_proxy(space: SpaceHandle) -> None:
    """C1.2: прокси с привязкой без `unbind` — отказ своей строки; остальные удалены."""
    free, bound, other = await _proxies(space)
    await _bind(space, bound, "+1")
    missing = uuid4()

    result = await space.proxies.delete_many([free, bound, other, missing], actor="ops")

    assert sorted(result.applied) == sorted([free, other]) and result.unbound == 0
    assert {r.key: r.code for r in result.rejected} == {bound: "validation_error", missing: "not_found"}
    assert [p.id for p in (await space.proxies.list()).items] == [bound]
    assert sorted(e.data["proxy_id"] for e in await _events(space, "proxy.deleted")) == sorted(
        [str(free), str(other)]
    )


async def test_delete_many_unbinds_and_counts(space: SpaceHandle) -> None:
    """C1.2: `unbind=True` отвязывает аккаунты, событие отвязки — на каждый, счётчик общий."""
    first, second, _third = await _proxies(space)
    accounts = [
        await _bind(space, first, "+1"),
        await _bind(space, first, "+2"),
        await _bind(space, second, "+3"),
    ]

    result = await space.proxies.delete_many([first, second], actor="ops", unbind=True)

    assert sorted(result.applied) == sorted([first, second]) and result.unbound == 3
    changed = [
        e for e in await _events(space, "proxy.binding.changed") if e.data["reason"] == "proxy deleted"
    ]
    assert sorted(e.identity_id for e in changed) == sorted(accounts)


async def test_delete_many_atomic_keeps_everything(space: SpaceHandle) -> None:
    """C1.2: `atomic=True` — отказ одной строки отменяет удаление всех."""
    ids = await _proxies(space)
    await _bind(space, sorted(ids)[-1], "+1")

    with pytest.raises(ValidationFailed):
        await space.proxies.delete_many(ids, actor="ops", atomic=True)

    assert len((await space.proxies.list()).items) == 3
    assert await _events(space, "proxy.deleted") == []


async def test_override_many_sets_status_per_proxy(space: SpaceHandle) -> None:
    """C1.2: у каждого прокси свой статус; события те же, что у одиночной команды."""
    first, second, third = await _proxies(space)
    missing = uuid4()

    result = await space.proxies.override_many(
        {first: ProxyStatus.DEAD, second: "quarantined", third: "нет такого", missing: "dead"},
        reason="проверка пульта",
        actor="ops",
    )

    assert isinstance(result, BatchResult)
    assert sorted(result.applied) == sorted([first, second])
    assert {r.key: r.code for r in result.rejected} == {third: "validation_error", missing: "not_found"}
    statuses = {p.id: p.status for p in (await space.proxies.list()).items}
    assert statuses == {first: ProxyStatus.DEAD, second: ProxyStatus.QUARANTINED, third: ProxyStatus.ALIVE}
    changed = await _events(space, "proxy.status.changed")
    assert sorted(e.proxy_id for e in changed) == sorted([first, second])
    with pytest.raises(ValidationFailed):
        await space.proxies.override_many({first: "alive"}, reason=" ", actor="ops")


async def test_report_many_applies_health_facts(space: SpaceHandle) -> None:
    """C1.2: `True` — успех, `False` и вес — ошибка; неизвестный прокси в ответ не попадает."""
    good, bad, heavy = await _proxies(space)
    threshold = space.proxies.health.error_threshold
    await space.proxies.report_failure(good, weight=threshold / 2, actor="worker")

    statuses = await space.proxies.report_many(
        {good: True, bad: False, heavy: float(threshold), uuid4(): False}, actor="пульт"
    )

    assert statuses == {good: ProxyStatus.ALIVE, bad: ProxyStatus.ALIVE, heavy: ProxyStatus.QUARANTINED}
    scores = {p.id: p.error_score for p in (await space.proxies.list()).items}
    assert scores[good] < threshold / 2 and scores[bad] == 1.0
    assert (await space.proxies.report_many({}, actor="пульт")) == {}


async def test_batch_commands_respect_limit(space: SpaceHandle) -> None:
    """C1.2: пачка больше потолка отклоняется без изменений."""
    ids = [uuid4() for _ in range(MAX_BATCH + 1)]

    with pytest.raises(ValidationFailed):
        await space.proxies.delete_many(ids, actor="ops")
    with pytest.raises(ValidationFailed):
        await space.proxies.override_many(dict.fromkeys(ids, "dead"), reason="r", actor="ops")
    with pytest.raises(ValidationFailed):
        await space.proxies.report_many(dict.fromkeys(ids, True), actor="ops")
