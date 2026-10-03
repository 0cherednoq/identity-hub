"""IMP-23 (I8.4): `space.identities` — изменение и состояние на MemoryStore и PostgreSQL."""

from __future__ import annotations

from typing import Any

import pytest

from identity_hub import (
    IdentityCreate,
    Lifecycle,
    NotFound,
    Override,
    SpaceHandle,
    Standing,
    TransitionNotAllowed,
    ValidationFailed,
)

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


async def _new(space: SpaceHandle, ref: str, **kwargs: Any) -> Any:
    return await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref=ref, **kwargs), actor="test"
    )


async def test_update_merges_in_one_event(space: SpaceHandle) -> None:
    created = await _new(
        space, "+1", profile={"country": "DE", "first_name": "Olena"}, labels={"a": 1, "b": 2}
    )

    updated = await space.identities.update(
        created.id, profile={"country": "UA"}, labels={"b": None, "c": "x"}, note="заметка", actor="test"
    )

    assert updated.profile["country"] == "UA"
    assert updated.profile["first_name"] == "Olena"
    assert updated.labels == {"a": 1, "c": "x"}
    assert updated.note == "заметка"
    assert updated.revision == created.revision + 1


async def test_update_without_changes_writes_no_event(space: SpaceHandle) -> None:
    created = await _new(space, "+2", labels={"a": 1}, note="n")

    same = await space.identities.update(created.id, labels={"a": 1}, note="n", actor="test")
    untouched = await space.identities.update(created.id, actor="test")

    assert same.revision == untouched.revision == created.revision


async def test_update_note_none_clears_but_unset_keeps(space: SpaceHandle) -> None:
    created = await _new(space, "+3", note="старая")

    kept = await space.identities.update(created.id, labels={"x": 1}, actor="test")
    cleared = await space.identities.update(created.id, note=None, actor="test")

    assert kept.note == "старая"
    assert cleared.note is None


async def test_update_profile_validated_by_schema(space: SpaceHandle) -> None:
    created = await _new(space, "+4")
    with pytest.raises(ValidationFailed):
        await space.identities.update(created.id, profile={"birthday": "не дата"}, actor="test")


async def test_replace_parts(space: SpaceHandle) -> None:
    created = await _new(space, "+5", profile={"country": "DE", "first_name": "Olena"}, labels={"a": 1})

    profile = await space.identities.replace_profile(created.id, {"country": "PL"}, actor="test")
    labels = await space.identities.replace_labels(created.id, {"z": True}, actor="test")
    attributes = await space.identities.replace_attributes(created.id, {"device": "Pixel"}, actor="test")

    assert profile.profile.get("first_name") is None and profile.profile["country"] == "PL"
    assert labels.labels == {"z": True}
    assert attributes.attributes["device"] == "Pixel"


async def test_tags_note_and_delete(space: SpaceHandle) -> None:
    created = await _new(space, "+6", tags=["a"])

    assert (await space.identities.add_tags(created.id, ["b"], actor="test")).tags == {"a", "b"}
    assert (await space.identities.remove_tags(created.id, ["a"], actor="test")).tags == {"b"}
    assert (await space.identities.set_tags(created.id, ["c", "d"], actor="test")).tags == {"c", "d"}
    assert (await space.identities.set_note(created.id, "n", actor="test")).note == "n"

    await space.identities.delete(created.id, actor="test")
    assert await space.identities.find(created.id) is None
    with pytest.raises(NotFound):
        await space.identities.delete(created.id, actor="test")


async def test_activate_retire_and_back(space: SpaceHandle) -> None:
    created = await _new(space, "+7")

    await space.identities.activate(created.id, actor="ops")
    assert (await space.identities.get(created.id)).lifecycle is Lifecycle.READY
    await space.identities.retire(created.id, actor="ops", reason="продан")
    assert (await space.identities.get(created.id)).lifecycle is Lifecycle.RETIRED
    await space.identities.activate(created.id, actor="ops", reason="вернули")
    assert (await space.identities.get(created.id)).lifecycle is Lifecycle.READY
    await space.identities.set_lifecycle(created.id, Lifecycle.RETIRED, actor="ops", reason="снова")
    assert (await space.identities.get(created.id)).lifecycle is Lifecycle.RETIRED


async def test_restore_standing_only_from_banned(space: SpaceHandle) -> None:
    created = await _new(space, "+8")

    with pytest.raises(TransitionNotAllowed):
        await space.identities.restore_standing(created.id, actor="ops", reason="нет бана")

    await space.identities.override(
        created.id, Override(region="standing", key="", state=Standing.BANNED, reason="тест", actor="admin")
    )
    assert (await space.identities.get(created.id)).standing is Standing.BANNED
    await space.identities.restore_standing(created.id, actor="ops", reason="апелляция")
    assert (await space.identities.get(created.id)).standing is Standing.GOOD


async def test_override_pin_and_unpin(space: SpaceHandle) -> None:
    from datetime import timedelta

    created = await _new(space, "+9")
    await space.identities.override(
        created.id,
        Override(
            region="standing",
            key="",
            state=Standing.LIMITED,
            reason="r",
            actor="admin",
            pin_for=timedelta(hours=1),
        ),
    )
    decision = await space.identities.unpin(created.id, "standing", "", actor="admin", reason="снять")
    assert decision.state.pinned_until is None
    assert (await space.identities.get(created.id)).standing is Standing.LIMITED
