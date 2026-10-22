"""C8.2 (`D-326`): отбор аккаунтов с непроверенными credentials и сессиями."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from identity_hub import IdentityCreate, IdentityHub, Lifecycle, SpaceHandle
from identity_hub.read.search import AccountSearchOptions

pytestmark = [pytest.mark.conformance]


async def _identity(space: SpaceHandle, suffix: str) -> UUID:
    created = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref=f"+{uuid4().hex[:8]}{suffix}",
            credentials={"phone": f"+{suffix}", "password": "p"},
            session={"auth_key": "k"},
            lifecycle=Lifecycle.READY,
        ),
        actor="test",
    )
    return created.id


async def _ids(space: SpaceHandle, **options: object) -> set[UUID]:
    page = await space.admin.identity_page(
        options=AccountSearchOptions(**options),  # type: ignore[arg-type]
        limit=50,
    )
    return {item.id for item in page.items}


async def test_credential_filter_selects_unchecked_records(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    fresh = await _identity(space, "1")
    checked = await _identity(space, "2")
    failed = await _identity(space, "3")

    await space.secrets.verify_credential(checked, actor="ops")
    await space.secrets.reject_credential(failed, actor="ops", code="bad_password")

    assert await _ids(space, credential_unverified=True) == {fresh}
    assert await _ids(space, credential_unverified=False) == {checked, failed}


async def test_session_filter_selects_unchecked_records(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    fresh = await _identity(space, "4")
    checked = await _identity(space, "5")

    await space.secrets.verify_session(checked, actor="ops")

    assert await _ids(space, session_unverified=True) == {fresh}
    assert await _ids(space, session_unverified=False) == {checked}


async def test_filters_combine_and_ignore_revoked_or_expired(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    both = await _identity(space, "6")
    revoked = await _identity(space, "7")

    # отозванная запись больше не ждёт проверки
    await space.secrets.revoke_credential(revoked, "default", actor="ops")
    await space.secrets.revoke_session(revoked, actor="ops")

    assert await _ids(space, credential_unverified=True, session_unverified=True) == {both}
    assert await _ids(space, credential_unverified=False) == {revoked}
    assert await _ids(space, session_unverified=False) == {revoked}
