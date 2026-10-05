"""IMP-07 (I1.2a): ревизия агрегата identity растёт один раз на команду (D-205)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from identity_hub import (
    IdentityCreate,
    IdentityHub,
    Lifecycle,
    Override,
    RevisionConflict,
    SpaceHandle,
    Standing,
)
from identity_hub.events import CATALOG
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]

FAR_PAST = datetime(2000, 1, 1, tzinfo=UTC)
FAR_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)


async def _create(space: SpaceHandle, ref: str) -> Any:
    return await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref=ref,
            lifecycle="ready",
            credentials={"phone": ref, "password": "p"},
            session={"auth_key": "k"},
        ),
        actor="test",
    )


async def _revision(space: SpaceHandle, identity_id: UUID) -> int:
    return (await space.identities.get(identity_id)).revision


async def _events_of(space: SpaceHandle, identity_id: UUID) -> list[Any]:
    page = await space.admin.event_page(
        filters=EventFilters(identity_id=identity_id, include_journal=True), limit=200
    )
    return sorted(page.items, key=lambda item: item.sequence or 0)


async def test_new_identity_starts_at_revision_one(space: SpaceHandle) -> None:
    created = await _create(space, "+1")

    assert created.revision == 1
    assert (await space.identities.card(created.id)).revision == 1
    (registered,) = [e for e in await _events_of(space, created.id) if e.type == "identity.registered"]
    assert registered.revision == 1


async def test_each_command_bumps_revision_once(space: SpaceHandle) -> None:
    created = await _create(space, "+2")
    commands: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
        ("update", lambda: space.identities.update(created.id, labels={"a": 1}, actor="ops")),
        ("add_tags", lambda: space.identities.add_tags(created.id, ["vip", "new"], actor="ops")),
        ("set_note", lambda: space.identities.set_note(created.id, "заметка", actor="ops")),
        ("retire", lambda: space.identities.retire(created.id, actor="ops", reason="пауза")),
        ("activate", lambda: space.identities.activate(created.id, actor="ops", reason="назад")),
        (
            "override",
            lambda: space.identities.override(
                created.id,
                Override(region="standing", key="", state=Standing.BANNED, reason="тест", actor="admin"),
            ),
        ),
        (
            "restore_standing",
            lambda: space.identities.restore_standing(created.id, actor="ops", reason="апелляция"),
        ),
        (
            "replace_credentials",
            lambda: space.secrets.replace_credentials(
                created.id, {"phone": "+2", "password": "new"}, actor="ops"
            ),
        ),
        (
            "replace_session",
            lambda: space.secrets.replace_session(created.id, {"auth_key": "k2"}, actor="ops"),
        ),
    ]
    revision = created.revision
    for name, command in commands:
        before = {e.event_id for e in await _events_of(space, created.id)}
        await command()
        after = await _revision(space, created.id)
        assert after == revision + 1, name
        fresh = [e for e in await _events_of(space, created.id) if e.event_id not in before]
        assert fresh, name
        assert {e.revision for e in fresh} == {after}, name  # все события команды — с одной ревизией
        revision = after


async def test_unchanged_command_keeps_revision(space: SpaceHandle) -> None:
    created = await _create(space, "+3")
    await space.identities.update(created.id, labels={"a": 1}, actor="ops")
    revision = await _revision(space, created.id)

    await space.identities.update(created.id, labels={"a": 1}, actor="ops")
    await space.identities.add_tags(created.id, [], actor="ops")

    assert await _revision(space, created.id) == revision


async def test_reveal_and_lease_do_not_bump_revision(space: SpaceHandle) -> None:
    created = await _create(space, "+4")
    revision = created.revision

    await space.secrets.reveal(created.id, "credentials", actor="admin", reason="проверка")
    lease = await space.pools.define("work", service="fake_tg", holder="w").acquire()
    assert lease.identity_id == created.id
    await lease.release()

    assert await _revision(space, created.id) == revision
    events = await _events_of(space, created.id)
    assert {"identity.secret.revealed", "lease.acquired", "lease.released"} <= {e.type for e in events}
    assert all(e.revision == revision for e in events)


async def test_session_saved_under_lease_bumps_revision(space: SpaceHandle) -> None:
    created = await _create(space, "+5")
    lease = await space.pools.define("work", service="fake_tg", holder="w").acquire()

    await lease.save_session({"auth_key": "k2"})
    await lease.release()

    assert await _revision(space, created.id) == created.revision + 1


async def test_lifecycle_change_reaches_events_and_store_events(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    created = await _create(space, "+6")
    await space.identities.set_lifecycle(created.id, Lifecycle.RETIRED, actor="ops", reason="продан")

    events = await hub_under_test._store.rollup_source_events(space.slug, FAR_PAST, FAR_FUTURE)
    changed = [e for e in events if e.type == "identity.lifecycle.changed" and e.identity_id == created.id]
    assert [e.revision for e in changed] == [2]
    assert changed[0].to_dict()["revision"] == 2


async def test_lock_identity_revision_raises_revision_conflict(space: SpaceHandle) -> None:
    created = await _create(space, "+7")
    await space.identities.add_tags(created.id, ["x"], actor="ops")

    with pytest.raises(RevisionConflict) as caught:
        async with space._tx() as tx:
            await tx.lock_identity_revision(created.id, created.revision)

    assert caught.value.expected_version == created.revision
    assert caught.value.actual_version == created.revision + 1
    async with space._tx() as tx:
        assert await tx.lock_identity_revision(created.id, created.revision + 1) == created.revision + 1


def test_aggregate_flag_in_catalog() -> None:
    aggregate = {t for t, spec in CATALOG.items() if spec.aggregate}

    assert "identity.updated" in aggregate and "session.saved" in aggregate
    assert not {"identity.secret.revealed", "identity.quota.exhausted", "identity.state.drift"} & aggregate
    assert not {t for t in aggregate if t.startswith(("lease.", "operation.", "proxy."))}
