"""IMP-23 (I8.3): `space.identities.create/ensure/create_many` на MemoryStore и PostgreSQL."""

from __future__ import annotations

from uuid import uuid4

import pytest

from identity_hub import (
    AlreadyExists,
    AuthState,
    IdentityCreate,
    IdentityHub,
    Lifecycle,
    OnConflict,
    SpaceHandle,
    ValidationFailed,
)

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


def _spec(ref: str, **kwargs: object) -> IdentityCreate:
    return IdentityCreate(service="fake_tg", external_ref=ref, **kwargs)  # type: ignore[arg-type]


async def test_create_returns_identity_and_rejects_duplicate(space: SpaceHandle) -> None:
    identity = await space.identities.create(
        _spec("+1", session={"auth_key": "k"}, lifecycle=Lifecycle.READY, tags=["a"])
    )

    assert identity.external_ref == "+1"
    assert identity.lifecycle is Lifecycle.READY
    assert identity.auth is AuthState.AUTHENTICATED
    assert identity.tags == frozenset({"a"})

    with pytest.raises(AlreadyExists) as exc:
        await space.identities.create(_spec("+1"))
    assert exc.value.existing_id == identity.id
    with pytest.raises(AlreadyExists):
        await space.identities.create(_spec("+other", id=identity.id))


async def test_ensure_returns_existing_without_changes(space: SpaceHandle) -> None:
    first, created = await space.identities.ensure(_spec("+2", note="первая"))
    again, created_again = await space.identities.ensure(_spec("+2", note="вторая"))

    assert created is True and created_again is False
    assert again.id == first.id
    assert again.note == "первая"


async def test_create_in_other_space_conflicts(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await space.identities.create(_spec("+3"))
    other = hub_under_test.space(f"o-{uuid4().hex[:8]}")

    with pytest.raises(AlreadyExists):
        await other.identities.ensure(_spec("+3"))
    with pytest.raises(AlreadyExists):
        await other.identities.create_many([_spec("+3")], on_conflict="skip")


async def test_session_and_default_slot_together_rejected(space: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed):
        await space.identities.create(
            _spec("+4", session={"auth_key": "a"}, sessions={"default": {"auth_key": "b"}})
        )


async def test_create_many_creates_in_input_order_with_links(space: SpaceHandle) -> None:
    target_id = uuid4()
    specs = [
        _spec("+z", links={"recovery": target_id}),
        _spec("+a", id=target_id, lifecycle="ready"),
        _spec("+m", tags=["x"]),
    ]

    result = await space.identities.create_many(specs)

    assert [i.external_ref for i in result.created] == ["+z", "+a", "+m"]
    assert result.skipped == []
    assert result.created[1].id == target_id
    card = await space.identities.card(result.created[0].id)
    assert [(link.role, link.identity_id) for link in card.links.outgoing] == [("recovery", target_id)]


async def test_create_many_is_atomic_on_invalid_record(space: SpaceHandle) -> None:
    specs = [_spec("+ok"), _spec("", note="пустой ref"), _spec("+bad", lifecycle="nope")]

    with pytest.raises(ValidationFailed) as exc:
        await space.identities.create_many(specs)

    message = str(exc.value)
    assert "[1]" in message and "[2]" in message
    assert await space.identities.count() == 0


async def test_create_many_rejects_duplicates_in_batch(space: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed, match="повтор"):
        await space.identities.create_many([_spec("+d"), _spec("+d")])
    assert await space.identities.count() == 0


async def test_create_many_fail_on_existing_creates_nothing(space: SpaceHandle) -> None:
    await space.identities.create(_spec("+e1"))

    with pytest.raises(AlreadyExists, match=r"fake_tg:\+e1"):
        await space.identities.create_many([_spec("+new"), _spec("+e1")], on_conflict=OnConflict.FAIL)

    assert await space.identities.find(service="fake_tg", external_ref="+new") is None


async def test_create_many_skip_reports_existing(space: SpaceHandle) -> None:
    existing = await space.identities.create(_spec("+s1"))

    result = await space.identities.create_many([_spec("+s1"), _spec("+s2")], on_conflict="skip")

    assert [i.external_ref for i in result.created] == ["+s2"]
    assert [(r.id, r.external_ref) for r in result.skipped] == [(existing.id, "+s1")]


async def test_create_many_limit(space: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed, match="1000"):
        await space.identities.create_many(_spec(f"+{n}") for n in range(1001))
    assert (await space.identities.create_many([])).created == []


async def test_register_alias_keeps_result_and_warns(space: SpaceHandle) -> None:
    with pytest.warns(DeprecationWarning, match=r"space\.identities\.ensure"):
        first = await space.register(service="fake_tg", external_ref="+r")
    with pytest.warns(DeprecationWarning):
        again = await space.register(service="fake_tg", external_ref="+r")

    assert first.created is True and again.created is False
    assert again.identity_id == first.identity_id
