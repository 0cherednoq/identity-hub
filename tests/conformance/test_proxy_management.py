"""F3.7b: управление прокси и выбор прокси одинаковы на обоих хранилищах (R2-F15, R2-F23)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.fixtures.api import register

from identity_hub import AlreadyExists, IdentityHub, NotFound, ProxyStatus, SpaceHandle, ValidationFailed
from identity_hub.proxies import ProxyHealthPolicy
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


async def _proxy(space: SpaceHandle, host: str, group: str = "g", **options: Any) -> UUID:
    return (await space.proxies.create(f"http://user:pw-old@{host}:80", group=group, **options)).id


async def _account(space: SpaceHandle, ref: str) -> UUID:
    return (await register(space, service="fake_tg", external_ref=ref)).identity_id


async def _events(space: SpaceHandle, type_: str) -> list[Any]:
    page = await space.admin.event_page(filters=EventFilters(type=type_))
    return list(page.items)


async def _bound_count(space: SpaceHandle, proxy_id: UUID) -> int:
    page = await space.proxies.list()
    return next(item.bound_count for item in page.items if item.id == proxy_id)  # type: ignore[return-value]


# ---------------------------------------------------------------- R2-F15


async def test_replace_credentials_keeps_address_and_bindings(space: SpaceHandle) -> None:
    proxy_id = await _proxy(space, "10.0.0.1")
    identity_id = await _account(space, "+1")
    await space.proxies.rebind(identity_id, proxy_id, reason="тест", actor="op")

    updated = await space.proxies.replace_credentials(
        proxy_id, username="user2", password="pw-new", actor="op"
    )

    assert (updated.id, updated.host, updated.status) == (proxy_id, "10.0.0.1", ProxyStatus.ALIVE)
    assert await space.proxies.url(proxy_id, actor="op", reason="тест") == "http://user2:pw-new@10.0.0.1:80"
    assert await _bound_count(space, proxy_id) == 1
    (event,) = await _events(space, "proxy.updated")
    assert (event.actor, event.proxy_id, event.data["fields"]) == ("op", proxy_id, ["credentials"])
    assert "pw-new" not in str(dict(event.data)) and "user2" not in str(dict(event.data))
    # новая пара адрес+логин занята этим же прокси
    again, created = await space.proxies.ensure("http://user2:other@10.0.0.1:80", group="g")
    assert (again.id, created) == (proxy_id, False)

    await space.proxies.replace_credentials(proxy_id, username=None, password=None, actor="op")
    assert await space.proxies.url(proxy_id, actor="op", reason="тест") == "http://10.0.0.1:80"


async def test_replace_credentials_rejects_collision_and_unknown_proxy(space: SpaceHandle) -> None:
    first = await _proxy(space, "10.0.1.1")
    second = (await space.proxies.create("http://other:pw@10.0.1.1:80", group="g")).id

    with pytest.raises(AlreadyExists):
        await space.proxies.replace_credentials(second, username="user", password="x", actor="op")
    with pytest.raises(ValidationFailed):
        await space.proxies.replace_credentials(first, username=None, password="x", actor="op")
    with pytest.raises(NotFound):
        await space.proxies.replace_credentials(uuid4(), username="u", password="p", actor="op")
    assert await space.proxies.url(second, actor="op", reason="тест") == "http://other:pw@10.0.1.1:80"


async def test_move_changes_group_and_keeps_binding(space: SpaceHandle) -> None:
    proxy_id = await _proxy(space, "10.0.2.1", group="old")
    identity_id = await _account(space, "+1")
    await space.proxies.rebind(identity_id, proxy_id, reason="тест", actor="op")

    moved = await space.proxies.move(proxy_id, group="new", actor="op")
    await space.proxies.move(proxy_id, group="new", actor="op")  # повтор — без события

    assert moved.group == "new" and await _bound_count(space, proxy_id) == 1
    assert [p.id for p in (await space.proxies.list(group="new")).items] == [proxy_id]
    assert (await space.proxies.list(group="old")).items == []
    (event,) = await _events(space, "proxy.updated")
    assert (event.data["fields"], event.data["pool"], event.data["from_pool"]) == (["group"], "new", "old")
    with pytest.raises(ValidationFailed):
        await space.proxies.move(proxy_id, group="", actor="op")


async def test_delete_refuses_bound_proxy_and_unbinds_on_request(space: SpaceHandle) -> None:
    proxy_id = await _proxy(space, "10.0.3.1")
    spare = await _proxy(space, "10.0.3.2")
    bound = [await _account(space, f"+{n}") for n in range(2)]
    for identity_id in bound:
        await space.proxies.rebind(identity_id, proxy_id, reason="тест", actor="op")

    with pytest.raises(ValidationFailed):
        await space.proxies.delete(proxy_id, actor="op")
    assert (await space.proxies.get(proxy_id)).id == proxy_id

    assert await space.proxies.delete(spare, actor="op") == 0  # без привязок — сразу
    assert await space.proxies.delete(proxy_id, actor="op", unbind=True) == 2

    with pytest.raises(NotFound):
        await space.proxies.get(proxy_id)
    with pytest.raises(NotFound):
        await space.proxies.delete(proxy_id, actor="op")
    async with space._tx(own=True) as tx:
        assert [await tx.get_binding(identity_id) for identity_id in bound] == [None, None]
    changed = [
        e for e in await _events(space, "proxy.binding.changed") if e.data["reason"] == "proxy deleted"
    ]
    assert sorted(e.identity_id for e in changed) == sorted(bound)
    assert all(e.data["to_proxy_id"] is None and e.data["from_proxy_id"] == str(proxy_id) for e in changed)
    deleted = await _events(space, "proxy.deleted")
    assert sorted(e.data["unbound"] for e in deleted) == [0, 2]
    # адрес свободен: тот же прокси можно завести заново
    assert (await space.proxies.create("http://user:pw-old@10.0.3.1:80", group="g")).id != proxy_id


# ---------------------------------------------------------------- R2-F23


async def test_parallel_failure_reports_are_all_counted(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """20 параллельных сообщений об ошибке дают `error_score = 20`, а не теряются."""
    hub_under_test.proxy_health = ProxyHealthPolicy(error_threshold=1000)
    proxy_id = await _proxy(space, "10.0.4.1")

    await asyncio.wait_for(
        asyncio.gather(
            *(space.proxies.report_failure(proxy_id, weight=1.0, actor="test") for _ in range(20))
        ),
        timeout=60,
    )

    assert (await space.proxies.get(proxy_id)).error_score == 20.0


async def test_threshold_still_quarantines_and_stops_counting(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    hub_under_test.proxy_health = ProxyHealthPolicy(error_threshold=3)
    proxy_id = await _proxy(space, "10.0.5.1")

    statuses = [await space.proxies.report_failure(proxy_id, actor="test") for _ in range(5)]

    assert [str(s) for s in statuses] == ["alive", "alive", "quarantined", "quarantined", "quarantined"]
    assert (await space.proxies.get(proxy_id)).error_score == 3.0  # в карантине счёт не растёт


# ---------------------------------------------------------------- choose / bind / rebind


async def test_choose_proxy_picks_least_loaded_alive_with_capacity(space: SpaceHandle) -> None:
    first = await _proxy(space, "10.0.6.1", max_identities=2)
    second = await _proxy(space, "10.0.6.2", max_identities=2)
    dead = await _proxy(space, "10.0.6.3")
    await space.proxies.override(dead, ProxyStatus.DEAD, reason="тест", actor="op")
    await _proxy(space, "10.0.6.4", group="other")

    async with space._tx(own=True) as tx:
        peek = await tx.choose_proxy("g", None, claim=False)
        claimed = [(await tx.choose_proxy("g", None, claim=True)).id for _ in range(4)]  # type: ignore[union-attr]
        full = await tx.choose_proxy("g", None, claim=True)
        capped = await tx.choose_proxy("g", 1, claim=True)
        empty = await tx.choose_proxy("missing", None, claim=True)

    assert peek is not None and peek.id in (first, second)
    assert sorted(claimed) == sorted([first, first, second, second])  # нагрузка распределена поровну
    assert claimed[0] != claimed[1]
    assert full is None and capped is None and empty is None


async def test_bind_proxy_stores_binding_and_releases_previous_capacity(space: SpaceHandle) -> None:
    first = await _proxy(space, "10.0.7.1")
    second = await _proxy(space, "10.0.7.2")
    identity_id = await _account(space, "+1")

    async with space._tx(own=True) as tx:
        await tx.adjust_proxy_load(first, 1)
        await tx.bind_proxy(identity_id, first, None)
    assert await _bound_count(space, first) == 1

    async with space._tx(own=True) as tx:
        await tx.adjust_proxy_load(second, 1)
        await tx.bind_proxy(identity_id, second, first)
        bound = await tx.get_binding(identity_id)

    assert bound is not None and bound.id == second
    assert (await _bound_count(space, first), await _bound_count(space, second)) == (0, 1)


async def test_rebind_proxy_respects_capacity_and_health(space: SpaceHandle) -> None:
    home = await _proxy(space, "10.0.8.1")
    small = await _proxy(space, "10.0.8.2", max_identities=1)
    sick = await _proxy(space, "10.0.8.3")
    await space.proxies.override(sick, ProxyStatus.QUARANTINED, reason="тест", actor="op")
    one, two = await _account(space, "+1"), await _account(space, "+2")

    async with space._tx(own=True) as tx:
        moved = await tx.rebind_proxy(one, home)
        same = await tx.rebind_proxy(one, home)
        to_small = await tx.rebind_proxy(one, small)
        no_room = await tx.rebind_proxy(two, small)
        unhealthy = await tx.rebind_proxy(two, sick)
        unknown_identity = await tx.rebind_proxy(uuid4(), home)
        unknown_proxy = await tx.rebind_proxy(two, uuid4())

    assert moved is not None and (moved[0], moved[1].id) == (None, home)
    assert same is not None and (same[0], same[1].id) == (home, home)  # повтор — без изменений
    assert to_small is not None and (to_small[0], to_small[1].id) == (home, small)
    assert no_room is None and unhealthy is None and unknown_identity is None and unknown_proxy is None
    assert (await _bound_count(space, home), await _bound_count(space, small)) == (0, 1)
