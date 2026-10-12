"""IMP-10 (I3.3b): режимы in_place и replace в API, события без данных сессии (D-219)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from identity_hub import IdentityCreate, SpaceHandle, ValidationFailed
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.conformance]


async def _identity(space: SpaceHandle) -> UUID:
    created = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+1", lifecycle="ready", session={"auth_key": "k0"}),
        actor="test",
    )
    return created.id


async def _history(space: SpaceHandle, identity_id: UUID) -> list[Any]:
    async with space._tx(own=True) as tx:
        return await tx.session_history(identity_id, "default")


async def _events(space: SpaceHandle, identity_id: UUID, event_type: str) -> list[Any]:
    page = await space.admin.event_page(
        filters=EventFilters(type=event_type, identity_id=identity_id, include_journal=True)
    )
    return sorted(page.items, key=lambda item: (item.occurred_at, item.event_id))  # UUIDv7 — порядок записи


async def test_lease_save_in_place_and_replace(space: SpaceHandle) -> None:
    identity_id = await _identity(space)
    lease = await space.pools.define("work", service="fake_tg", holder="w").acquire()
    (original,) = await _history(space, identity_id)

    await lease.save_session({"auth_key": "k1"})
    await lease.save_session({"auth_key": "k2"}, mode="replace")
    await lease.release()

    history = await _history(space, identity_id)
    assert [(row.status, row.revision) for row in history] == [("revoked", 2), ("active", 3)]
    assert history[0].id == original.id
    saved = await _events(space, identity_id, "session.saved")
    assert [(e.data["mode"], e.data["session_revision"]) for e in saved] == [("in_place", 2), ("replace", 3)]
    assert saved[1].data["replaced_id"] == str(original.id) and saved[1].data["session_id"] == str(
        history[1].id
    )
    assert all("k1" not in repr(e) and "k2" not in repr(e) for e in saved)


async def test_secrets_replace_and_revoke(space: SpaceHandle) -> None:
    identity_id = await _identity(space)

    await space.secrets.replace_session(identity_id, {"auth_key": "k1"}, actor="ops", mode="replace")
    assert [row.status for row in await _history(space, identity_id)] == ["revoked", "active"]
    assert await space.secrets.revoke_session(identity_id, "default", actor="ops", reason="утечка")
    assert not await space.secrets.revoke_session(identity_id, "default", actor="ops")
    assert (await space.secrets.describe(identity_id)).sessions == []

    updates = await _events(space, identity_id, "identity.session.updated")
    assert [e.data["mode"] for e in updates] == ["replace", "revoke"]
    with pytest.raises(ValidationFailed):
        await space.secrets.replace_session(identity_id, {"auth_key": "k3"}, actor="ops", mode="merge")
