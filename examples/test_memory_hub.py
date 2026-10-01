"""Пользовательский тест без PostgreSQL: ``pytest examples/test_memory_hub.py``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from identity_hub.hub import SpaceHandle


async def test_worker_gets_ready_identity(
    identity_hub_space: SpaceHandle,
    identity_hub_make_identity: Callable[..., Awaitable[UUID]],
) -> None:
    identity_id = await identity_hub_make_identity(tags=["worker"])

    async with identity_hub_space.pools.define(
        "workers", service="test_service", holder="test"
    ).lease() as lease:
        assert lease.identity_id == identity_id
        async with lease.operation("work"):
            pass
