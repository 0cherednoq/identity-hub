"""Pytest-плагин с полностью in-memory окружением Identity Hub."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from uuid import UUID

import pytest

from identity_hub.clock import FakeClock
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.protocols import SecretsProvider
from identity_hub.service import Service
from identity_hub.testing.factories import make_identity, make_service
from identity_hub.testing.memory import MemoryStore

__all__ = [
    "identity_hub",
    "identity_hub_clock",
    "identity_hub_make_identity",
    "identity_hub_secrets",
    "identity_hub_service",
    "identity_hub_services",
    "identity_hub_space",
    "identity_hub_store",
]


@pytest.fixture
def identity_hub_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def identity_hub_secrets() -> SecretsProvider:
    return StaticSecretsProvider({"testing": b"\x49" * 32}, "testing")


@pytest.fixture
def identity_hub_service() -> Service:
    return make_service()


@pytest.fixture
def identity_hub_services(identity_hub_service: Service) -> Sequence[Service]:
    return (identity_hub_service,)


@pytest.fixture
def identity_hub_store(identity_hub_clock: FakeClock) -> MemoryStore:
    return MemoryStore(identity_hub_clock)


@pytest.fixture
async def identity_hub(
    identity_hub_store: MemoryStore,
    identity_hub_clock: FakeClock,
    identity_hub_secrets: SecretsProvider,
    identity_hub_services: Sequence[Service],
) -> IdentityHub:
    hub = IdentityHub(
        secrets=identity_hub_secrets,
        services=identity_hub_services,
        store=identity_hub_store,
        clock=identity_hub_clock,
        strict_events=True,
    )
    await hub.start()
    return hub


@pytest.fixture
def identity_hub_space(identity_hub: IdentityHub) -> SpaceHandle:
    return identity_hub.space("test", "Test")


@pytest.fixture
def identity_hub_make_identity(
    identity_hub_space: SpaceHandle,
) -> Callable[..., Awaitable[UUID]]:
    """Фабрика, уже привязанная к fixture ``identity_hub_space``."""

    return partial(make_identity, identity_hub_space)
