"""IMP-04 (I1.4b): ключ идемпотентности команд с секретами — HMAC-теги, ротация (D-208)."""

from __future__ import annotations

from uuid import UUID

import pytest
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG

from identity_hub import (
    IdempotencyConflict,
    IdentityCreate,
    IdentityHub,
    SecretConfigurationError,
    SpaceHandle,
    StaticSecretsProvider,
)
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.conformance]

FP_OLD, FP_NEW = bytes(range(40, 72)), bytes(range(80, 112))


async def _hub(base: IdentityHub, **fingerprint: object) -> IdentityHub:
    hub = IdentityHub(
        store=base._store,
        secrets=StaticSecretsProvider(TEST_KEYS, "k1", **fingerprint),  # type: ignore[arg-type]
        services=[FAKE_TG, FAKE_EXCHANGE],
        clock=base.clock,
    )
    await hub.start()
    return hub


async def _identity(space: SpaceHandle) -> UUID:
    created = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+1", credentials={"phone": "+1", "password": "p0"}),
        actor="test",
    )
    return created.id


async def _updates(space: SpaceHandle, identity_id: UUID, event_type: str) -> list[object]:
    page = await space.admin.event_page(filters=EventFilters(type=event_type, identity_id=identity_id))
    return list(page.items)


async def test_same_key_same_secret_replays_without_writing(space: SpaceHandle) -> None:
    identity_id = await _identity(space)
    creds = {"phone": "+1", "password": "p1"}

    await space.secrets.replace_credentials(identity_id, creds, actor="ops", idempotency_key="c-1")
    revision = (await space.identities.get(identity_id)).revision
    await space.secrets.replace_credentials(identity_id, creds, actor="ops", idempotency_key="c-1")

    assert len(await _updates(space, identity_id, "identity.credentials.updated")) == 1
    assert (await space.identities.get(identity_id)).revision == revision


async def test_same_key_other_secret_conflicts(space: SpaceHandle) -> None:
    identity_id = await _identity(space)
    await space.secrets.replace_credentials(
        identity_id, {"phone": "+1", "password": "p1"}, actor="ops", idempotency_key="c-2"
    )

    with pytest.raises(IdempotencyConflict) as caught:
        await space.secrets.replace_credentials(
            identity_id, {"phone": "+1", "password": "p2"}, actor="ops", idempotency_key="c-2"
        )
    assert "p1" not in str(caught.value) and "p2" not in str(caught.value)
    revealed = await space.secrets.reveal(identity_id, "credentials", actor="ops", reason="проверка")
    assert revealed["password"] == "p1"


async def test_session_replace_is_idempotent(space: SpaceHandle) -> None:
    identity_id = await _identity(space)

    for _ in range(2):
        await space.secrets.replace_session(
            identity_id, {"auth_key": "s1"}, actor="ops", idempotency_key="s-1"
        )
    with pytest.raises(IdempotencyConflict):
        await space.secrets.replace_session(
            identity_id, {"auth_key": "s2"}, actor="ops", idempotency_key="s-1"
        )

    assert len(await _updates(space, identity_id, "identity.session.updated")) == 1


async def test_rotation_overlap_replays_with_historical_key(hub_under_test: IdentityHub) -> None:
    old = await _hub(hub_under_test, fingerprint_keys={"f1": FP_OLD})
    space = old.space("rot-idem", create=True)
    identity_id = await _identity(space)
    creds = {"phone": "+1", "password": "p1"}
    await space.secrets.replace_credentials(identity_id, creds, actor="ops", idempotency_key="r-1")

    new = await _hub(hub_under_test, fingerprint_keys={"f2": FP_NEW, "f1": FP_OLD}, fingerprint_active="f2")
    rotated = new.space("rot-idem")
    await rotated.secrets.replace_credentials(identity_id, creds, actor="ops", idempotency_key="r-1")
    with pytest.raises(IdempotencyConflict):
        await rotated.secrets.replace_credentials(
            identity_id, {"phone": "+1", "password": "other"}, actor="ops", idempotency_key="r-1"
        )
    assert len(await _updates(rotated, identity_id, "identity.credentials.updated")) == 1

    gone = await _hub(hub_under_test, fingerprint_keys={"f2": FP_NEW})
    with pytest.raises(SecretConfigurationError):
        await gone.space("rot-idem").secrets.replace_credentials(
            identity_id, creds, actor="ops", idempotency_key="r-1"
        )


async def test_secret_not_in_events_or_errors(space: SpaceHandle) -> None:
    identity_id = await _identity(space)
    await space.secrets.replace_credentials(
        identity_id, {"phone": "+1", "password": "very-secret"}, actor="ops", idempotency_key="c-3"
    )

    page = await space.admin.event_page(filters=EventFilters(identity_id=identity_id, include_journal=True))
    assert all("very-secret" not in repr(item) for item in page.items)
