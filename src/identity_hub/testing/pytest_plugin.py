"""Pytest-плагин с полностью in-memory окружением Identity Hub.

Асинхронные фикстуры объявлены через `pytest_asyncio.fixture`, поэтому работают и в strict-режиме
pytest-asyncio (по умолчанию), и в auto. Без pytest-asyncio тест, которому нужна фикстура хаба,
останавливается с понятной ошибкой (R1-01).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import Any
from uuid import UUID

import pytest

try:
    import pytest_asyncio
except ImportError:  # pragma: no cover - зависит от окружения пользователя
    pytest_asyncio = None  # type: ignore[assignment]

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


_NEEDS_ASYNCIO = (
    "фикстуры Identity Hub асинхронные: установите pytest-asyncio (pip install 'identity-hub[testing]') "
    "и не отключайте его плагин; в strict-режиме отметьте тест @pytest.mark.asyncio"
)


def _async_fixture(function: Any) -> Any:
    if pytest_asyncio is None:  # pragma: no cover - зависит от окружения пользователя
        return pytest.fixture(function)
    return pytest_asyncio.fixture(function)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    if "identity_hub" in getattr(item, "fixturenames", ()) and not item.config.pluginmanager.has_plugin(
        "asyncio"
    ):
        raise pytest.UsageError(_NEEDS_ASYNCIO)


@_async_fixture
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
    return identity_hub.space("test", "Test", create=True)


@pytest.fixture
def identity_hub_make_identity(
    identity_hub_space: SpaceHandle,
) -> Callable[..., Awaitable[UUID]]:
    """Фабрика, уже привязанная к fixture ``identity_hub_space``."""

    return partial(make_identity, identity_hub_space)
