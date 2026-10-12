"""C1.5: отвязка аккаунта от прокси и пакетная перепривязка."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.fixtures.api import register

from identity_hub import NotFound, ProxyPolicy, ProxyStatus, SpaceHandle, ValidationFailed
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


async def _changes(space: SpaceHandle) -> list[Any]:
    page = await space.admin.event_page(filters=EventFilters(type="proxy.binding.changed"))
    return list(reversed(page.items))


async def _bound(space: SpaceHandle) -> dict[UUID, int | None]:
    return {p.id: p.bound_count for p in (await space.proxies.list()).items}


async def _binding(space: SpaceHandle, identity_id: UUID) -> UUID | None:
    async with space._tx(own=True) as tx:
        row = await tx.get_binding(identity_id)
    return row.id if row else None


async def test_unbind_releases_capacity_and_next_lease_rebinds(space: SpaceHandle) -> None:
    """C1.5: отвязка освобождает место на прокси; следующий захват выбирает прокси по политике."""
    home = await _proxy(space, "10.14.0.1", max_identities=1)
    identity_id = await _account(space, "+1")
    await space.proxies.rebind(identity_id, home, reason="тест", actor="op")

    assert await space.proxies.unbind(identity_id, reason="смена адреса", actor="ops:ivan") is True

    assert await _binding(space, identity_id) is None and (await _bound(space))[home] == 0
    event = (await _changes(space))[-1]
    assert (event.actor, event.identity_id, event.service) == ("ops:ivan", identity_id, "fake_tg")
    assert (event.data["from_proxy_id"], event.data["to_proxy_id"]) == (str(home), None)
    assert event.data["reason"] == "смена адреса"
    assert await space.proxies.unbind(identity_id, reason="повтор", actor="op") is False
    assert len(await _changes(space)) == 2  # привязка и отвязка; повтор события не пишет

    await space.proxies.override(home, ProxyStatus.DEAD, reason="тест", actor="op")
    spare = await _proxy(space, "10.14.0.2")
    pool = space.pools.define("p", service="fake_tg", proxy_policy=ProxyPolicy(group="g"))
    async with pool.lease() as lease:
        assert lease.identity_id == identity_id and lease.proxy_id == spare


async def test_unbind_validates_input(space: SpaceHandle) -> None:
    """C1.5: причина и автор обязательны; неизвестный аккаунт — `NotFound`."""
    identity_id = await _account(space, "+1")

    with pytest.raises(ValidationFailed):
        await space.proxies.unbind(identity_id, reason=" ", actor="op")
    with pytest.raises(NotFound):
        await space.proxies.unbind(uuid4(), reason="тест", actor="op")


async def test_rebind_many_reports_each_account(space: SpaceHandle) -> None:
    """C1.5: отказы — нет места, прокси не `alive`, прокси выключен, нет такого; остальные перенесены."""
    roomy = await _proxy(space, "10.14.1.1", max_identities=2)
    small = await _proxy(space, "10.14.1.2", max_identities=1)
    sick = await _proxy(space, "10.14.1.3")
    off = await _proxy(space, "10.14.1.4")
    await space.proxies.override(sick, ProxyStatus.QUARANTINED, reason="тест", actor="op")
    await space.proxies.disable(off, reason="тест", actor="op")
    accounts = sorted([await _account(space, f"+{n}") for n in range(6)])
    one, two, three, four, five, six = accounts
    missing = uuid4()

    result = await space.proxies.rebind_many(
        {one: roomy, two: small, three: small, four: sick, five: off, six: uuid4(), missing: roomy},
        reason="перенос пула",
        actor="ops:ivan",
    )

    assert result.applied == [one, two]
    assert {r.key: r.code for r in result.rejected} == {
        three: "no_resource_available",
        four: "validation_error",
        five: "validation_error",
        six: "not_found",
        missing: "not_found",
    }
    assert [await _binding(space, a) for a in accounts] == [roomy, small, None, None, None, None]
    bound = await _bound(space)
    assert (bound[roomy], bound[small], bound[sick], bound[off]) == (1, 1, 0, 0)
    assert sorted(e.identity_id for e in await _changes(space)) == sorted([one, two])
    assert {e.data["reason"] for e in await _changes(space)} == {"перенос пула"}


async def test_rebind_many_atomic_moves_nothing_on_refusal(space: SpaceHandle) -> None:
    """C1.5: `atomic=True` — отказ одного аккаунта отменяет перенос всех."""
    small = await _proxy(space, "10.14.2.1", max_identities=1)
    first, second = sorted([await _account(space, "+1"), await _account(space, "+2")])

    with pytest.raises(Exception) as caught:
        await space.proxies.rebind_many({first: small, second: small}, reason="тест", actor="op", atomic=True)

    assert type(caught.value).__name__ == "LeaseUnavailable"
    assert (await _binding(space, first), await _binding(space, second)) == (None, None)
    assert (await _bound(space))[small] == 0 and await _changes(space) == []
    with pytest.raises(ValidationFailed):
        await space.proxies.rebind_many({first: small}, reason="", actor="op")
