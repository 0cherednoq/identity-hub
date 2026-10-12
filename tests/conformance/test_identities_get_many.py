"""C3.5: состояния аккаунтов пачкой через API — работает на обоих хранилищах."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from tests.fixtures.api import register

from identity_hub import AuthState, IdentityHub, Lifecycle, Override, SpaceHandle, Standing, ValidationFailed
from identity_hub.api.batch import MAX_BATCH

pytestmark = pytest.mark.conformance


async def _account(space: SpaceHandle, ref: str, **fields: object) -> UUID:
    identity_id: UUID = (await register(space, service="fake_tg", external_ref=ref, **fields)).identity_id
    return identity_id


async def test_get_many_returns_states_by_id(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """C3.5: состояния регионов аккаунтов одним вызовом; чужие и неизвестные id пропускаются."""
    fresh = await _account(space, "+1")
    ready = await _account(space, "+2", lifecycle="ready", session={"auth_key": "k"})
    banned = await _account(space, "+3", tags=["вип"])
    await space.identities.override(banned, Override("standing", "", "banned", reason="тест", actor="op"))
    other = hub_under_test.space(f"{space.slug}-other", create=True)
    foreign = await _account(other, "+9")

    found = await space.identities.get_many([fresh, ready, banned, foreign, uuid4(), ready])

    assert set(found) == {fresh, ready, banned}
    assert (found[fresh].lifecycle, found[fresh].auth) == (Lifecycle.PROVISIONING, AuthState.NO_SESSION)
    assert (found[ready].lifecycle, found[ready].auth) == (Lifecycle.READY, AuthState.AUTHENTICATED)
    assert (found[banned].standing, found[banned].tags) == (Standing.BANNED, frozenset({"вип"}))
    assert found[ready] == await space.identities.get(ready)
    assert await space.identities.get_many([]) == {}
    with pytest.raises(ValidationFailed):
        await space.identities.get_many([uuid4() for _ in range(MAX_BATCH + 1)])
