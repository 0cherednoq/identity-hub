"""N2.3: атомарные массовые изменения identity."""

from __future__ import annotations

from uuid import UUID

import pytest

from identity_hub import (
    IdentityCreate,
    IdentityFilter,
    Lifecycle,
    NotFound,
    SpaceHandle,
    TransitionNotAllowed,
    ValidationFailed,
)

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


async def _new(
    space: SpaceHandle,
    identity_id: UUID,
    ref: str,
    *,
    lifecycle: Lifecycle = Lifecycle.PROVISIONING,
    tags: tuple[str, ...] = (),
) -> None:
    await space.identities.create(
        IdentityCreate(
            id=identity_id,
            service="fake_tg",
            external_ref=ref,
            lifecycle=lifecycle,
            tags=tags,
        ),
        actor="test",
    )


async def test_update_many_by_ids_changes_each_identity_once(space: SpaceHandle) -> None:
    first, second = UUID(int=101), UUID(int=102)
    await _new(space, first, "+bulk-1", tags=("old", "drop"))
    await _new(space, second, "+bulk-2", tags=("drop",))
    before = {item.id: item.revision for item in (await space.identities.list()).items}

    result = await space.identities.update_many(
        [second, first],
        add_tags=["new", "drop"],
        remove_tags=["drop"],
        set_labels={"batch": 1},
        lifecycle=Lifecycle.READY,
        actor="ops",
    )

    assert (result.changed, result.skipped) == (2, 0)
    for identity_id in (first, second):
        identity = await space.identities.get(identity_id)
        assert identity.tags == ({"old", "new"} if identity_id == first else {"new"})
        assert identity.labels == {"batch": 1}
        assert identity.lifecycle is Lifecycle.READY
        assert identity.revision == before[identity_id] + 1


async def test_update_many_filter_enforces_limit_before_writes(space: SpaceHandle) -> None:
    first, second = UUID(int=201), UUID(int=202)
    await _new(space, first, "+filter-1", tags=("batch",))
    await _new(space, second, "+filter-2", tags=("batch",))

    with pytest.raises(ValidationFailed, match="больше 1"):
        await space.identities.update_many(
            IdentityFilter(tags=["batch"]), add_tags=["changed"], actor="ops", limit=1
        )

    assert "changed" not in (await space.identities.get(first)).tags
    assert "changed" not in (await space.identities.get(second)).tags

    result = await space.identities.update_many(
        IdentityFilter(tags=["batch"]), add_tags=["changed"], actor="ops", limit=2
    )
    assert (result.changed, result.skipped) == (2, 0)


async def test_update_many_invalid_transition_rolls_back_whole_batch(space: SpaceHandle) -> None:
    valid, invalid = UUID(int=301), UUID(int=302)
    await _new(space, valid, "+atomic-1")
    await _new(space, invalid, "+atomic-2", lifecycle=Lifecycle.RETIRED)

    with pytest.raises(TransitionNotAllowed, match=r"reason|причин"):
        await space.identities.update_many(
            [valid, invalid], lifecycle=Lifecycle.READY, add_tags=["should-rollback"], actor="ops"
        )

    first = await space.identities.get(valid)
    assert first.lifecycle is Lifecycle.PROVISIONING
    assert "should-rollback" not in first.tags


async def test_update_many_missing_id_rolls_back_and_noop_is_skipped(space: SpaceHandle) -> None:
    existing, missing = UUID(int=401), UUID(int=402)
    await _new(space, existing, "+missing", tags=("same",))

    with pytest.raises(NotFound):
        await space.identities.update_many([existing, missing], add_tags=["new"], actor="ops")
    assert (await space.identities.get(existing)).tags == {"same"}

    result = await space.identities.update_many([existing], add_tags=["same"], actor="ops")
    assert (result.changed, result.skipped) == (0, 1)


async def test_update_many_rejects_empty_change_and_oversized_id_list(space: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed, match="хотя бы одно"):
        await space.identities.update_many([], actor="ops")
    with pytest.raises(ValidationFailed, match="больше 1"):
        await space.identities.update_many(
            [UUID(int=501), UUID(int=502)], add_tags=["x"], actor="ops", limit=1
        )
