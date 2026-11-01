"""Статический контракт диагностического допуска (AR2.1)."""

from __future__ import annotations

from typing import assert_type
from uuid import UUID

from identity_hub import (
    AuthRevalidationResult,
    Availability,
    DiagnosticPolicy,
    Lifecycle,
    RevalidationStatus,
    Service,
    SessionVerdict,
    SpaceHandle,
    Standing,
)
from identity_hub.errors import Blocker


def _diagnostics_contract(service: Service) -> None:
    policy = DiagnosticPolicy(availability=frozenset(Availability))
    assert_type(policy.lifecycles, frozenset[Lifecycle])
    assert_type(policy.availability, frozenset[Availability])
    assert_type(policy.standing, frozenset[Standing])
    assert_type(policy.blockers(Lifecycle.READY, Availability.PAUSED, Standing.GOOD), tuple[Blocker, ...])
    assert_type(service.diagnostic_policy(), DiagnosticPolicy)


async def _revalidate_contract(space: SpaceHandle, identity_id: UUID) -> None:
    result = await space.auth.revalidate(identity_id, actor="checker", reason="recheck", request_id="r-1")
    assert_type(result, AuthRevalidationResult)
    assert_type(result.status, RevalidationStatus)
    assert_type(result.session_verdict, SessionVerdict)
    assert_type(result.applied, dict[str, str])
    assert_type(result.blockers, tuple[Blocker, ...])
