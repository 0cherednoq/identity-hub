from __future__ import annotations

import asyncio

from identity_hub import Identity, IdentityCreate, IdentityFilter, Lifecycle, SpaceHandle


async def _ready(space: SpaceHandle, ref: str) -> Identity:
    return await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref=ref,
            sessions={"default": {"auth_key": f"session-{ref}"}},
            lifecycle=Lifecycle.READY,
        ),
        actor="test",
    )


async def test_link_predicates_list_find_and_count(space: SpaceHandle) -> None:
    a, b, c, d = [await _ready(space, f"+link-{n}") for n in range(4)]
    exchange = await space.identities.create(
        IdentityCreate(service="fake_exchange", external_ref="link-source"), actor="test"
    )
    await space.links.add(a.id, "backup", b.id, actor="test")
    await space.links.add(exchange.id, "managed", a.id, actor="test")
    await space.links.add(c.id, "managed", b.id, actor="test")

    without_backup = IdentityFilter(service="fake_tg", without_link="backup")
    assert {item.id for item in (await space.identities.list(without_backup)).items} == {
        b.id,
        c.id,
        d.id,
    }
    targeted = IdentityFilter(service="fake_tg", is_target_of=("managed", None))
    assert {item.id for item in (await space.identities.list(targeted)).items} == {a.id, b.id}
    not_exchange_target = IdentityFilter(service="fake_tg", not_target_of=("managed", "fake_exchange"))
    assert {item.id for item in (await space.identities.list(not_exchange_target)).items} == {
        b.id,
        c.id,
        d.id,
    }
    untargeted = IdentityFilter(service="fake_tg", not_target_of=("managed", None))
    assert await space.identities.count(untargeted) == 2
    found = await space.identities.find(untargeted)
    assert found is not None and found.id in {c.id, d.id}


async def test_link_predicates_select_distinct_exclusive_leases(space: SpaceHandle) -> None:
    targeted = await _ready(space, "+lease-targeted")
    first = await _ready(space, "+lease-first")
    second = await _ready(space, "+lease-second")
    source = await space.identities.create(
        IdentityCreate(service="fake_exchange", external_ref="lease-source"), actor="test"
    )
    await space.links.add(source.id, "managed", targeted.id, actor="test")

    pool = space.pools.define("link-campaign", service="fake_tg", holder="campaign")
    leases = await asyncio.gather(
        pool.acquire(not_target_of=("managed", None)),
        pool.acquire(not_target_of=("managed", None)),
    )
    try:
        assert {lease.identity.id for lease in leases} == {first.id, second.id}
    finally:
        await asyncio.gather(*(lease.release() for lease in leases))
