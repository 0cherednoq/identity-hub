"""IMP-10 (I3.3a): сессия — история записей, режимы in_place и replace в Store (D-218)."""

from __future__ import annotations

from uuid import UUID

import pytest
from tests.fixtures.services import TgSession

from identity_hub import IdentityCreate, SpaceHandle
from identity_hub.crypto import associated_data
from identity_hub.enums import SessionStatus

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


async def _identity(space: SpaceHandle) -> UUID:
    created = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+1", session={"auth_key": "k0"}), actor="test"
    )
    return created.id


def _payload(space: SpaceHandle, identity_id: UUID, key: str, slot: str = "default") -> object:
    return space.hub._envelope.seal_model(
        TgSession(auth_key=key),
        associated_data("ih_sessions", identity_id, slot),  # type: ignore[arg-type]
    )


async def _current(space: SpaceHandle, identity_id: UUID) -> str:
    async with space._tx(own=True) as tx:
        found = await tx.get_session(identity_id, "default")
    assert found is not None
    model = space.hub._envelope.open_model(
        TgSession, found[0], associated_data("ih_sessions", identity_id, "default")
    )
    return model.auth_key.get_secret_value()


async def test_in_place_keeps_record_and_bumps_revision(space: SpaceHandle) -> None:
    identity_id = await _identity(space)
    async with space._tx() as tx:
        first = await tx.put_session(identity_id, "default", _payload(space, identity_id, "k1"), fencing=0)
        second = await tx.put_session(identity_id, "default", _payload(space, identity_id, "k2"), fencing=0)

    assert first is not None and second is not None
    assert second.id == first.id and second.revision == first.revision + 1 and second.replaced_id is None
    assert await _current(space, identity_id) == "k2"


async def test_replace_revokes_old_and_keeps_history(space: SpaceHandle) -> None:
    identity_id = await _identity(space)
    async with space._tx() as tx:
        old = await tx.put_session(identity_id, "default", _payload(space, identity_id, "k1"), fencing=0)
        new = await tx.put_session(
            identity_id, "default", _payload(space, identity_id, "k2"), fencing=0, mode="replace"
        )
        history = await tx.session_history(identity_id, "default")

    assert old is not None and new is not None
    assert new.id != old.id and new.replaced_id == old.id and new.revision == old.revision + 1
    assert [(row.id, row.status) for row in history] == [(old.id, "revoked"), (new.id, "active")]
    assert history[0].revoked_at is not None
    assert await _current(space, identity_id) == "k2"
    card = await space.secrets.describe(identity_id)
    assert [(s.slot, s.status) for s in card.sessions] == [("default", "active")]  # одна на слот


async def test_revoke_is_terminal_and_rollback_restores(space: SpaceHandle) -> None:
    identity_id = await _identity(space)
    async with space._tx() as tx:
        assert await tx.revoke_session(identity_id, "default")
        assert not await tx.revoke_session(identity_id, "default")
        assert await tx.get_session(identity_id, "default") is None

    with pytest.raises(RuntimeError):
        async with space._tx() as tx:
            await tx.put_session(identity_id, "default", _payload(space, identity_id, "k9"), fencing=0)
            raise RuntimeError("откат")
    async with space._tx(own=True) as tx:
        assert await tx.get_session(identity_id, "default") is None  # откат не оставил записи


async def test_stale_fencing_and_status_lifecycle(space: SpaceHandle) -> None:
    identity_id = await _identity(space)
    async with space._tx() as tx:
        assert await tx.put_session(identity_id, "default", _payload(space, identity_id, "k5"), fencing=5)
        assert (
            await tx.put_session(identity_id, "default", _payload(space, identity_id, "old"), fencing=4)
            is None
        )
        await tx.set_session_status(identity_id, "default", SessionStatus.INVALIDATED)
        found = await tx.get_session(identity_id, "default")
    assert found is not None and found[1] == "invalidated"
    assert SessionStatus.VALID is SessionStatus.ACTIVE and SessionStatus.INVALID is SessionStatus.INVALIDATED
