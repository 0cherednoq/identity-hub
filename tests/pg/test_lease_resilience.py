"""F2.1: heartbeat и освобождение аренды на PostgreSQL (R1-03, R1-12, R1-14, R1-21)."""

from __future__ import annotations

import pytest

from identity_hub.hub import IdentityHub
from tests.fixtures import lease_resilience as scenarios

pytestmark = pytest.mark.pg


async def test_single_heartbeat_failure_keeps_lease(
    hub: IdentityHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    await scenarios.check_single_heartbeat_failure_keeps_lease(hub, monkeypatch)


async def test_heartbeat_gives_up_at_deadline(hub: IdentityHub, monkeypatch: pytest.MonkeyPatch) -> None:
    await scenarios.check_heartbeat_gives_up_at_deadline(hub, monkeypatch)


async def test_double_cancel_during_release_frees_lease(
    hub: IdentityHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    await scenarios.check_double_cancel_during_release_frees_lease(hub, monkeypatch)


async def test_release_validates_reason_before_touching_lease(hub: IdentityHub) -> None:
    await scenarios.check_release_validates_reason_before_touching_lease(hub)


async def test_release_can_be_retried_after_failure(
    hub: IdentityHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    await scenarios.check_release_can_be_retried_after_failure(hub, monkeypatch)


async def test_proxy_pool_exhausted_is_reported_once_per_acquire(hub: IdentityHub) -> None:
    await scenarios.check_proxy_pool_exhausted_is_reported_once_per_acquire(hub)
