"""P4.2: lease.operation (ТЗ FR-7.1, 7.3, 7.4, 7.5, §3.9, G-2.1)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import OperationNotAllowed, QuotaExhausted
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.pool import Pool
from identity_hub.postgres.models import (
    EventRecord,
    IdentityRecord,
    LeaseHistoryRecord,
    QuotaUsageRecord,
    SessionRecord,
)
from identity_hub.protocols import Blame, Fault
from identity_hub.service import OperationRule, Quota
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.pg


class RateLimited(Exception):
    pass


class SessionRevoked(Exception):
    pass


class Banned(Exception):
    pass


class ProxyBroken(Exception):
    pass


class Classifier:
    def classify(self, exc: BaseException) -> Fault | None:
        if isinstance(exc, RateLimited):
            return Fault(Blame.IDENTITY, retry_after=timedelta(minutes=5), code="FLOOD_WAIT")
        if isinstance(exc, SessionRevoked):
            return Fault(Blame.SESSION_INVALID, raw_status="AUTH_KEY_UNREGISTERED")
        if isinstance(exc, Banned):
            return Fault(Blame.IDENTITY_BANNED, code="spam", raw_status="USER_DEACTIVATED")
        if isinstance(exc, ProxyBroken):
            return Fault(Blame.PROXY)
        return None


CLASSIFIED = replace(
    FAKE_TG,
    name="classified",
    error_classifier=Classifier(),
    quarantine_threshold=3.0,
    operations={
        **FAKE_TG.operations,
        "tiny": OperationRule(quotas=(Quota(2, timedelta(hours=1)),)),
    },
)


@pytest.fixture
async def pool(hub: IdentityHub, space: SpaceHandle) -> Pool:
    hub.register_service(CLASSIFIED)
    await hub.start()
    await space.register(
        service="classified", external_ref="+1", lifecycle="ready", session={"auth_key": "k"}
    )
    return space.pool("p", service="classified")


async def _identity(sm: async_sessionmaker[AsyncSession]) -> Any:
    async with sm() as s:
        return (await s.execute(select(IdentityRecord))).scalar_one()


async def _event_types(sm: async_sessionmaker[AsyncSession]) -> list[str]:
    async with sm() as s:
        return list((await s.execute(select(EventRecord.type).order_by(EventRecord.id))).scalars())


async def test_success_charges_before_call_and_counts(
    pool: Pool, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with pool.lease() as lease:
        async with lease.operation("tiny") as op:
            async with sessionmaker() as s:  # списано до вызова платформы
                assert (await s.execute(select(QuotaUsageRecord.used))).scalar_one() == 1
            assert not op.duplicate
        async with lease.operation("tiny"):
            pass
        with pytest.raises(QuotaExhausted):
            async with lease.operation("tiny"):
                pytest.fail("тело не должно выполняться")
    async with sessionmaker() as s:
        history = (await s.execute(select(LeaseHistoryRecord))).scalar_one()
    assert history.operations == {"tiny": 2}
    assert history.succeeded == 2


async def test_quota_not_rolled_back_by_user_exception(
    pool: Pool, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with pool.lease() as lease:
        with pytest.raises(ValueError):
            async with lease.operation("tiny"):
                raise ValueError("user bug")
    async with sessionmaker() as s:
        assert (await s.execute(select(QuotaUsageRecord.used))).scalar_one() == 1  # G-2.1
    assert "operation.failed" in await _event_types(sessionmaker)
    assert (await _identity(sessionmaker)).error_score == 0  # неизвестная ошибка — вина запроса


async def test_operation_key_duplicate(pool: Pool, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with pool.lease() as lease:
        async with lease.operation("tiny", key="job-1") as first:
            pass
        async with lease.operation("tiny", key="job-1") as again:
            pass
    assert (first.duplicate, again.duplicate) == (False, True)
    async with sessionmaker() as s:
        assert (await s.execute(select(QuotaUsageRecord.used))).scalar_one() == 1


async def test_rate_limit_cools_down_and_quarantines(
    pool: Pool, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with pool.lease() as lease:
        with pytest.raises(RateLimited):
            async with lease.operation("read_channel"):
                raise RateLimited
        identity = await _identity(sessionmaker)
        assert (identity.availability, identity.error_score) == ("cooling", 1.0)
        with pytest.raises(OperationNotAllowed) as exc:
            async with lease.operation("read_channel"):
                pass
        assert exc.value.blockers[0].code == "availability"

        async with sessionmaker() as s, s.begin():  # искусственно: кулдаун истёк, счёт почти на пороге
            await s.execute(
                update(IdentityRecord).values(availability_until=IdentityRecord.created_at, error_score=2.5)
            )
        with pytest.raises(RateLimited):
            async with lease.operation("read_channel"):
                raise RateLimited
    identity = await _identity(sessionmaker)
    assert identity.availability == "quarantined"
    assert identity.error_score == 3.5


async def test_success_decays_error_score(pool: Pool, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as s, s.begin():
        await s.execute(update(IdentityRecord).values(error_score=2.0))
    async with pool.lease() as lease, lease.operation("read_channel"):
        pass
    assert (await _identity(sessionmaker)).error_score == 1.0


async def test_session_invalid_and_ban(pool: Pool, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with pool.lease() as lease:
        with pytest.raises(SessionRevoked):
            async with lease.operation("read_channel"):
                raise SessionRevoked
    identity = await _identity(sessionmaker)
    assert (identity.auth_state, identity.standing) == ("session_invalid", "good")  # не бан
    async with sessionmaker() as s:
        assert (await s.execute(select(SessionRecord.status))).scalar_one() == "invalid"

    async with sessionmaker() as s, s.begin():
        await s.execute(update(IdentityRecord).values(auth_state="authenticated"))
    async with pool.lease() as lease:
        with pytest.raises(Banned):
            async with lease.operation("read_channel"):
                raise Banned
    identity = await _identity(sessionmaker)
    assert (identity.standing, identity.standing_code) == ("banned", "spam")
    assert "identity.standing.changed" in await _event_types(sessionmaker)


async def test_cool_down_only_extends(pool: Pool, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with pool.lease() as lease, lease.operation("read_channel") as op:
        await op.cool_down(timedelta(minutes=10))
        first = (await _identity(sessionmaker)).availability_until
        await op.cool_down(30)
    identity = await _identity(sessionmaker)
    assert identity.availability == "cooling"
    assert identity.availability_until == first


async def test_rule_blocks_operation(pool: Pool, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with pool.lease() as lease:
        async with sessionmaker() as s, s.begin():
            await s.execute(update(IdentityRecord).values(standing="limited"))
        with pytest.raises(OperationNotAllowed) as exc:
            async with lease.operation("send_message"):
                pass
    assert [b.code for b in exc.value.blockers] == ["standing"]
