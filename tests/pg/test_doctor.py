"""P9.2: doctor и постепенная ротация мастер-ключа (G-6.2, S-3)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import SecretStr
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub._ids import new_id
from identity_hub.crypto import Envelope, StaticSecretsProvider, associated_data
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import (
    ChallengeRecord,
    CredentialsRecord,
    IdentityRecord,
    NotificationChannelRecord,
    ProxyRecord,
    SessionRecord,
    VerificationRecord,
    WebhookEndpointRecord,
)
from tests.conftest import TEST_KEYS
from tests.fixtures.api import add_proxy, register
from tests.fixtures.services import FAKE_TG, TgCredentials

pytestmark = pytest.mark.pg


async def test_doctor_finds_and_fixes_denormalization(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    registered = await register(
        space,
        service="fake_tg",
        external_ref="+doctor",
        lifecycle="ready",
        session={"auth_key": "doctor-key", "dc_id": 2},
    )
    identity_id = registered.identity_id
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(IdentityRecord)
            .where(IdentityRecord.id == identity_id)
            .values(
                standing="banned",
                auth_state="session_invalid",
                verified_factors=["ghost"],
                event_seq=0,
                revision=0,
            )
        )

    issues = await hub.doctor.check()
    assert {issue.kind for issue in issues} >= {
        "standing",
        "auth_state",
        "verified_factors",
        "event_seq",
        "revision",  # нижняя граница — ревизия последнего события (D-205)
    }
    fixed = await hub.doctor.check(fix=True)
    assert len(fixed) == len(issues)
    assert await hub.doctor.check() == []


async def test_rotate_key_rewraps_every_encrypted_table(
    hub: IdentityHub,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    secret_service = replace(FAKE_TG, name="secret_tg", profile_encrypted=True)
    hub.register_service(secret_service)
    await hub.start()
    space = hub.space("rotation", create=True)
    registered = await register(
        space,
        service="secret_tg",
        external_ref="+rotate",
        credentials=TgCredentials(phone="+rotate", password=SecretStr("password")),
        session={"auth_key": "auth-key", "dc_id": 2},
        profile={"first_name": "Ada", "country": "GB"},
    )
    identity_id = registered.identity_id
    await add_proxy(space, "http://login:pass@127.0.0.1:8080", pool="rotate")
    await space.webhooks.create(
        url="https://hooks.example.test/rotate",
        secret=SecretStr("webhook-secret"),
        event_types=["*"],
    )
    await space.notifications.create_telegram_channel(
        "rotate", bot_token=SecretStr("123456:bot-secret"), chat_id=1
    )

    challenge_id = new_id()
    challenge_payload = hub._envelope.seal(
        b"{}", associated_data("ih_challenges", identity_id, str(challenge_id))
    )
    evidence = hub._envelope.seal(b"{}", associated_data("ih_verifications", identity_id, "phone"))
    ref = await space.ref()
    async with sessionmaker() as session, session.begin():
        await session.execute(
            insert(ChallengeRecord).values(
                id=challenge_id,
                identity_id=identity_id,
                space_id=ref.id,
                kind="sms",
                hint="***",
                **challenge_payload.columns("context_"),
            )
        )
        await session.execute(
            insert(VerificationRecord).values(
                identity_id=identity_id,
                factor="phone",
                space_id=ref.id,
                attempts=0,
                details={},
                **evidence.columns("evidence_"),
            )
        )

    hub._envelope = Envelope(StaticSecretsProvider(TEST_KEYS, "k2"))
    report = await hub.doctor.rotate_key(limit=100)

    assert report.total == 8
    assert set(report.counts) == {
        "credentials",
        "sessions",
        "challenges",
        "verifications",
        "proxies",
        "webhooks",
        "notification_channels",
        "profiles",
    }
    async with sessionmaker() as session:
        key_columns = (
            CredentialsRecord.key_id,
            SessionRecord.key_id,
            ChallengeRecord.context_key_id,
            VerificationRecord.evidence_key_id,
            ProxyRecord.key_id,
            WebhookEndpointRecord.key_id,
            NotificationChannelRecord.key_id,
            IdentityRecord.profile_key_id,
        )
        for column in key_columns:
            assert set((await session.execute(select(column))).scalars()) == {"k2"}


async def test_rotate_key_reports_locked_rows_and_finishes_after_unlock(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """R2-S08: строки, заблокированные другим процессом, не считаются переобёрнутыми."""
    for n in range(4):
        await register(
            space,
            service="fake_tg",
            external_ref=f"+lock{n}",
            credentials=TgCredentials(phone=f"+lock{n}", password=SecretStr("p")),
        )
    hub._envelope = Envelope(StaticSecretsProvider(TEST_KEYS, "k2"))

    async with sessionmaker() as locker, locker.begin():
        locked = (
            await locker.execute(
                select(CredentialsRecord.identity_id)
                .order_by(CredentialsRecord.identity_id)
                .limit(2)
                .with_for_update()
            )
        ).scalars()
        assert len(list(locked)) == 2

        report = await hub.doctor.rotate_key(limit=3, lock_retries=2, retry_delay=0)

        assert report.counts == {"credentials": 2}
        assert report.remaining == {"credentials": {"k1": 2}}
        assert not report.complete and report.unreadable == ()

    report = await hub.doctor.rotate_key(limit=3)
    assert report.counts == {"credentials": 2} and report.complete
    async with sessionmaker() as session:
        assert set((await session.execute(select(CredentialsRecord.key_id))).scalars()) == {"k2"}
