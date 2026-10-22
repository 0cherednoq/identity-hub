"""C8.1 (`D-326`): пометка валидности под `space.using(session)` — одна фиксация с событием."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub import IdentityCreate
from identity_hub.hub import SpaceHandle
from identity_hub.postgres.models import CredentialsRecord, EventRecord

pytestmark = pytest.mark.pg


async def _events(sessionmaker: async_sessionmaker[AsyncSession], event_type: str) -> int:
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(EventRecord).where(EventRecord.type == event_type)
                )
            ).scalar_one()
        )


async def test_marks_roll_back_with_user_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="+79990000111",
            credentials={"phone": "+79990000111", "password": "p"},
            session={"auth_key": "k"},
        ),
        actor="test",
    )
    before = await _events(sessionmaker, "identity.credentials.updated")

    async with sessionmaker() as user:
        await user.begin()
        secrets = space.using(user).secrets

        marked = await secrets.reject_credential(identity.id, actor="ops", code="bad_password")
        assert marked is not None and marked.reject_code == "bad_password"
        # пометка видна в транзакции приложения до её фиксации
        codes = (
            await user.execute(
                select(CredentialsRecord.reject_code).where(CredentialsRecord.identity_id == identity.id)
            )
        ).scalars()
        assert list(codes) == ["bad_password"]
        assert await secrets.reject_session(identity.id, actor="ops", code="session_invalid")

        await user.rollback()

    # откат приложения убрал и пометку, и событие
    assert [c.reject_code for c in await space.secrets.credentials(identity.id)] == [None]
    card = await space.secrets.describe(identity.id)
    assert [s.reject_code for s in card.sessions] == [None]
    assert await _events(sessionmaker, "identity.credentials.updated") == before


async def test_marks_commit_with_user_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="+79990000222",
            credentials={"phone": "+79990000222", "password": "p"},
            session={"auth_key": "k"},
        ),
        actor="test",
    )
    async with sessionmaker() as user, user.begin():
        secrets = space.using(user).secrets
        assert await secrets.verify_credential(identity.id, actor="ops") is not None
        assert await secrets.verify_session(identity.id, actor="ops")

    records = await space.secrets.credentials(identity.id)
    assert records[0].last_verified_at is not None and records[0].reject_code is None
    card = await space.secrets.describe(identity.id)
    assert [s.last_verified_at is not None for s in card.sessions] == [True]
