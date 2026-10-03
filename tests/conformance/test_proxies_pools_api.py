"""IMP-23 (I8.6): `space.proxies`, `space.pools` и явная сигнатура аренды."""

from __future__ import annotations

from uuid import uuid4

import pytest

from identity_hub import (
    AlreadyExists,
    AuthState,
    IdentityCreate,
    Lifecycle,
    NotFound,
    ProxyStatus,
    SpaceHandle,
    ValidationFailed,
)

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


async def test_create_get_ensure_and_conflict(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("socks5://user:pw-777@10.0.0.1:1080", group="res", max_identities=2)

    assert (proxy.group, proxy.host, proxy.port, proxy.max_identities) == ("res", "10.0.0.1", 1080, 2)
    assert proxy.status is ProxyStatus.ALIVE
    assert "pw-777" not in repr(proxy)
    assert (await space.proxies.get(proxy.id)).id == proxy.id
    assert (
        await space.proxies.url(proxy.id, actor="ops", reason="тест") == "socks5://user:pw-777@10.0.0.1:1080"
    )

    again, created = await space.proxies.ensure("socks5://user:pw-777@10.0.0.1:1080", group="res")
    assert created is False and again.id == proxy.id
    with pytest.raises(AlreadyExists):
        await space.proxies.create("socks5://user:pw-777@10.0.0.1:1080", group="res")


async def test_create_many_atomic_and_skip(space: SpaceHandle) -> None:
    first = await space.proxies.create("http://10.0.1.1:8080", group="dc")

    with pytest.raises(AlreadyExists):
        await space.proxies.create_many(["http://10.0.1.2:8080", "http://10.0.1.1:8080"], group="dc")
    assert [p.host for p in (await space.proxies.list(group="dc")).items] == ["10.0.1.1"]

    result = await space.proxies.create_many(
        ["http://10.0.1.2:8080", "http://10.0.1.1:8080"], group="dc", on_conflict="skip"
    )
    assert [p.host for p in result.created] == ["10.0.1.2"]
    assert result.skipped == [first.id]
    with pytest.raises(ValidationFailed):
        await space.proxies.create_many([], group="dc", on_conflict="replace")


async def test_list_filters_and_pages(space: SpaceHandle) -> None:
    for n in range(3):
        await space.proxies.create(f"http://10.0.2.{n}:80", group="a")
    other = await space.proxies.create("http://10.0.3.1:80", group="b")
    await space.proxies.override(other.id, ProxyStatus.DEAD, reason="тест", actor="admin")

    first = await space.proxies.list(group="a", limit=2)
    second = await space.proxies.list(group="a", limit=2, after=first.next_cursor)
    dead = await space.proxies.list(status=ProxyStatus.DEAD)

    assert len(first.items) == 2 and len(second.items) == 1 and second.next_cursor is None
    assert all(p.bound_count == 0 for p in first.items)
    assert [p.id for p in dead.items] == [other.id]


async def test_health_reports(space: SpaceHandle) -> None:
    proxy = await space.proxies.create("http://10.0.4.1:80", group="h")
    threshold = space.proxies.health.error_threshold

    status = ProxyStatus.ALIVE
    for _ in range(int(threshold) + 2):
        status = await space.proxies.report_failure(proxy.id, actor="test")
    assert status is ProxyStatus.QUARANTINED
    await space.proxies.report_success(proxy.id)

    async def healthy(_url: str) -> bool:
        return True

    assert await space.proxies.recheck(proxy.id, healthy, actor="admin") is ProxyStatus.ALIVE
    assert (await space.proxies.recheck_due(healthy)).statuses == {}
    with pytest.raises(NotFound):
        await space.proxies.get(uuid4())


async def test_pools_define_lease_capacity_revoke(space: SpaceHandle) -> None:
    identity = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="+p1",
            lifecycle=Lifecycle.READY,
            auth=AuthState.AUTHENTICATED,
            tags=["parser"],
        ),
        actor="test",
    )
    pool = space.pools.define("parsers", service="fake_tg", tags=["parser"])

    assert await pool.capacity() == 1
    async with pool.lease(for_operation="read_channel") as lease:
        assert lease.identity_id == identity.id
        assert await pool.capacity() == 0
        assert await space.pools.revoke(identity.id, reason="тест", actor="admin") is True
    assert await space.pools.revoke(identity.id, reason="тест", actor="admin") is False


async def test_lease_rejects_unknown_parameter(space: SpaceHandle) -> None:
    pool = space.pools.define("strict", service="fake_tg")
    with pytest.raises(TypeError):
        pool.lease(for_opration="read_channel")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await pool.acquire(tag=["x"])  # type: ignore[call-arg]


async def test_due_proxies_include_dead_and_skip_pinned(space: SpaceHandle) -> None:
    """F3.7a: на перепроверку идут `quarantined` и `dead`; закреплённые и чужая группа — нет."""
    from datetime import timedelta

    async def make(host: str, group: str = "g") -> ProxyStatus:
        return (await space.proxies.create(f"http://{host}:80", group=group)).id  # type: ignore[return-value]

    dead, pinned, alive, other = [await make(f"10.9.0.{n}") for n in range(3)] + [await make("10.9.1.1", "x")]
    await space.proxies.override(dead, ProxyStatus.DEAD, reason="тест", actor="op")
    await space.proxies.override(
        pinned, ProxyStatus.DEAD, reason="держим", actor="op", pin_for=timedelta(days=1)
    )
    await space.proxies.override(other, ProxyStatus.DEAD, reason="тест", actor="op")

    async with space._tx(own=True) as tx:
        everything = await tx.due_proxies(10)
        only_group = await tx.due_proxies(10, pool="g")
        limited = await tx.due_proxies(1, pool="x")

    assert sorted(everything) == sorted([dead, other])  # alive и закреплённый не возвращаются
    assert only_group == [dead] and limited == [other]
    assert alive not in everything
