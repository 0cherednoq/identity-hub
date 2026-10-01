"""P3.3: жизненный цикл аренды (ТЗ FR-6.2, 6.3, 6.7, G-1.2, G-4.2, T-2, FR-12.1)."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.context import current_lease
from identity_hub.errors import LeaseLost
from identity_hub.hub import SpaceHandle
from identity_hub.pool import Pool
from identity_hub.postgres.models import EventRecord, LeaseHistoryRecord, LeaseRecord
from identity_hub.postgres.store import PgTx
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg


def fast_pool(space: SpaceHandle) -> Pool:
    return space.pools.define(
        "fast",
        service="fake_tg",
        ttl=timedelta(seconds=1.5),
        heartbeat=timedelta(seconds=0.3),
        safety_margin=timedelta(seconds=0.5),
    )


async def _history(sm: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with sm() as s:
        return list(
            (await s.execute(select(LeaseHistoryRecord).order_by(LeaseHistoryRecord.acquired_at))).scalars()
        )


async def test_heartbeat_extends_and_release_closes(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await make_identity(space)
    pool = fast_pool(space)
    async with pool.lease() as lease:
        assert current_lease() is not None and current_lease().lease_id == lease.lease_id  # type: ignore[union-attr]
        first_expiry = lease.expires_at
        await asyncio.sleep(2.0)  # дольше TTL: без heartbeat аренда бы истекла
        assert lease.expires_at > first_expiry
        assert not lease.lost
        lease._count("read", ok=True)
        lease._count("read", ok=False, error="boom")
    assert current_lease() is None

    (history,) = await _history(sessionmaker)
    assert history.end_reason == "released"
    assert history.released_at is not None
    assert history.operations == {"read": 2}
    assert (history.succeeded, history.failed, history.last_error) == (1, 1, "boom")
    async with sessionmaker() as s:
        row = (await s.execute(select(LeaseRecord))).scalar_one()
        types = list((await s.execute(select(EventRecord.type).order_by(EventRecord.id))).scalars())
    assert row.idempotency_key is None
    assert types == ["lease.acquired", "lease.released"]
    again = await pool.acquire()  # аккаунт снова свободен сразу после release
    assert again.fencing == lease.fencing + 1


async def test_exception_marks_failed(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await make_identity(space)
    with pytest.raises(ValueError, match="bad"):
        async with fast_pool(space).lease():
            raise ValueError("bad")
    (history,) = await _history(sessionmaker)
    assert history.end_reason == "failed"
    assert history.last_error == "ValueError"


async def test_lost_before_ttl_when_heartbeat_fails(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await make_identity(space)
    pool = fast_pool(space)
    lost_called = asyncio.Event()

    async def broken(self: PgTx, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("db down")

    started = time.monotonic()
    with pytest.raises(LeaseLost) as exc:
        async with pool.lease(on_lost=lost_called.set) as lease:
            monkeypatch.setattr(PgTx, "extend_lease", broken)
            await asyncio.sleep(10)
    elapsed = time.monotonic() - started
    assert exc.value.lease_id == lease.lease_id
    assert lost_called.is_set()
    assert elapsed < 1.5  # TTL − safety_margin = 1.0 с, раньше TTL (G-1.2)
    assert lease.lost
    monkeypatch.undo()
    (history,) = await _history(sessionmaker)
    assert history.end_reason == "lost"


async def test_revoked_lease_detected_by_heartbeat(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await make_identity(space)
    with pytest.raises(LeaseLost, match="revoked"):
        async with fast_pool(space).lease():
            async with sessionmaker() as s, s.begin():
                await s.execute(update(LeaseRecord).values(token=LeaseRecord.lease_id))  # «чужой» token
            await asyncio.sleep(5)


async def test_async_on_lost_and_check(space: SpaceHandle, monkeypatch: pytest.MonkeyPatch) -> None:
    await make_identity(space)
    calls: list[str] = []

    async def on_lost() -> None:
        calls.append("disconnect")

    async def slow(self: PgTx, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(30)

    with pytest.raises(LeaseLost):
        async with fast_pool(space).lease(on_lost=on_lost) as lease:
            monkeypatch.setattr(PgTx, "extend_lease", slow)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                lease.check()  # поднимет LeaseLost, как только аренда потеряна
    assert calls == ["disconnect"]


async def test_release_idempotent_and_foreign_token_noop(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await make_identity(space)
    pool = fast_pool(space)
    lease = await pool.acquire()
    await lease.release()
    await lease.release()
    newer = await pool.acquire()
    stale = lease
    stale._released = False
    await stale.release()  # старый token не трогает новую аренду
    async with sessionmaker() as s:
        row = (await s.execute(select(LeaseRecord))).scalar_one()
    assert row.lease_id == newer.lease_id
    assert row.expires_at > newer.acquired_at
    reasons = [h.end_reason for h in await _history(sessionmaker)]
    assert reasons == ["released", None]


async def test_heartbeat_does_not_use_user_session(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await make_identity(space)
    async with sessionmaker() as user:
        await user.begin()
        async with fast_pool(space.using(user)).lease() as lease:  # type: ignore[arg-type]
            await asyncio.sleep(0.8)
            assert not lease.lost
        await user.rollback()
    (history,) = await _history(sessionmaker)
    assert history.end_reason == "released"


async def test_on_lost_fires_when_check_detects_loss_first(
    space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Регрессия: потерю первым заметил check(), а не heartbeat — on_lost всё равно вызывается."""
    from identity_hub.lease import Lease

    async def idle(self: Lease) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(Lease, "_heartbeat_loop", idle)
    await make_identity(space)
    calls: list[str] = []
    with pytest.raises(LeaseLost):
        async with fast_pool(space).lease(on_lost=lambda: calls.append("lost")) as lease:
            await asyncio.sleep(1.1)  # дедлайн TTL − safety_margin = 1.0 с
            lease.check()
    assert calls == ["lost"]
