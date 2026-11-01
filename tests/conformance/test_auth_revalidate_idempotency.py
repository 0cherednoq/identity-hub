"""AR3.3: `auth.revalidate()` — идемпотентность по `request_id` и конкурентность (D-335, L3)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from tests.conformance.test_auth_revalidate_credential import (
    KEY,
    _operations,
    _revalidate,
    _setup,
)
from tests.conformance.test_session_first_auth import _expire_provider_claim

from identity_hub import (
    AuthState,
    CredentialVerdict,
    IdempotencyConflict,
    IdentityHub,
    ProbeResult,
    RevalidationStatus,
    SessionVerdict,
    SpaceHandle,
    Standing,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


async def test_completed_request_is_replayed_without_provider_call(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator, probe, identity_id = await _setup(hub_under_test, space, "ri_replay")

    first = await _revalidate(space, identity_id, request_id="r-1")
    second = await _revalidate(space, identity_id, request_id="r-1")

    assert first.credential_verdict is CredentialVerdict.VALID
    assert second == first
    assert authenticator.calls == 1 and probe.calls == 0
    assert len([op for op in await _operations(space, identity_id) if op.kind == "login"]) == 1


async def test_session_step_is_replayed(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    _auth, probe, identity_id = await _setup(
        hub_under_test, space, "ri_session", session=True, probe_state=AuthState.AUTHENTICATED
    )

    first = await _revalidate(space, identity_id, credential=None, request_id="r-1")
    second = await _revalidate(space, identity_id, credential=None, request_id="r-1")

    assert first.session_verdict is SessionVerdict.VALID and second == first
    assert probe.calls == 1


async def test_definitive_failure_is_replayed(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    authenticator, _probe, identity_id = await _setup(hub_under_test, space, "ri_reject", mode="reject")

    first = await _revalidate(space, identity_id, request_id="r-1")
    second = await _revalidate(space, identity_id, request_id="r-1")

    assert first.credential_verdict is CredentialVerdict.REJECTED
    assert second == first and authenticator.calls == 1


async def test_same_request_id_with_other_credential_conflicts(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator, _probe, identity_id = await _setup(hub_under_test, space, "ri_conflict")
    await space.secrets.put_credential(identity_id, "default", {"phone": "+7"}, name="backup", actor="t")
    await _revalidate(space, identity_id, request_id="r-1")

    with pytest.raises(IdempotencyConflict):
        await _revalidate(space, identity_id, ("default", "backup"), request_id="r-1")
    assert authenticator.calls == 1


async def test_new_request_id_is_a_new_attempt(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    authenticator, probe, identity_id = await _setup(hub_under_test, space, "ri_new", session=True)

    first = await _revalidate(space, identity_id, request_id="r-1")
    second = await _revalidate(space, identity_id, request_id="r-2")

    assert first.credential_verdict is second.credential_verdict is CredentialVerdict.VALID
    assert set(first.operation_ids).isdisjoint(second.operation_ids)
    assert authenticator.calls == 2 and probe.calls == 2
    logins = [op for op in await _operations(space, identity_id) if op.kind == "login"]
    assert len(logins) == 2


async def test_technical_failure_is_retried_with_same_request_id(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator, _probe, identity_id = await _setup(hub_under_test, space, "ri_retry", mode="tech")
    first = await _revalidate(space, identity_id, request_id="r-1")
    assert first.status is RevalidationStatus.FAILED and first.retryable

    authenticator.mode = "ok"
    second = await _revalidate(space, identity_id, request_id="r-1")

    assert second.status is RevalidationStatus.COMPLETED
    assert second.credential_verdict is CredentialVerdict.VALID and authenticator.calls == 2


async def test_parallel_requests_make_one_provider_call(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator, _probe, identity_id = await _setup(hub_under_test, space, "ri_parallel")
    authenticator.gate = asyncio.Event()
    first = asyncio.create_task(_revalidate(space, identity_id, request_id="r-1"))
    await authenticator.entered.wait()

    second = await _revalidate(space, identity_id, request_id="r-1")
    authenticator.gate.set()
    finished = await first

    assert second.status is RevalidationStatus.IN_PROGRESS
    assert (
        finished.status is RevalidationStatus.COMPLETED
        and finished.credential_verdict is CredentialVerdict.VALID
    )
    assert authenticator.calls == 1


async def test_live_operation_with_same_request_key_reports_in_progress(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator, _probe, identity_id = await _setup(hub_under_test, space, "ri_live")
    async with space._tx(own=True) as tx:
        await tx.enqueue_provider_operation(identity_id, "login", request_key="r-1:credential")
        await tx.claim_provider_operations("other-worker", limit=1, claim_for=timedelta(minutes=5))

    result = await _revalidate(space, identity_id, request_id="r-1")

    assert result.status is RevalidationStatus.IN_PROGRESS
    assert [blocker.code for blocker in result.blockers] == ["busy"]
    assert authenticator.calls == 0


async def test_request_continues_after_claim_expired(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    authenticator, _probe, identity_id = await _setup(hub_under_test, space, "ri_expired")
    async with space._tx(own=True) as tx:
        queued = await tx.enqueue_provider_operation(identity_id, "login", request_key="r-1:credential")
        await tx.claim_provider_operations("crashed-worker", limit=1, claim_for=timedelta(minutes=5))
    await _expire_provider_claim(hub_under_test, space, queued.id)
    assert (await hub_under_test.workers.provider_operations.run_once()).counts == {"recovered": 1}

    result = await _revalidate(space, identity_id, request_id="r-1")

    assert result.status is RevalidationStatus.COMPLETED
    assert result.credential_verdict is CredentialVerdict.VALID and authenticator.calls == 1


async def test_standing_probe_is_replayed_too(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    from tests.conformance.test_auth_revalidate_session import Probe as _Unused  # noqa: F401
    from tests.conformance.test_auth_revalidate_session import StandingProbe
    from tests.conformance.test_auth_revalidate_session import _setup as _setup_probe

    probe = StandingProbe(ProbeResult(AuthState.AUTHENTICATED), ProbeResult(Standing.BANNED))
    identity_id = await _setup_probe(hub_under_test, space, "ri_standing", probe)

    first = await space.auth.revalidate(identity_id, actor="checker", reason="recheck", request_id="r-1")
    second = await space.auth.revalidate(identity_id, actor="checker", reason="recheck", request_id="r-1")

    assert first.applied["standing"] == "applied" and second == first
    assert (probe.auth_calls, probe.standing_calls) == (1, 1)
    _ = (KEY, UUID)
