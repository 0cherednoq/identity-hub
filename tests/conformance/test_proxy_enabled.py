"""C1.4a: выключение прокси оператором — отдельное от здоровья измерение (D-224, п. 4 и 9)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.fixtures.api import register

from identity_hub import (
    IdentityHub,
    LeaseUnavailable,
    ProxyDownPolicy,
    ProxyPolicy,
    ProxyStatus,
    SpaceHandle,
    ValidationFailed,
)
from identity_hub.proxies import ProxyHealthPolicy
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


async def _proxy(space: SpaceHandle, host: str, **options: Any) -> UUID:
    return (await space.proxies.create(f"http://user:pw@{host}:80", group="g", **options)).id


async def _account(space: SpaceHandle, ref: str) -> UUID:
    reg = await register(
        space, service="fake_tg", external_ref=ref, lifecycle="ready", session={"auth_key": "k"}
    )
    identity_id: UUID = reg.identity_id
    return identity_id


async def _events(space: SpaceHandle, type_: str) -> list[Any]:
    return list((await space.admin.event_page(filters=EventFilters(type=type_))).items)


def _pool(space: SpaceHandle, policy: ProxyDownPolicy) -> Any:
    return space.pools.define(
        f"p-{policy}", service="fake_tg", proxy_policy=ProxyPolicy(group="g", on_proxy_down=policy)
    )


class Checker:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls = 0

    async def __call__(self, url: str) -> bool:
        self.calls += 1
        return self.ok


async def test_disable_and_enable_keep_health(space: SpaceHandle) -> None:
    """C1.4a: выключение хранит причину и автора и не трогает статус; включение его возвращает."""
    proxy_id = await _proxy(space, "10.13.0.1")
    await space.proxies.override(proxy_id, ProxyStatus.QUARANTINED, reason="тест", actor="op")

    disabled = await space.proxies.disable(proxy_id, reason="работы у провайдера", actor="ops:ivan")

    assert (disabled.enabled, disabled.status) == (False, ProxyStatus.QUARANTINED)
    assert (disabled.disabled_reason, disabled.disabled_by) == ("работы у провайдера", "ops:ivan")
    assert disabled.disabled_at is not None
    listed = (await space.proxies.list(enabled=False)).items
    assert [(p.id, p.enabled, p.disabled_reason) for p in listed] == [
        (proxy_id, False, "работы у провайдера")
    ]
    assert (await space.proxies.list(enabled=True)).items == []
    (event,) = await _events(space, "proxy.disabled")
    assert (event.actor, event.proxy_id, event.data["reason"]) == (
        "ops:ivan",
        proxy_id,
        "работы у провайдера",
    )

    again = await space.proxies.disable(proxy_id, reason="другая", actor="other")  # уже выключен
    assert again.disabled_reason == "работы у провайдера" and len(await _events(space, "proxy.disabled")) == 1

    enabled = await space.proxies.enable(proxy_id, actor="ops:ivan")

    assert (enabled.enabled, enabled.status) == (True, ProxyStatus.QUARANTINED)
    assert (enabled.disabled_reason, enabled.disabled_by, enabled.disabled_at) == (None, None, None)
    assert enabled.next_check_at is not None and disabled.next_check_at is not None
    assert enabled.next_check_at <= disabled.next_check_at  # перепроверка назначена на сейчас
    (event,) = await _events(space, "proxy.enabled")
    assert (event.actor, event.proxy_id) == ("ops:ivan", proxy_id)
    await space.proxies.enable(proxy_id, actor="ops:ivan")  # уже включён
    assert len(await _events(space, "proxy.enabled")) == 1
    with pytest.raises(ValidationFailed):
        await space.proxies.disable(proxy_id, reason=" ", actor="op")


async def test_disabled_proxy_is_not_issued(space: SpaceHandle) -> None:
    """C1.4a: правило выдачи — `enabled` и `alive`: выбор и ручная перепривязка обходят выключенный."""
    off = await _proxy(space, "10.13.1.1")
    on = await _proxy(space, "10.13.1.2", max_identities=1)
    identity_id, other = await _account(space, "+1"), await _account(space, "+2")
    await space.proxies.disable(off, reason="тест", actor="op")

    async with space._tx(own=True) as tx:
        chosen = [await tx.choose_proxy("g", None, claim=True) for _ in range(2)]
        direct = await tx.rebind_proxy(other, off)

    assert [c.id if c else None for c in chosen] == [on, None] and direct is None
    with pytest.raises(ValidationFailed):
        await space.proxies.rebind(identity_id, off, reason="тест", actor="op")


async def test_automation_does_not_touch_disabled_proxy(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """C1.4a: отчёты и плановая перепроверка выключенный прокси не меняют; ручная проверка — меняет."""
    hub_under_test.proxy_health = ProxyHealthPolicy(error_threshold=2, base_backoff=timedelta(0), jitter=0)
    alive = await _proxy(space, "10.13.2.1")
    sick = await _proxy(space, "10.13.2.2")
    await space.proxies.report_failure(alive, actor="worker")
    await space.proxies.override(sick, ProxyStatus.QUARANTINED, reason="тест", actor="op")
    for proxy_id in (alive, sick):
        await space.proxies.disable(proxy_id, reason="тест", actor="op")

    statuses = [await space.proxies.report_failure(alive, weight=5, actor="worker") for _ in range(2)]
    await space.proxies.report_success(alive)
    reported = await space.proxies.report_many({alive: False, sick: True}, actor="пульт")
    checker = Checker(ok=True)
    due = await space.proxies.recheck_due(checker)

    assert statuses == [ProxyStatus.ALIVE, ProxyStatus.ALIVE]
    assert reported == {alive: ProxyStatus.ALIVE, sick: ProxyStatus.QUARANTINED}
    assert (await space.proxies.get(alive)).error_score == 1.0
    assert due.statuses == {} and checker.calls == 0
    assert (await space.proxies.get(sick)).status == ProxyStatus.QUARANTINED

    manual = await space.proxies.recheck(sick, checker, actor="op")

    after = await space.proxies.get(sick)
    assert manual == ProxyStatus.ALIVE and (after.status, after.enabled) == (ProxyStatus.ALIVE, False)


@pytest.mark.parametrize("policy", [ProxyDownPolicy.WAIT, ProxyDownPolicy.REBIND])
async def test_disabled_proxy_keeps_bindings(space: SpaceHandle, policy: ProxyDownPolicy) -> None:
    """C1.4a: выключение всегда ведёт себя как WAIT — аккаунт не выдаётся, привязка остаётся."""
    home = await _proxy(space, "10.13.3.1")
    identity_id = await _account(space, "+1")
    pool = _pool(space, policy)
    async with pool.lease() as lease:
        assert lease.identity_id == identity_id and lease.proxy_id == home
    spare = await _proxy(space, "10.13.3.2")

    await space.proxies.disable(home, reason="тест", actor="op")

    with pytest.raises(LeaseUnavailable):
        async with pool.lease():
            pass
    async with space._tx(own=True) as tx:
        bound = await tx.get_binding(identity_id)
    assert bound is not None and bound.id == home
    assert [e for e in await _events(space, "proxy.binding.changed") if e.data["reason"] != "initial"] == []

    await space.proxies.enable(home, actor="op")
    async with pool.lease() as lease:
        assert lease.proxy_id == home
    assert (await space.proxies.get(spare)).bound_count in (None, 0)


async def test_disable_with_unbind_moves_accounts(space: SpaceHandle) -> None:
    """C1.4a: `unbind=True` отвязывает аккаунты; следующий захват выбирает прокси заново."""
    home = await _proxy(space, "10.13.4.1")
    identity_id = await _account(space, "+1")
    pool = _pool(space, ProxyDownPolicy.WAIT)
    async with pool.lease() as lease:
        assert lease.proxy_id == home
    spare = await _proxy(space, "10.13.4.2")

    await space.proxies.disable(home, reason="вывод из эксплуатации", actor="op", unbind=True)

    unbound = [e for e in await _events(space, "proxy.binding.changed") if e.data["to_proxy_id"] is None]
    assert [(e.identity_id, e.data["from_proxy_id"]) for e in unbound] == [(identity_id, str(home))]
    (event,) = await _events(space, "proxy.disabled")
    assert event.data["unbound"] == 1
    page = {p.id: p.bound_count for p in (await space.proxies.list()).items}
    assert page[home] == 0
    async with pool.lease() as lease:
        assert lease.identity_id == identity_id and lease.proxy_id == spare


async def test_set_enabled_many_reports_each_proxy(space: SpaceHandle) -> None:
    """C1.4a: пакетное выключение и включение на `BatchResult`."""
    first, second = await _proxy(space, "10.13.5.1"), await _proxy(space, "10.13.5.2")
    missing = uuid4()

    off = await space.proxies.set_enabled_many([first, second, missing], False, reason="тест", actor="op")

    assert sorted(off.applied) == sorted([first, second])
    assert [(r.key, r.code) for r in off.rejected] == [(missing, "not_found")]
    assert {p.enabled for p in (await space.proxies.list()).items} == {False}
    with pytest.raises(ValidationFailed):
        await space.proxies.set_enabled_many([first], False, actor="op")  # причина обязательна

    on = await space.proxies.set_enabled_many([first, second], True, actor="op")

    assert sorted(on.applied) == sorted([first, second])
    assert {p.enabled for p in (await space.proxies.list()).items} == {True}
    assert (
        len(await _events(space, "proxy.disabled")) == 2 and len(await _events(space, "proxy.enabled")) == 2
    )
