"""P5.4: верификации в БД (ТЗ FR-10.1–10.6, FR-9.5) и требования платформы."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import AttemptsExhausted, ValidationFailed, VerificationRequirementsNotMet
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.lease import Lease
from identity_hub.postgres.models import EventRecord, IdentityRecord, RegionStateRecord, VerificationRecord
from identity_hub.store.base import ServiceRow
from tests.fixtures.api import register
from tests.fixtures.services import KycEvidence

pytestmark = pytest.mark.pg


async def _exchange(space: SpaceHandle) -> tuple[UUID, Lease]:
    r = await register(
        space,
        service="fake_exchange",
        external_ref="trader@x",
        credentials={"email": "trader@x", "password": "p"},
        session={"cookies": {"sid": "1"}},
        lifecycle="ready",
    )
    lease = await space.pools.define("ex", service="fake_exchange").acquire(among=[r.identity_id])
    return r.identity_id, lease


async def _verified(sm: async_sessionmaker[AsyncSession], identity_id: UUID) -> list[str]:
    async with sm() as s:
        value = (
            await s.execute(select(IdentityRecord.verified_factors).where(IdentityRecord.id == identity_id))
        ).scalar_one()
    return sorted(value)


async def test_full_kyc_cycle(space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    identity_id, lease = await _exchange(space)
    async with sessionmaker() as s:
        assert (
            await s.execute(
                select(IdentityRecord.verification_tier_rank).where(IdentityRecord.id == identity_id)
            )
        ).scalar_one() is None
    kyc = lease.verification("kyc_id")
    with pytest.raises(VerificationRequirementsNotMet):
        await kyc.start()

    for factor in ("email", "phone"):
        v = lease.verification(factor)
        await v.start()
        assert (await v.submit()).state == "verified"  # auto
    assert await _verified(sessionmaker, identity_id) == ["email", "phone"]
    async with sessionmaker() as s:
        assert (
            await s.execute(
                select(IdentityRecord.verification_tier_rank).where(IdentityRecord.id == identity_id)
            )
        ).scalar_one() == 0

    status = await kyc.start(KycEvidence(document_number=SecretStr("AB123456")))
    assert status.state == "pending"
    async with sessionmaker() as s:
        row = (
            await s.execute(select(VerificationRecord).where(VerificationRecord.factor == "kyc_id"))
        ).scalar_one()
    assert row.evidence_ciphertext is not None and b"AB123456" not in row.evidence_ciphertext
    evidence = await kyc.evidence()
    assert isinstance(evidence, KycEvidence) and evidence.document_number.get_secret_value() == "AB123456"

    assert (await kyc.submit()).state == "under_review"
    approved = await kyc.approve()
    assert approved.state == "verified" and approved.expires_at is not None and approved.attempts == 1
    assert await _verified(sessionmaker, identity_id) == ["email", "kyc_id", "phone"]
    async with sessionmaker() as s:
        assert (
            await s.execute(
                select(IdentityRecord.verification_tier_rank).where(IdentityRecord.id == identity_id)
            )
        ).scalar_one() == 1
    assert (await space.verifications.status(identity_id, "kyc_id")).state == "verified"

    # withdraw требует уровень full — теперь выдаётся (FR-10.5: verified_factors используется выборкой)
    await lease.release()
    full = await space.pools.define("ex2", service="fake_exchange").acquire(for_operation="withdraw")
    assert full.identity_id == identity_id

    async with sessionmaker() as s:
        events = [
            (e.data["key"], e.data["to"])
            for e in (await s.execute(select(EventRecord).order_by(EventRecord.id))).scalars()
            if e.type == "identity.verification.changed"
        ]
    assert ("kyc_id", "under_review") in events and ("kyc_id", "verified") in events
    async with sessionmaker() as s:
        tier_events = [
            (event.type, event.data["tier"], event.data["factor"])
            for event in (
                await s.execute(
                    select(EventRecord).where(
                        EventRecord.type.in_(("identity.tier.reached", "identity.tier.lost"))
                    )
                )
            ).scalars()
        ]
    assert tier_events == [
        ("identity.tier.reached", "basic", "email"),
        ("identity.tier.reached", "full", "kyc_id"),
    ]


async def test_read_tier_rank_matches_account_item_and_resyncs_service_configuration(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    identity_id, lease = await _exchange(space)
    for factor in ("email", "phone"):
        verification = lease.verification(factor)
        await verification.start()
        await verification.submit()

    ref = await space.ref()
    async with sessionmaker() as session:
        read_rank = (
            await session.execute(
                select(hub.read.identities.c.verification_tier_rank).where(
                    hub.read.identities.c.space_id == ref.id,
                    hub.read.identities.c.id == identity_id,
                )
            )
        ).scalar_one()
    item = next(item for item in (await space.admin.identity_page()).items if item.id == identity_id)
    assert read_rank == item.verification_tier_rank == 0

    service = hub.service("fake_exchange")
    await hub._store.sync_services([ServiceRow(service.name, service.title, service.url, ())])
    async with sessionmaker() as session:
        assert (
            await session.execute(
                select(IdentityRecord.verification_tier_rank).where(IdentityRecord.id == identity_id)
            )
        ).scalar_one() is None

    await hub._store.sync_services(
        [ServiceRow(service.name, service.title, service.url, tuple(tier.factors for tier in service.tiers))]
    )
    async with sessionmaker() as session:
        assert (
            await session.execute(
                select(IdentityRecord.verification_tier_rank).where(IdentityRecord.id == identity_id)
            )
        ).scalar_one() == 0


async def test_reject_attempts_and_revocation(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id, lease = await _exchange(space)
    for factor in ("email", "phone"):
        v = lease.verification(factor)
        await v.start()
        await v.submit()
    kyc = lease.verification("kyc_id")
    for _ in range(3):
        await kyc.start()
        await kyc.submit()
        status = await kyc.reject("blurry")
    assert (status.state, status.attempts, status.region.code) == ("rejected", 3, "blurry")
    with pytest.raises(AttemptsExhausted):
        await kyc.start()
    with pytest.raises(ValidationFailed):
        await lease.verification("unknown_factor").start()

    email = lease.verification("email")
    # R2-F13: одна проба не снимает подтверждение — нужны два наблюдения подряд
    assert (await email.reconcile("missing", raw_status="EMAIL_UNCONFIRMED")).state == "verified"
    assert (await email.reconcile("missing", raw_status="EMAIL_UNCONFIRMED")).state == "revoked"  # FR-9.5
    assert await _verified(sessionmaker, identity_id) == ["phone"]


async def test_resume_and_pending_expiry(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id, lease = await _exchange(space)
    phone = lease.verification("phone")
    first = await phone.start()
    again = await phone.start()  # возобновление
    assert first.region.version == again.region.version
    assert await space.maintenance.expire_verifications() == 0
    async with sessionmaker() as s, s.begin():
        await s.execute(
            update(RegionStateRecord).where(RegionStateRecord.key == "phone").values(until=text("now()"))
        )
    assert await space.maintenance.expire_verifications() == 1
    assert (await space.verifications.status(identity_id, "phone")).state == "missing"


async def test_requirements(space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    identity_id, _ = await _exchange(space)
    assert await space.verifications.requirements.add(identity_id, "phone_verification", actor="test")
    assert not await space.verifications.requirements.add(identity_id, "phone_verification", actor="test")
    async with sessionmaker() as s:
        reqs: Any = (await s.execute(select(IdentityRecord.requirements))).scalar_one()
    assert [r["code"] for r in reqs] == ["phone_verification"]
    assert await space.verifications.requirements.resolve(identity_id, "phone_verification", actor="test")
    assert not await space.verifications.requirements.resolve(identity_id, "phone_verification", actor="test")
    async with sessionmaker() as s:
        types = list((await s.execute(select(EventRecord.type))).scalars())
    assert "identity.requirement.added" in types and "identity.requirement.resolved" in types
