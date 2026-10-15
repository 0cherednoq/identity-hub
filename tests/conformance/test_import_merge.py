"""S9.3: накопительный импорт `OnExisting.MERGE`."""

from __future__ import annotations

import pytest

from identity_hub import IdentityCreate, ImportRecord, OnExisting, SecretRef, SpaceHandle

pytestmark = pytest.mark.conformance


async def _existing(space: SpaceHandle, ref: str) -> object:
    return await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref=ref,
            lifecycle="ready",
            credentials={"phone": ref, "password": "original"},
            session={"auth_key": "original-session"},
        ),
        actor="test",
    )


async def test_merge_covers_credential_and_session_duplicate_matrix(space: SpaceHandle) -> None:
    # Дубль credentials + новый слот session.
    first = await _existing(space, "merge-1")
    result = await space.imports.run(
        service="fake_tg",
        records=[
            ImportRecord(
                "merge-1",
                credentials={"password": "original", "phone": "merge-1"},
                sessions={"web": {"auth_key": "web-session"}},
            )
        ],
        on_existing=OnExisting.MERGE,
        actor="test",
    )
    assert result.updated == [first.id] and result.skipped == []
    card = await space.secrets.describe(first.id)
    assert len(card.credential_records) == 1
    assert {session.slot for session in card.sessions} == {"default", "web"}

    # Новая пара + дубль session.
    second = await _existing(space, "merge-2")
    result = await space.imports.run(
        service="fake_tg",
        records=[
            ImportRecord(
                "merge-2",
                credentials={"phone": "merge-2", "password": "candidate"},
                sessions={"default": {"dc_id": 2, "auth_key": "original-session"}},
            )
        ],
        on_existing="merge",
        actor="test",
    )
    assert result.updated == [second.id]
    records = (await space.secrets.describe(second.id)).credential_records
    assert len(records) == 2
    assert {record.name for record in records} >= {"default"}
    assert any(record.name.startswith("import-") for record in records)

    # Новая пара + новый слот session.
    third = await _existing(space, "merge-3")
    result = await space.imports.run(
        service="fake_tg",
        records=[
            ImportRecord(
                "merge-3",
                credentials={"phone": "merge-3", "password": "candidate"},
                sessions={"web": {"auth_key": "another-web"}},
            )
        ],
        on_existing="merge",
        actor="test",
    )
    assert result.updated == [third.id]
    card = await space.secrets.describe(third.id)
    assert len(card.credential_records) == 2
    assert {session.slot for session in card.sessions} == {"default", "web"}

    # Полный дубль ничего не меняет.
    fourth = await _existing(space, "merge-4")
    result = await space.imports.run(
        service="fake_tg",
        records=[
            ImportRecord(
                "merge-4",
                credentials={"phone": "merge-4", "password": "original"},
                sessions={"default": {"auth_key": "original-session"}},
            )
        ],
        on_existing="merge",
        actor="test",
    )
    assert result.updated == []
    assert result.skipped and result.skipped[0][0] == "merge-4"
    assert len((await space.secrets.describe(fourth.id)).credential_records) == 1


async def test_merge_keeps_active_session_by_default_and_replace_overrides(space: SpaceHandle) -> None:
    identity = await _existing(space, "merge-session-policy")

    kept = await space.imports.run(
        service="fake_tg",
        records=[
            ImportRecord(
                "merge-session-policy",
                sessions={"default": {"auth_key": "unverified"}},
            )
        ],
        on_existing="merge",
        actor="test",
    )
    assert kept.updated == [] and kept.skipped
    assert kept.merge_details == [("merge-session-policy", "session default: активная сохранена")]
    current = await space.secrets.reveal(
        identity.id,
        SecretRef.session(),
        actor="test",
        reason="merge assertion",
    )
    assert current["auth_key"] == "original-session"

    replaced = await space.imports.run(
        service="fake_tg",
        records=[
            ImportRecord(
                "merge-session-policy",
                sessions={"default": {"auth_key": "replacement"}},
            )
        ],
        on_existing="merge",
        merge_sessions="replace",
        actor="test",
    )
    assert replaced.updated == [identity.id]
    current = await space.secrets.reveal(
        identity.id,
        SecretRef.session(),
        actor="test",
        reason="merge assertion",
    )
    assert current["auth_key"] == "replacement"
    async with space._tx(own=True) as tx:
        history = await tx.session_history(identity.id, "default")
    assert [row.status for row in history] == ["revoked", "active"]


async def test_merge_replaces_invalidated_session_without_changing_identity_state(
    space: SpaceHandle,
) -> None:
    identity = await _existing(space, "merge-invalidated")
    before = await space.identities.get(identity.id)
    async with space._tx(own=True) as tx:
        await tx.set_session_status(identity.id, "default", "invalidated")

    report = await space.imports.run(
        service="fake_tg",
        records=[
            ImportRecord(
                "merge-invalidated",
                sessions={"default": {"auth_key": "recovered"}},
            )
        ],
        on_existing="merge",
        actor="test",
    )

    assert report.updated == [identity.id]
    after = await space.identities.get(identity.id)
    assert (after.lifecycle, after.standing, after.auth) == (
        before.lifecycle,
        before.standing,
        before.auth,
    )
    async with space._tx(own=True) as tx:
        history = await tx.session_history(identity.id, "default")
    assert [row.status for row in history] == ["revoked", "active"]
