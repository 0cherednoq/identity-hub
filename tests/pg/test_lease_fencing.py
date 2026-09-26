"""P3.4: fencing, секреты под арендой, отзыв (ТЗ FR-5.2, 5.3, 5.6, 6.9, G-1.4)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import LeaseLost, StaleFencing, ValidationFailed
from identity_hub.hub import SpaceHandle
from identity_hub.pool import Pool
from identity_hub.postgres.models import EventRecord, IdentityRecord, LeaseHistoryRecord, LeaseRecord
from tests.fixtures.services import TgCredentials, TgSession

pytestmark = pytest.mark.pg


def fast_pool(space: SpaceHandle) -> Pool:
    return space.pool(
        "p",
        service="fake_tg",
        ttl=timedelta(seconds=1.5),
        heartbeat=timedelta(seconds=0.3),
        safety_margin=timedelta(seconds=0.5),
    )


async def _ready(space: SpaceHandle, **kwargs: object) -> None:
    await space.register(
        service="fake_tg",
        external_ref="+1",
        lifecycle="ready",
        credentials=TgCredentials(phone="+1", password=SecretStr("pw")),
        session={"auth_key": "old"},
        **kwargs,  # type: ignore[arg-type]
    )


async def test_secrets_only_via_lease(space: SpaceHandle) -> None:
    await _ready(space)
    async with fast_pool(space).lease() as lease:
        creds = await lease.credentials()
        assert isinstance(creds, TgCredentials) and creds.password is not None
        assert creds.password.get_secret_value() == "pw"
        session = await lease.session()
        assert isinstance(session, TgSession) and session.auth_key.get_secret_value() == "old"
        assert await lease.session("backup") is None
        await lease.save_session(TgSession(auth_key=SecretStr("new"), dc_id=5))
        updated = await lease.session()
        assert isinstance(updated, TgSession) and updated.auth_key.get_secret_value() == "new"
        with pytest.raises(ValidationFailed):
            await lease.save_session({"dc_id": 1})


async def test_zombie_after_expiry_cannot_write(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _ready(space)
    pool = fast_pool(space)
    zombie = await pool.acquire()  # без heartbeat
    async with sessionmaker() as s, s.begin():
        await s.execute(update(LeaseRecord).values(expires_at=text("now() - interval '1 second'")))
    fresh = await pool.acquire()
    assert fresh.fencing == zombie.fencing + 1
    with pytest.raises(StaleFencing):
        await zombie.save_session({"auth_key": "zombie"})
    with pytest.raises(StaleFencing):
        await zombie.mark_authenticated()
    await fresh.save_session({"auth_key": "fresh"})
    session = await fresh.session()
    assert isinstance(session, TgSession) and session.auth_key.get_secret_value() == "fresh"


async def test_mark_authenticated(space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await space.register(service="fake_tg", external_ref="+1", lifecycle="ready")
    pool = fast_pool(space)
    lease = await pool.acquire(for_operation="probe")  # без сессии аренда только для проб/входа
    await lease.save_session({"auth_key": "k"})
    await lease.mark_authenticated()
    async with sessionmaker() as s:
        assert (await s.execute(select(IdentityRecord.auth_state))).scalar_one() == "authenticated"
        types = list((await s.execute(select(EventRecord.type).order_by(EventRecord.id))).scalars())
    assert "session.saved" in types and "identity.auth.changed" in types


async def test_revoke_from_admin(space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await _ready(space)
    async with sessionmaker() as s:
        identity_id = (await s.execute(select(IdentityRecord.id))).scalar_one()
    with pytest.raises(ValidationFailed):
        await space.revoke_lease(identity_id, reason="", actor="admin")
    assert not await space.revoke_lease(identity_id, reason="нет аренды", actor="admin")

    with pytest.raises(LeaseLost, match="revoked"):
        async with fast_pool(space).lease() as lease:
            assert await space.revoke_lease(identity_id, reason="подозрительная активность", actor="admin")
            with pytest.raises(StaleFencing):
                await lease.save_session({"auth_key": "after-revoke"})
            await asyncio.sleep(5)

    async with sessionmaker() as s:
        history = (await s.execute(select(LeaseHistoryRecord))).scalar_one()
        revoked = (
            await s.execute(select(EventRecord).where(EventRecord.type == "lease.revoked"))
        ).scalar_one()
    assert history.end_reason == "revoked"
    assert revoked.actor == "admin"
    assert revoked.data["reason"] == "подозрительная активность"
