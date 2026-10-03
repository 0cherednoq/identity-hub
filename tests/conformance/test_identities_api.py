"""IMP-23 (I8.2): `space.identities` — чтение; одинаково на MemoryStore и PostgreSQL."""

from __future__ import annotations

import warnings
from dataclasses import asdict
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.fixtures.services import FAKE_TG

from identity_hub import (
    AuthState,
    Identity,
    IdentityCreate,
    IdentityFilter,
    IdentityHub,
    Lifecycle,
    NotFound,
    SpaceHandle,
    ValidationFailed,
)

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


async def _add(space: SpaceHandle, ref: str, **kwargs: Any) -> UUID:
    identity = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref=ref, **kwargs), actor="test"
    )
    return identity.id


async def test_get_returns_secret_free_identity(space: SpaceHandle) -> None:
    identity_id = await _add(
        space,
        "+100",
        credentials={"phone": "+100", "password": "hunter2"},
        session={"auth_key": "top-secret"},
        profile={"country": "DE"},
        labels={"farm": "eu-1"},
        tags=["parser"],
        note="заметка",
    )

    identity = await space.identities.get(identity_id)

    assert isinstance(identity, Identity)
    assert identity.id == identity_id
    assert (identity.service, identity.external_ref) == ("fake_tg", "+100")
    assert identity.lifecycle is Lifecycle.PROVISIONING
    assert identity.auth is AuthState.AUTHENTICATED
    assert identity.tags == frozenset({"parser"})
    assert identity.labels == {"farm": "eu-1"}
    assert identity.profile["country"] == "DE"
    assert identity.revision >= 1
    assert identity.ref.external_ref == "+100"
    dumped = repr(identity) + repr(asdict(identity))
    assert "hunter2" not in dumped and "top-secret" not in dumped


async def test_get_missing_raises_and_find_returns_none(space: SpaceHandle) -> None:
    missing = uuid4()
    with pytest.raises(NotFound):
        await space.identities.get(missing)
    assert await space.identities.find(missing) is None
    assert await space.identities.find(service="fake_tg", external_ref="nobody") is None
    with pytest.raises(NotFound):
        await space.identities.get_by_ref("fake_tg", "nobody")


async def test_other_space_identity_is_invisible(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    identity_id = await _add(space, "+200")
    other = hub_under_test.space(f"o-{uuid4().hex[:8]}", create=True)

    assert await other.identities.find(identity_id) is None
    assert await other.identities.find(service="fake_tg", external_ref="+200") is None
    with pytest.raises(NotFound):
        await other.identities.get(identity_id)


async def test_find_and_get_by_ref(space: SpaceHandle) -> None:
    identity_id = await _add(space, "+300")

    found = await space.identities.find(service=FAKE_TG, external_ref="+300")
    assert found is not None and found.id == identity_id
    assert (await space.identities.get_by_ref("fake_tg", "+300")).id == identity_id


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"service": "fake_tg"}, {"external_ref": "+1"}],
    ids=["nothing", "service-only", "ref-only"],
)
async def test_find_requires_id_or_full_ref(space: SpaceHandle, kwargs: dict[str, str]) -> None:
    with pytest.raises(ValidationFailed):
        await space.identities.find(**kwargs)  # type: ignore[arg-type]


async def test_find_rejects_id_together_with_ref(space: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed):
        await space.identities.find(uuid4(), service="fake_tg", external_ref="+1")


async def test_card_is_full_identity_card(space: SpaceHandle) -> None:
    identity_id = await _add(space, "+400")
    card = await space.identities.card(identity_id)
    assert card.identity.id == identity_id
    assert card.eligible is False  # provisioning не выдаётся в аренду


async def test_list_and_search_agree(space: SpaceHandle) -> None:
    await _add(space, "+501", profile={"country": "DE"}, labels={"farm": "eu-1"}, tags=["parser", "warm"])
    await _add(space, "+502", profile={"country": "DE"}, labels={"farm": "eu-2"}, tags=["parser"])
    await _add(space, "+503", profile={"country": "US"}, labels={"farm": "eu-1"}, tags=["parser"])
    await _add(space, "+504", lifecycle=Lifecycle.READY, tags=["parser"])

    where = IdentityFilter(
        service="fake_tg", tags=["parser"], labels={"farm": "eu-1"}, profile={"country": "DE"}
    )
    listed = await space.identities.list(where)
    searched = await space.identities.search("service:fake_tg tag:parser farm:eu-1 country:DE")

    assert [i.external_ref for i in listed.items] == ["+501"]
    assert [i.external_ref for i in searched.items] == ["+501"]
    assert await space.identities.count(where) == 1

    ready = await space.identities.list(IdentityFilter(lifecycle=["ready"]))
    assert [i.external_ref for i in ready.items] == ["+504"]
    assert await space.identities.count() == 4


async def test_list_pages_with_cursor(space: SpaceHandle) -> None:
    for n in range(5):
        await _add(space, f"+60{n}")

    first = await space.identities.list(limit=2, count=True)
    second = await space.identities.list(limit=2, after=first.next_cursor)
    third = await space.identities.list(limit=2, after=second.next_cursor)

    refs = [i.external_ref for page in (first, second, third) for i in page.items]
    assert sorted(refs) == [f"+60{n}" for n in range(5)]
    assert first.total == 5
    assert second.total is None
    assert third.next_cursor is None


async def test_filter_rejects_unknown_profile_and_ambiguous_label(space: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed):
        await space.identities.list(IdentityFilter(profile={"first_name": "Olena"}))
    with pytest.raises(ValidationFailed):
        await space.identities.list(IdentityFilter(labels={"country": "DE"}))


async def test_namespace_calls_do_not_warn(space: SpaceHandle) -> None:
    identity_id = await _add(space, "+800")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        await space.identities.card(identity_id)
        await space.identities.search("+800")
        await space.identities.list()
