"""Контракт полной пригодной ёмкости пула для resource governor."""

from __future__ import annotations

import inspect

import pytest

import identity_hub
from identity_hub import (
    EligibleCapacity,
    IdentityCreate,
    Override,
    Pool,
    PoolCapacity,
    SpaceHandle,
    ValidationFailed,
)

pytestmark = [pytest.mark.conformance]


def test_eligible_capacity_is_public_with_stable_method_signature() -> None:
    from identity_hub.pool import EligibleCapacity

    assert identity_hub.EligibleCapacity is EligibleCapacity
    assert "EligibleCapacity" in identity_hub.__all__

    parameters = inspect.signature(Pool.eligible_count).parameters
    assert list(parameters) == ["self", "for_operation", "among", "require_quota", "limit"]
    assert parameters["for_operation"].default is None
    assert parameters["among"].default is None
    assert parameters["require_quota"].default is False
    assert parameters["limit"].default == 1_000_000
    assert all(
        parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in ("for_operation", "among", "require_quota", "limit")
    )


async def test_existing_capacity_contract_remains_unchanged(space: SpaceHandle) -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg",
                external_ref=f"+eligible-baseline-{index}",
                lifecycle="ready",
                session={"auth_key": "k"},
            )
            for index in range(3)
        ],
        actor="test",
    )
    pool = space.pools.define("eligible-baseline", service="fake_tg", reserve=1, reserve_strict=True)

    assert await pool.capacity() == 1
    assert await pool.capacity_detail() == PoolCapacity(free=3, reserved=1, available=2)


async def test_eligible_capacity_is_stable_while_lease_moves_between_buckets(
    space: SpaceHandle,
) -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg",
                external_ref=f"+eligible-live-{index}",
                lifecycle="ready",
                session={"auth_key": "k"},
            )
            for index in range(3)
        ],
        actor="test",
    )
    pool = space.pools.define("eligible-live", service="fake_tg", reserve=1, reserve_strict=True)

    assert await pool.eligible_count() == EligibleCapacity(
        eligible=3,
        free=3,
        leased=0,
        reserved=1,
        allocatable=2,
    )

    lease = await pool.acquire()
    assert await pool.eligible_count() == EligibleCapacity(
        eligible=3,
        free=2,
        leased=1,
        reserved=1,
        allocatable=2,
    )

    await lease.release()
    assert await pool.eligible_count() == EligibleCapacity(
        eligible=3,
        free=3,
        leased=0,
        reserved=1,
        allocatable=2,
    )


async def test_among_collection_and_predicate_match_lease_selection(space: SpaceHandle) -> None:
    created = await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg",
                external_ref=f"+eligible-among-{index}",
                lifecycle="ready",
                session={"auth_key": "k"},
                labels={"farm": str(index % 2)},
            )
            for index in range(3)
        ],
        actor="test",
    )
    ids = [item.id for item in created.created]
    pool = space.pools.define("eligible-among", service="fake_tg")

    by_ids = await pool.eligible_count(among=[ids[0], ids[2]])
    by_predicate = await pool.eligible_count(among=lambda identity: identity.labels.get("farm") == "0")

    assert by_ids == by_predicate
    assert by_ids.eligible == 2
    lease = await pool.acquire(among=lambda identity: identity.labels.get("farm") == "0")
    assert lease.identity_id in {ids[0], ids[2]}
    await lease.release()


async def test_reserve_semantics_are_reflected_in_allocatable(space: SpaceHandle) -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg",
                external_ref=f"+eligible-reserve-{index}",
                lifecycle="ready",
                session={"auth_key": "k"},
            )
            for index in range(2)
        ],
        actor="test",
    )

    flexible = space.pools.define("eligible-flexible", service="fake_tg", reserve=2)
    strict = space.pools.define("eligible-strict", service="fake_tg", reserve=2, reserve_strict=True)

    assert (await flexible.eligible_count()).reserved == 0
    assert (await flexible.eligible_count()).allocatable == 2
    assert (await strict.eligible_count()).reserved == 2
    assert (await strict.eligible_count()).allocatable == 0


async def test_eligible_count_uses_lifecycle_auth_and_standing_filters(
    space: SpaceHandle,
) -> None:
    created = await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg",
                external_ref="+eligible-admissible",
                lifecycle="ready",
                session={"auth_key": "k"},
            ),
            IdentityCreate(
                service="fake_tg",
                external_ref="+eligible-no-auth",
                lifecycle="ready",
            ),
            IdentityCreate(
                service="fake_tg",
                external_ref="+eligible-draft",
                lifecycle="provisioning",
                session={"auth_key": "k"},
            ),
        ],
        actor="test",
    )
    pool = space.pools.define("eligible-state", service="fake_tg")

    assert (await pool.eligible_count()).eligible == 1

    await space.identities.override(
        created.created[0].id,
        Override("standing", "", "banned", reason="test", actor="test"),
    )
    assert (await pool.eligible_count()).eligible == 0


async def test_capacity_check_failure_does_not_break_release(
    space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="+eligible-release-failure",
            lifecycle="ready",
            session={"auth_key": "k"},
        ),
        actor="test",
    )
    pool = space.pools.define("eligible-release-failure", service="fake_tg")
    lease = await pool.acquire()
    assert await pool.capacity() == 0

    async def failed_check() -> int:
        raise RuntimeError("capacity unavailable")

    monkeypatch.setattr(pool, "_check_capacity", failed_check)
    await lease.release()
    monkeypatch.undo()

    successor = await pool.acquire()
    await successor.release()


async def test_limit_caps_each_internal_counter(space: SpaceHandle) -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg",
                external_ref=f"+eligible-limit-{index}",
                lifecycle="ready",
                session={"auth_key": "k"},
            )
            for index in range(3)
        ],
        actor="test",
    )
    pool = space.pools.define("eligible-limit", service="fake_tg")
    lease = await pool.acquire()

    saturated = await pool.eligible_count(limit=1)

    assert saturated.free == saturated.leased == 1
    assert saturated.eligible == 2
    with pytest.raises(ValidationFailed):
        await pool.eligible_count(limit=0)
    with pytest.raises(ValidationFailed):
        await pool.eligible_count(limit=True)
    await lease.release()
