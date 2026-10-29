"""SF2.1: публичная адресная проверка session slot."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from identity_hub import (
    AuthState,
    ConcurrentModification,
    IdentityCreate,
    IdentityHub,
    Lease,
    LeaseExpired,
    LeaseLost,
    LeaseOwnershipLost,
    LeaseRevoked,
    ProbeContext,
    ProbeResult,
    RevisionConflict,
    Service,
    SessionAdoptionOutcome,
    SessionVerificationOutcome,
    SpaceHandle,
    StaleFencing,
)
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


class Session(BaseModel):
    token: str


class AuthProbe:
    def __init__(self, result: ProbeResult | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[str, Session | None]] = []
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def auth(self, ctx: ProbeContext) -> ProbeResult | None:
        value = await ctx.session()
        self.calls.append((ctx.session_slot, value if isinstance(value, Session) else None))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


async def _setup(
    hub: IdentityHub,
    space: SpaceHandle,
    name: str,
    result: ProbeResult | BaseException,
) -> tuple[AuthProbe, Any]:
    probe = AuthProbe(result)
    service = Service(name, name, session=Session, probes=probe)
    hub.register_service(service)
    await hub.start()
    created = await space.identities.create(
        IdentityCreate(
            service=name,
            external_ref=f"{name}-1",
            lifecycle="provisioning",
            sessions={"cookie-1": {"token": "cookie-token"}},
        ),
        actor="test",
    )
    return probe, created


async def _setup_default(
    hub: IdentityHub,
    space: SpaceHandle,
    name: str,
    result: ProbeResult,
) -> tuple[AuthProbe, Any]:
    probe = AuthProbe(result)
    service = Service(name, name, session=Session, probes=probe)
    hub.register_service(service)
    await hub.start()
    created = await space.identities.create(
        IdentityCreate(
            service=name,
            external_ref=f"{name}-1",
            lifecycle="provisioning",
            session={"token": "default-token"},
        ),
        actor="test",
    )
    return probe, created


async def _current_session(space: SpaceHandle, identity_id: Any) -> Any:
    async with space._tx(own=True) as tx:
        return (await tx.session_history(identity_id, "cookie-1"))[-1]


async def _expire_provider_claim(hub: IdentityHub, space: SpaceHandle, operation_id: Any) -> None:
    from identity_hub.testing import MemoryStore

    if isinstance(hub._store, MemoryStore):
        row = hub._store._state.provider_operations[operation_id]
        hub._store._state.provider_operations[operation_id] = replace(
            row,
            claim_expires_at=hub.clock.now() - timedelta(seconds=1),
        )
        return
    from sqlalchemy import text

    async with space._tx(own=True) as tx:
        await tx.execute(  # type: ignore[attr-defined]
            text(
                "UPDATE ih_provider_operations SET claim_expires_at = now() - interval '1 second' "
                "WHERE id = :id"
            ),
            {"id": operation_id},
        )


async def test_verify_named_session_marks_metadata_only(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_verify_valid",
        ProbeResult(AuthState.AUTHENTICATED),
    )

    result = await space.auth.verify_session(created.id, "cookie-1", actor="checker")

    assert result.outcome is SessionVerificationOutcome.VALID
    assert probe.calls == [("cookie-1", Session(token="cookie-token"))]
    assert (await _current_session(space, created.id)).last_verified_at is not None
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.NO_SESSION
    assert identity.lifecycle == "provisioning"


async def test_verify_invalid_session_rejects_only_source_metadata(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_verify_invalid",
        ProbeResult(AuthState.SESSION_INVALID, code="expired"),
    )

    result = await space.auth.verify_session(created.id, "cookie-1", actor="checker")

    assert result.outcome is SessionVerificationOutcome.INVALID
    row = await _current_session(space, created.id)
    assert row.last_rejected_at is not None and row.reject_code == "session_invalid"
    assert (await space.identities.get(created.id)).auth == AuthState.NO_SESSION
    assert len(probe.calls) == 1


async def test_verify_invalid_default_marks_global_auth_invalid(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup_default(
        hub_under_test,
        space,
        "sf_verify_default_invalid",
        ProbeResult(AuthState.SESSION_INVALID),
    )

    result = await space.auth.verify_session(created.id, actor="checker")

    assert result.outcome is SessionVerificationOutcome.INVALID
    assert (await space.identities.get(created.id)).auth == AuthState.SESSION_INVALID
    assert probe.calls == [("default", Session(token="default-token"))]


async def test_verify_technical_failure_is_retryable_without_metadata_change(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_verify_failure",
        ConnectionError("provider unavailable"),
    )

    result = await space.auth.verify_session(created.id, "cookie-1", actor="checker")

    assert result.outcome is SessionVerificationOutcome.FAILED and result.retryable
    row = await _current_session(space, created.id)
    assert row.last_verified_at is None and row.last_rejected_at is None
    assert len(probe.calls) == 1


async def test_verify_missing_slot_does_not_call_provider(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_verify_missing",
        ProbeResult(AuthState.AUTHENTICATED),
    )

    result = await space.auth.verify_session(created.id, "missing", actor="checker")

    assert result.outcome is SessionVerificationOutcome.INVALID
    assert probe.calls == []


async def test_adopt_named_session_sets_canonical_auth_and_activates(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_valid",
        ProbeResult(AuthState.AUTHENTICATED),
    )

    result = await space.auth.adopt_session(
        created.id,
        "cookie-1",
        actor="checker",
        activate=True,
    )

    assert result.outcome is SessionAdoptionOutcome.ADOPTED
    assert result.auth == AuthState.AUTHENTICATED and result.lifecycle == "ready"
    assert probe.calls == [("cookie-1", Session(token="cookie-token"))]
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.AUTHENTICATED and identity.lifecycle == "ready"
    async with space._tx(own=True) as tx:
        source = (await tx.session_history(created.id, "cookie-1"))[-1]
        default = await tx.session_history(created.id, "default")
    assert source.last_verified_at is not None
    assert len(default) == 1 and default[0].status == "active"
    assert default[0].last_verified_at is not None


async def test_adopt_invalid_session_does_not_create_default_or_flip_auth(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_invalid",
        ProbeResult(AuthState.SESSION_INVALID),
    )

    result = await space.auth.adopt_session(
        created.id,
        "cookie-1",
        actor="checker",
        activate=True,
    )

    assert result.outcome is SessionAdoptionOutcome.INVALID
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.NO_SESSION and identity.lifecycle == "provisioning"
    async with space._tx(own=True) as tx:
        assert await tx.session_history(created.id, "default") == []
    assert len(probe.calls) == 1


async def test_adopt_invalid_default_marks_global_auth_invalid(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup_default(
        hub_under_test,
        space,
        "sf_adopt_default_invalid",
        ProbeResult(AuthState.SESSION_INVALID),
    )

    result = await space.auth.adopt_session(created.id, actor="checker", activate=True)

    assert result.outcome is SessionAdoptionOutcome.INVALID
    assert result.auth == AuthState.SESSION_INVALID
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.SESSION_INVALID
    assert identity.lifecycle == "provisioning"
    assert probe.calls == [("default", Session(token="default-token"))]


async def test_adopt_technical_failure_has_no_partial_state(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_failure",
        ConnectionError("provider unavailable"),
    )

    result = await space.auth.adopt_session(
        created.id,
        "cookie-1",
        actor="checker",
        activate=True,
    )

    assert result.outcome is SessionAdoptionOutcome.FAILED and result.retryable
    row = await _current_session(space, created.id)
    assert row.last_verified_at is None and row.last_rejected_at is None
    assert (await space.identities.get(created.id)).auth == AuthState.NO_SESSION
    async with space._tx(own=True) as tx:
        assert await tx.session_history(created.id, "default") == []
    assert len(probe.calls) == 1


async def test_adopt_is_idempotent_for_default_history(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_repeat",
        ProbeResult(AuthState.AUTHENTICATED),
    )

    first = await space.auth.adopt_session(created.id, "cookie-1", actor="checker")
    second = await space.auth.adopt_session(created.id, "cookie-1", actor="checker")

    assert first.outcome is second.outcome is SessionAdoptionOutcome.ADOPTED
    async with space._tx(own=True) as tx:
        default = await tx.session_history(created.id, "default")
    assert len(default) == 1
    assert len(probe.calls) == 2


async def test_adopt_default_does_not_replace_its_own_history(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_default",
        ProbeResult(AuthState.AUTHENTICATED),
    )
    await space.secrets.replace_session(
        created.id,
        {"token": "default-token"},
        actor="test",
        slot="default",
    )

    result = await space.auth.adopt_session(created.id, actor="checker")

    assert result.outcome is SessionAdoptionOutcome.ADOPTED
    async with space._tx(own=True) as tx:
        default = await tx.session_history(created.id, "default")
    assert len(default) == 1 and default[0].last_verified_at is not None
    assert probe.calls == [("default", Session(token="default-token"))]


async def test_adopt_final_transaction_rolls_back_as_a_unit(
    hub_under_test: IdentityHub,
    space: SpaceHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_rollback",
        ProbeResult(AuthState.AUTHENTICATED),
    )
    original = Lease._write_session

    async def fail_after_default_write(self: Lease[Any, Any], *args: Any, **kwargs: Any) -> None:
        await original(self, *args, **kwargs)
        raise RuntimeError("injected final transaction failure")

    monkeypatch.setattr(Lease, "_write_session", fail_after_default_write)

    with pytest.raises(RuntimeError, match="injected final transaction failure"):
        await space.auth.adopt_session(
            created.id,
            "cookie-1",
            actor="checker",
            activate=True,
        )

    source = await _current_session(space, created.id)
    assert source.last_verified_at is None and source.last_rejected_at is None
    async with space._tx(own=True) as tx:
        assert await tx.session_history(created.id, "default") == []
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.NO_SESSION and identity.lifecycle == "provisioning"
    assert len(probe.calls) == 1


async def test_adopt_rejects_late_result_after_source_replacement(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_source_race",
        ProbeResult(AuthState.AUTHENTICATED),
    )
    probe.release = asyncio.Event()
    adoption = asyncio.create_task(
        space.auth.adopt_session(created.id, "cookie-1", actor="checker", activate=True)
    )
    await probe.entered.wait()
    await space.secrets.replace_session(
        created.id,
        {"token": "replacement-token"},
        actor="concurrent-writer",
        slot="cookie-1",
    )
    probe.release.set()

    with pytest.raises(ConcurrentModification):
        await adoption

    source = await _current_session(space, created.id)
    assert source.last_verified_at is None and source.last_rejected_at is None
    async with space._tx(own=True) as tx:
        assert await tx.session_history(created.id, "default") == []
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.NO_SESSION and identity.lifecycle == "provisioning"
    assert len(probe.calls) == 1


async def test_adopt_expected_revision_is_checked_before_provider(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_stale_revision",
        ProbeResult(AuthState.AUTHENTICATED),
    )
    stale = created.revision
    await space.identities.set_note(created.id, "changed", actor="writer")

    with pytest.raises(RevisionConflict):
        await space.auth.adopt_session(
            created.id,
            "cookie-1",
            actor="checker",
            expected_revision=stale,
        )

    assert probe.calls == []


async def test_adopt_expected_revision_is_rechecked_after_provider(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_revision_race",
        ProbeResult(AuthState.AUTHENTICATED),
    )
    probe.release = asyncio.Event()
    adoption = asyncio.create_task(
        space.auth.adopt_session(
            created.id,
            "cookie-1",
            actor="checker",
            activate=True,
            expected_revision=created.revision,
        )
    )
    await probe.entered.wait()
    await space.identities.set_note(created.id, "changed", actor="writer")
    probe.release.set()

    with pytest.raises(RevisionConflict):
        await adoption

    async with space._tx(own=True) as tx:
        assert await tx.session_history(created.id, "default") == []
    assert (await space.identities.get(created.id)).auth == AuthState.NO_SESSION


async def test_cancelled_adoption_does_not_apply_late_result(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_cancelled",
        ProbeResult(AuthState.AUTHENTICATED),
    )
    probe.release = asyncio.Event()
    adoption = asyncio.create_task(
        space.auth.adopt_session(created.id, "cookie-1", actor="checker", activate=True)
    )
    await probe.entered.wait()
    adoption.cancel()

    with pytest.raises(asyncio.CancelledError):
        await adoption

    async with space._tx(own=True) as tx:
        assert await tx.session_history(created.id, "default") == []
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.NO_SESSION and identity.lifecycle == "provisioning"


async def test_provider_secret_text_is_not_exposed(
    hub_under_test: IdentityHub,
    space: SpaceHandle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "TOKEN-SESSION-FIRST-LEAK"
    _probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_redaction",
        ConnectionError(f"provider failed with {secret}"),
    )

    result = await space.auth.adopt_session(created.id, "cookie-1", actor="checker")

    assert result.outcome is SessionAdoptionOutcome.FAILED
    events = await space.admin.event_page(
        filters=EventFilters(identity_id=created.id, include_journal=True),
        limit=100,
    )
    assert secret not in repr(result) and secret not in caplog.text
    assert secret not in repr(events.items)


async def test_expired_provider_claim_rejects_late_adoption(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_claim_expired",
        ProbeResult(AuthState.AUTHENTICATED),
    )
    probe.release = asyncio.Event()
    adoption = asyncio.create_task(
        space.auth.adopt_session(created.id, "cookie-1", actor="checker", activate=True)
    )
    await probe.entered.wait()
    async with space._tx(own=True) as tx:
        (operation,) = await tx.list_provider_operations(
            identity_id=created.id,
            states=frozenset({"running"}),
            limit=10,
        )
    await _expire_provider_claim(hub_under_test, space, operation.id)
    assert await hub_under_test._provider_worker.run_once(limit=10) == 1
    probe.release.set()

    with pytest.raises((LeaseOwnershipLost, LeaseLost, LeaseRevoked, LeaseExpired, StaleFencing)):
        await adoption

    async with space._tx(own=True) as tx:
        assert await tx.session_history(created.id, "default") == []
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.NO_SESSION and identity.lifecycle == "provisioning"


async def test_revoked_lease_rejects_late_adoption(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_lease_revoked",
        ProbeResult(AuthState.AUTHENTICATED),
    )
    probe.release = asyncio.Event()
    adoption = asyncio.create_task(
        space.auth.adopt_session(created.id, "cookie-1", actor="checker", activate=True)
    )
    await probe.entered.wait()
    assert await space.pools.revoke(created.id, reason="race test", actor="test")
    probe.release.set()

    with pytest.raises((LeaseOwnershipLost, LeaseLost, LeaseRevoked, LeaseExpired, StaleFencing)):
        await adoption

    async with space._tx(own=True) as tx:
        assert await tx.session_history(created.id, "default") == []
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.NO_SESSION and identity.lifecycle == "provisioning"


async def test_concurrent_adoptions_leave_one_coherent_default(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_two_slots",
        ProbeResult(AuthState.AUTHENTICATED),
    )
    await space.secrets.replace_session(
        created.id,
        {"token": "second-token"},
        actor="test",
        slot="cookie-2",
    )

    first, second = await asyncio.gather(
        space.auth.adopt_session(created.id, "cookie-1", actor="worker-1"),
        space.auth.adopt_session(created.id, "cookie-2", actor="worker-2"),
    )

    assert first.outcome is second.outcome is SessionAdoptionOutcome.ADOPTED
    assert [slot for slot, _session in probe.calls] in [
        ["cookie-1", "cookie-2"],
        ["cookie-2", "cookie-1"],
    ]
    last_session = probe.calls[-1][1]
    assert last_session is not None
    revealed = await space.secrets.reveal(
        created.id,
        "session",
        actor="test",
        reason="assert coherent winner",
    )
    assert revealed["token"] == last_session.token
    identity = await space.identities.get(created.id)
    assert identity.auth == AuthState.AUTHENTICATED


async def test_waiting_adoption_rechecks_expected_revision_before_provider(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    probe, created = await _setup(
        hub_under_test,
        space,
        "sf_adopt_same_revision",
        ProbeResult(AuthState.AUTHENTICATED),
    )
    probe.release = asyncio.Event()
    first = asyncio.create_task(
        space.auth.adopt_session(
            created.id,
            "cookie-1",
            actor="worker-1",
            expected_revision=created.revision,
        )
    )
    await probe.entered.wait()
    second = asyncio.create_task(
        space.auth.adopt_session(
            created.id,
            "cookie-1",
            actor="worker-2",
            expected_revision=created.revision,
        )
    )
    probe.release.set()

    assert (await first).outcome is SessionAdoptionOutcome.ADOPTED
    with pytest.raises(RevisionConflict):
        await second
    assert len(probe.calls) == 1
