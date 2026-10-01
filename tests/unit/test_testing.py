"""Публичные фабрики и pytest-плагин identity_hub.testing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest

from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.service import OperationRule
from identity_hub.testing import MemoryStore, make_identity, make_service

pytestmark = pytest.mark.unit


def test_make_service_has_work_operation() -> None:
    service = make_service("demo", operations={"send": OperationRule(needs_auth=False)})
    assert service.name == "demo"
    assert set(service.operations) == {"send", "probe"}


async def test_make_identity_creates_pool_candidate() -> None:
    service = make_service()
    from identity_hub.crypto import StaticSecretsProvider

    hub = IdentityHub(
        secrets=StaticSecretsProvider({"test": b"\x51" * 32}, "test"),
        services=[service],
        store=MemoryStore(),
    )
    await hub.start()
    space = hub.space("factory")
    identity_id = await make_identity(space, tags=["worker"])

    lease = await space.pools.define("p", service=service, tags=["worker"]).acquire()
    assert lease.identity_id == identity_id


async def test_plugin_fixtures_are_ready(
    identity_hub: IdentityHub,
    identity_hub_space: SpaceHandle,
    identity_hub_make_identity: Callable[..., Awaitable[UUID]],
) -> None:
    identity_id = await identity_hub_make_identity()
    assert isinstance(identity_hub.store, MemoryStore)
    assert (await identity_hub_space.identities.card(identity_id)).identity.id == identity_id
