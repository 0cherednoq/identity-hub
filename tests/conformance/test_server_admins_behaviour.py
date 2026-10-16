"""Регрессии server-admins S0–S3 на MemoryStore и PostgreSQL."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr
from tests.fixtures.services import FAKE_TG, TgSession

from identity_hub import (
    Blame,
    ChangeSource,
    Fault,
    IdempotencyConflict,
    IdentityCreate,
    IdentityHub,
    LoginFailure,
    LoginOutcome,
    Observation,
    Override,
    RevisionConflict,
    SpaceHandle,
    Standing,
    TransitionNotAllowed,
)
from identity_hub.domain.regions import Outcome
from identity_hub.read.events import EventFilters
from identity_hub.rollups import BucketSize

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


class FailingAuthenticator:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    async def login(self, ctx: Any) -> BaseModel:
        raise self.failure


class GatedFailureAuthenticator(FailingAuthenticator):
    def __init__(self, failure: Exception) -> None:
        super().__init__(failure)
        self.called = asyncio.Event()
        self.gate = asyncio.Event()

    async def login(self, ctx: Any) -> BaseModel:
        self.called.set()
        await self.gate.wait()
        raise self.failure


class FailThenSucceedAuthenticator:
    def __init__(self) -> None:
        self.calls = 0

    async def login(self, ctx: Any) -> BaseModel:
        self.calls += 1
        if self.calls == 1:
            raise LoginFailure("credentials_rejected", retryable=False)
        return TgSession(auth_key=SecretStr("new-session"))


class InvalidFaultClassifier:
    def classify(self, exc: BaseException) -> Fault:
        return Fault(
            Blame.IDENTITY_BANNED,
            standing="frozen",
            until=datetime(2026, 1, 2),
        )


async def _login_identity(hub: IdentityHub, space: SpaceHandle, name: str, failure: Exception) -> UUID:
    hub.register_service(replace(FAKE_TG, name=name, authenticator=FailingAuthenticator(failure)))
    await hub.start()
    identity = await space.identities.create(
        IdentityCreate(service=name, external_ref=f"{name}-1", credentials={"phone": "+100"}),
        actor="test",
    )
    return identity.id


async def _region(space: SpaceHandle, identity_id: UUID, region: str) -> Any:
    card = await space.identities.card(identity_id)
    return next(item for item in card.regions if item.region == region and item.key in {"", "default"})


async def _expire_provider_claim(hub: IdentityHub, space: SpaceHandle, operation_id: UUID) -> None:
    from identity_hub.testing import MemoryStore

    if isinstance(hub._store, MemoryStore):
        row = hub._store._state.provider_operations[operation_id]
        hub._store._state.provider_operations[operation_id] = replace(
            row, claim_expires_at=hub.clock.now() - timedelta(seconds=1)
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


async def test_login_failure_code_is_in_card_event_and_operation(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _login_identity(
        hub_under_test,
        space,
        "sa_login_code",
        LoginFailure("credentials_rejected", retryable=False),
    )

    assert await space.auth.login(identity_id) is LoginOutcome.FAILED
    assert (await space.identities.get(identity_id)).auth_code == "credentials_rejected"
    auth_region = await _region(space, identity_id, "auth")
    assert auth_region.code == "credentials_rejected"
    assert auth_region.retryable is False
    events = await space.admin.event_page(
        filters=EventFilters(type="identity.login.failed", include_journal=True)
    )
    event = next(item for item in events.items if item.identity_id == identity_id)
    assert event.data == {
        "error": "LoginFailure",
        "code": "credentials_rejected",
        "retryable": False,
    }
    async with space._tx(own=True) as tx:
        (operation,) = await tx.list_provider_operations(identity_id=identity_id, states=None, limit=10)
    assert (operation.state, operation.error_code, operation.result) == (
        "failed",
        "credentials_rejected",
        {"outcome": "failed", "retryable": False},
    )

    source_events = await hub_under_test._store.rollup_source_events(
        space.slug, event.occurred_at - timedelta(minutes=1), event.occurred_at + timedelta(minutes=1)
    )
    await hub_under_test._rollups.handle_events(source_events)
    series = await space.analytics.series(
        "logins.failed",
        start=event.occurred_at - timedelta(minutes=1),
        end=event.occurred_at + timedelta(minutes=1),
        bucket_size=BucketSize.MINUTE,
        group_by="code",
    )
    assert [group.key for group in series.groups] == ["credentials_rejected"]


async def test_retryable_login_failure_is_visible_in_card(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _login_identity(
        hub_under_test,
        space,
        "sa_login_retryable_card",
        LoginFailure("temporary", retryable=True),
    )

    assert await space.auth.login(identity_id) is LoginOutcome.FAILED
    assert (await _region(space, identity_id, "auth")).retryable is True
    assert (await _region(space, identity_id, "lifecycle")).retryable is None


async def test_login_failure_fault_applies_ban_and_cooling(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    banned = await _login_identity(
        hub_under_test,
        space,
        "sa_login_ban",
        LoginFailure(
            "identity_banned",
            retryable=False,
            fault=Fault(Blame.IDENTITY_BANNED, code="platform_ban"),
        ),
    )
    cooling = await _login_identity(
        hub_under_test,
        space,
        "sa_login_cooling",
        LoginFailure(
            "transient",
            fault=Fault(Blame.IDENTITY, retry_after=timedelta(minutes=5)),
        ),
    )

    assert await space.auth.login(banned) is LoginOutcome.FAILED
    assert await space.auth.login(cooling) is LoginOutcome.FAILED
    assert (await space.identities.get(banned)).standing is Standing.BANNED
    cooled = await space.identities.get(cooling)
    assert cooled.availability == "cooling" and cooled.availability_until is not None


async def test_login_failure_can_mark_session_invalid(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _login_identity(
        hub_under_test,
        space,
        "sa_session_invalid",
        LoginFailure(
            "session_invalid",
            retryable=False,
            fault=Fault(Blame.SESSION_INVALID, raw_status="SESSION_REVOKED"),
        ),
    )

    assert await space.auth.login(identity_id) is LoginOutcome.FAILED
    identity = await space.identities.get(identity_id)
    auth = await _region(space, identity_id, "auth")
    assert identity.auth == "session_invalid" and identity.auth_code == "session_invalid"
    assert auth.raw_status == "SESSION_REVOKED"


async def test_plain_login_exception_never_persists_its_message(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    secret = "password=Sup3rSecret"
    identity_id = await _login_identity(hub_under_test, space, "sa_login_secret", RuntimeError(secret))

    report = await space.auth.login_many(service="sa_login_secret")

    assert report.failed[identity_id] == "RuntimeError"
    events = await space.admin.event_page(
        filters=EventFilters(type="identity.login.failed", include_journal=True)
    )
    async with space._tx(own=True) as tx:
        operations = await tx.list_provider_operations(identity_id=identity_id, states=None, limit=10)
    assert secret not in str(events.items)
    assert secret not in str(operations)


async def test_next_login_clears_previous_auth_code(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    service = replace(
        FAKE_TG,
        name="sa_login_retry",
        authenticator=FailThenSucceedAuthenticator(),
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()
    identity = await space.identities.create(
        IdentityCreate(service=service.name, external_ref="sa-login-retry"), actor="test"
    )

    assert await space.auth.login(identity.id) is LoginOutcome.FAILED
    assert (await space.identities.get(identity.id)).auth_code == "credentials_rejected"
    assert await space.auth.login(identity.id) is LoginOutcome.SUCCEEDED
    current = await space.identities.get(identity.id)
    assert current.auth == "authenticated" and current.auth_code is None


async def test_deleted_is_terminal_for_public_observations(space: SpaceHandle) -> None:
    identity = await space.identities.create(
        IdentityCreate(service=FAKE_TG.name, external_ref="sa-deleted"), actor="test"
    )
    await space.identities.override(
        identity.id,
        Override("standing", "", "deleted", reason="platform deleted", actor="admin"),
    )

    for observed in (Standing.LIMITED, Standing.GOOD):
        decision = await space.identities.observe(
            identity.id,
            Observation("standing", "", observed),
            actor="webhook",
            source=ChangeSource.WEBHOOK,
        )
        assert decision.outcome is Outcome.STICKY
        assert (await space.identities.get(identity.id)).standing is Standing.DELETED

    drift = await space.admin.event_page(
        filters=EventFilters(type="identity.state.drift", include_journal=True)
    )
    assert any(item.identity_id == identity.id and item.data["cause"] == "sticky" for item in drift.items)
    await space.identities.override(
        identity.id,
        Override("standing", "", "good", reason="ошибка удаления", actor="admin"),
    )
    assert (await space.identities.get(identity.id)).standing is Standing.GOOD


async def test_login_fault_cannot_revive_deleted(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    auth = GatedFailureAuthenticator(
        LoginFailure(
            "identity_limited",
            fault=Fault(Blame.IDENTITY_BANNED, standing="limited", code="rate_limit"),
        )
    )
    service = replace(FAKE_TG, name="sa_deleted_login", authenticator=auth)
    hub_under_test.register_service(service)
    await hub_under_test.start()
    identity = await space.identities.create(
        IdentityCreate(service=service.name, external_ref="sa-deleted-login"), actor="test"
    )

    task = asyncio.create_task(space.auth.login(identity.id))
    await auth.called.wait()
    await space.identities.override(
        identity.id,
        Override("standing", "", "deleted", reason="platform deleted", actor="admin"),
    )
    auth.gate.set()
    assert await task is LoginOutcome.FAILED
    assert (await space.identities.get(identity.id)).standing is Standing.DELETED


async def test_late_login_failure_applies_no_fault_effects(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    auth = GatedFailureAuthenticator(LoginFailure("identity_banned", fault=Fault(Blame.IDENTITY_BANNED)))
    service = replace(FAKE_TG, name="sa_late_failure", authenticator=auth)
    hub_under_test.register_service(service)
    await hub_under_test.start()
    identity = await space.identities.create(
        IdentityCreate(service=service.name, external_ref="sa-late-failure"), actor="test"
    )
    task = asyncio.create_task(space.auth.login(identity.id))
    await auth.called.wait()
    async with space._tx(own=True) as tx:
        (running,) = await tx.list_provider_operations(identity_id=identity.id, states=None, limit=10)
    await _expire_provider_claim(hub_under_test, space, running.id)
    assert await hub_under_test._provider_worker.run_once(limit=10) == 1

    auth.gate.set()
    assert await task is LoginOutcome.FAILED
    assert (await space.identities.get(identity.id)).standing is Standing.GOOD


async def test_temporary_ban_expires_but_indefinite_ban_does_not(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    now = hub_under_test.clock.now()
    temporary = await _login_identity(
        hub_under_test,
        space,
        "sa_temp_ban",
        LoginFailure("identity_banned", fault=Fault(Blame.IDENTITY_BANNED, until=now - timedelta(seconds=1))),
    )
    permanent = await _login_identity(
        hub_under_test,
        space,
        "sa_perm_ban",
        LoginFailure("identity_banned", fault=Fault(Blame.IDENTITY_BANNED)),
    )
    await space.auth.login(temporary)
    await space.auth.login(permanent)
    assert (await space.identities.get(temporary)).standing is Standing.BANNED

    await hub_under_test.maintenance.run_all()

    assert (await space.identities.get(temporary)).standing is Standing.GOOD
    assert (await space.identities.get(permanent)).standing is Standing.BANNED


async def test_operation_fault_sets_limited_until_in_one_failure(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    until = hub_under_test.clock.now() + timedelta(hours=2)
    service = replace(
        FAKE_TG,
        name="sa_fault_until",
        error_classifier={
            RuntimeError: Fault(
                Blame.IDENTITY_BANNED,
                standing="limited",
                until=until,
                code="rate_limit",
            )
        },
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()
    identity = await space.identities.create(
        IdentityCreate(
            service=service.name,
            external_ref="sa-fault-until",
            lifecycle="ready",
            sessions={"default": {"auth_key": "k"}},
        ),
        actor="test",
    )
    pool = space.pools.define("sa-fault", service=service)

    with pytest.raises(RuntimeError):
        async with pool.lease() as lease, lease.operation("read_channel"):
            raise RuntimeError("response contains secret")

    limited = await space.identities.get(identity.id)
    assert limited.standing is Standing.LIMITED and limited.standing_until == until


async def test_invalid_fault_from_classifier_falls_back_to_request(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    service = replace(
        FAKE_TG,
        name="sa_invalid_fault",
        error_classifier=InvalidFaultClassifier(),
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()
    identity = await space.identities.create(
        IdentityCreate(
            service=service.name,
            external_ref="sa-invalid-fault",
            lifecycle="ready",
            sessions={"default": {"auth_key": "k"}},
        ),
        actor="test",
    )

    with pytest.raises(RuntimeError):
        async with (
            space.pools.define("sa-invalid", service=service).lease() as lease,
            lease.operation("read_channel"),
        ):
            raise RuntimeError("platform error")

    assert (await space.identities.get(identity.id)).standing is Standing.GOOD
    events = await space.admin.event_page(filters=EventFilters(type="operation.failed", include_journal=True))
    failure = next(item for item in events.items if item.identity_id == identity.id)
    assert failure.data["blame"] == "request"


async def test_cool_down_does_not_weaken_quarantine(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    service = replace(
        FAKE_TG,
        name="sa_quarantine",
        error_classifier={RuntimeError: Fault(Blame.IDENTITY, retry_after=timedelta(minutes=5))},
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()
    identity = await space.identities.create(
        IdentityCreate(
            service=service.name,
            external_ref="sa-quarantine",
            lifecycle="ready",
            sessions={"default": {"auth_key": "k"}},
        ),
        actor="test",
    )
    pool = space.pools.define("sa-quarantine", service=service)

    with pytest.raises(RuntimeError):
        async with pool.lease() as lease, lease.operation("read_channel") as operation:
            await space.identities.override(
                identity.id,
                Override(
                    "availability",
                    "",
                    "quarantined",
                    reason="too many errors",
                    actor="admin",
                ),
            )
            await operation.cool_down(60)
            raise RuntimeError("rate limit")

    with pytest.raises(TransitionNotAllowed):
        await space.identities.cool_down(identity.id, for_=timedelta(minutes=5), actor="worker")
    current = await space.identities.get(identity.id)
    assert current.availability == "quarantined" and current.availability_until is None


async def test_replace_credentials_resets_auth_code_only_when_requested(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _login_identity(
        hub_under_test,
        space,
        "sa_reset_auth",
        LoginFailure("credentials_rejected", retryable=False),
    )
    await space.secrets.replace_session(
        identity_id, {"auth_key": "still-valid"}, actor="admin", mode="replace"
    )
    await space.auth.login(identity_id)

    await space.secrets.replace_credentials(identity_id, {"phone": "+101"}, actor="admin", reset_auth=False)
    assert (await _region(space, identity_id, "auth")).code == "credentials_rejected"

    await space.secrets.replace_credentials(identity_id, {"phone": "+102"}, actor="admin", reset_auth=True)
    auth = await _region(space, identity_id, "auth")
    assert auth.state == "no_session" and auth.code is None
    access = await space.secrets.describe(identity_id)
    assert access.sessions and access.sessions[0].status == "active"


async def test_put_credential_resets_auth_only_when_requested(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _login_identity(
        hub_under_test,
        space,
        "sa_put_reset_auth",
        LoginFailure("credentials_rejected", retryable=False),
    )
    await space.secrets.replace_session(
        identity_id, {"auth_key": "still-valid"}, actor="admin", mode="replace"
    )
    assert await space.auth.login(identity_id) is LoginOutcome.FAILED

    revision = (await space.identities.get(identity_id)).revision
    await space.secrets.put_credential(
        identity_id,
        "default",
        {"phone": "+201"},
        name="fresh",
        actor="admin",
        expected_revision=revision,
    )
    unchanged = await _region(space, identity_id, "auth")
    assert unchanged.code == "credentials_rejected" and unchanged.retryable is False

    revision = (await space.identities.get(identity_id)).revision
    with pytest.raises(RevisionConflict):
        await space.secrets.put_credential(
            identity_id,
            "default",
            {"phone": "+202"},
            name="newer",
            actor="admin",
            expected_revision=revision - 1,
            reset_auth=True,
        )
    assert (await _region(space, identity_id, "auth")).code == "credentials_rejected"
    assert "newer" not in {item.name for item in (await space.secrets.credentials(identity_id))}

    await space.secrets.put_credential(
        identity_id,
        "default",
        {"phone": "+202"},
        name="newer",
        actor="admin",
        expected_revision=revision,
        reset_auth=True,
    )
    auth = await _region(space, identity_id, "auth")
    assert auth.state == "no_session" and auth.code is None and auth.retryable is None
    access = await space.secrets.describe(identity_id)
    assert access.sessions and access.sessions[0].status == "active"

    service = hub_under_test.service("sa_put_reset_auth")
    authenticated = await space.identities.create(
        IdentityCreate(
            service=service.name,
            external_ref="sa-put-reset-authenticated",
            lifecycle="ready",
            credentials={"phone": "+203"},
            sessions={"default": {"auth_key": "authenticated"}},
        ),
        actor="test",
    )
    assert (await space.identities.get(authenticated.id)).auth == "authenticated"
    await space.secrets.put_credential(
        authenticated.id,
        "default",
        {"phone": "+204"},
        actor="admin",
        reset_auth=True,
    )
    assert (await space.identities.get(authenticated.id)).auth == "authenticated"


async def test_reset_auth_participates_in_credentials_idempotency(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _login_identity(
        hub_under_test,
        space,
        "sa_reset_idempotency",
        LoginFailure("credentials_rejected", retryable=False),
    )
    await space.auth.login(identity_id)
    credentials = {"phone": "+103"}

    await space.secrets.replace_credentials(
        identity_id,
        credentials,
        actor="admin",
        reset_auth=False,
        idempotency_key="replace-credentials",
    )
    with pytest.raises(IdempotencyConflict):
        await space.secrets.replace_credentials(
            identity_id,
            credentials,
            actor="admin",
            reset_auth=True,
            idempotency_key="replace-credentials",
        )
