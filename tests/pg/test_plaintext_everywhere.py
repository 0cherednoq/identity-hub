"""S13.1 (D-323): режим plaintext хранит ОТКРЫТО все виды секретов, не только creds/sessions."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub import ImportRecord, SecretRef
from identity_hub._ids import new_id
from identity_hub.crypto import StaticSecretsProvider, associated_data
from identity_hub.hub import IdentityHub
from identity_hub.postgres.models import ChallengeRecord
from tests.conftest import TEST_KEYS
from tests.fixtures.api import add_proxy, register
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG, KycEvidence, TgCredentials

pytestmark = pytest.mark.pg


async def _plaintext_hub(sessionmaker: async_sessionmaker[AsyncSession]) -> IdentityHub:
    secret_service = replace(FAKE_TG, name="plain_tg", profile_encrypted=True)
    hub = IdentityHub(
        sessionmaker,
        StaticSecretsProvider(TEST_KEYS, "k1"),
        [secret_service],
        secret_storage="plaintext",
    )
    await hub.start()
    return hub


async def _scalar(sessionmaker: async_sessionmaker[AsyncSession], sql: str) -> object:
    async with sessionmaker() as session:
        return (await session.execute(text(sql))).scalar_one()


async def test_all_secret_stores_readable_in_plaintext_mode(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    hub = await _plaintext_hub(sessionmaker)
    space = hub.space("plain", "Plain", create=True)
    registered = await register(
        space,
        service="plain_tg",
        external_ref="+plain",
        credentials=TgCredentials(phone="+plain", password=SecretStr("creds-pw")),
        session={"auth_key": "auth-secret", "dc_id": 2},
        profile={"first_name": "Ada", "country": "GB"},  # first_name — секретная часть профиля
    )
    identity_id = registered.identity_id
    await add_proxy(space, "http://login:proxypw@127.0.0.1:8080", pool="plain")
    await space.webhooks.create(
        url="https://hooks.example.test/x", secret=SecretStr("wh-secret"), event_types=["*"]
    )
    await space.notifications.create_telegram_channel(
        "chan", bot_token=SecretStr("123456:bot-secret"), chat_id=1
    )

    # 1. Сырые колонки: payload заполнен, шифр пуст — читается без мастер-ключа.
    checks = {
        "ih_credentials": "payload IS NOT NULL AND ciphertext IS NULL",
        "ih_sessions": "payload IS NOT NULL AND ciphertext IS NULL",
        "ih_proxies": "payload IS NOT NULL AND ciphertext IS NULL",
        "ih_webhook_endpoints": "payload IS NOT NULL AND ciphertext IS NULL",
        "ih_notification_channels": "payload IS NOT NULL AND ciphertext IS NULL",
    }
    for table, cond in checks.items():
        total = await _scalar(sessionmaker, f"SELECT count(*) FROM {table}")
        matching = await _scalar(sessionmaker, f"SELECT count(*) FROM {table} WHERE {cond}")
        assert total and total == matching, f"{table}: {matching}/{total} в plaintext"

    profile_open = await _scalar(
        sessionmaker,
        "SELECT profile_payload IS NOT NULL AND profile_ciphertext IS NULL FROM ih_identities "
        f"WHERE id = '{identity_id}'",
    )
    assert profile_open is True

    # 2. Открытый пароль прокси виден прямым SQL.
    proxy_pw = await _scalar(
        sessionmaker, "SELECT payload->>'password' FROM ih_proxies WHERE host = '127.0.0.1'"
    )
    assert proxy_pw == "proxypw"

    # 3. Публичное чтение по-прежнему работает (смешиваемость формата).
    revealed = await space.secrets.reveal(identity_id, SecretRef.CREDENTIALS, actor="admin", reason="тест")
    assert revealed["password"] == "creds-pw"
    profile_secret = await space.secrets.reveal(identity_id, SecretRef.PROFILE, actor="admin", reason="тест")
    assert profile_secret["first_name"] == "Ada"
    await hub.aclose()


async def test_doctor_converts_every_secret_store_round_trip(
    hub: IdentityHub,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    service = replace(FAKE_TG, name="convert_tg", profile_encrypted=True)
    hub.register_service(service)
    await hub.start()
    space = hub.space("convert-all", create=True)
    registered = await register(
        space,
        service=service.name,
        external_ref="+convert-all",
        credentials=TgCredentials(phone="+convert-all", password=SecretStr("pw")),
        session={"auth_key": "session"},
        profile={"first_name": "Ada"},
        lifecycle="ready",
    )
    await add_proxy(space, "http://user:password@127.0.0.2:8080", pool="convert")
    await space.webhooks.create(
        url="https://hooks.example.test/convert", secret=SecretStr("secret"), event_types=["*"]
    )
    await space.notifications.create_telegram_channel(
        "convert", bot_token=SecretStr("123456:secret"), chat_id=1
    )
    await space.imports.stage(
        service=service.name,
        source="convert.jsonl",
        actor="test",
        records=[ImportRecord(external_ref="+staged", credentials={"phone": "+staged"})],
    )

    exchange = await register(
        space,
        service=FAKE_EXCHANGE.name,
        external_ref="convert@example.test",
        credentials={"email": "convert@example.test", "password": "pw"},
        session={"cookies": {"sid": "1"}},
        lifecycle="ready",
    )
    lease = await space.pools.define("verify", service=FAKE_EXCHANGE.name).acquire(
        among=[exchange.identity_id]
    )
    for factor in ("email", "phone"):
        verification = lease.verification(factor)
        await verification.start()
        await verification.submit()
    kyc = lease.verification("kyc_id")
    await kyc.start(KycEvidence(document_number=SecretStr("AB123456")))

    challenge_id = new_id()
    aad = associated_data("ih_challenges", registered.identity_id, str(challenge_id))
    context = hub._envelope.seal_model({"phone_code_hash": "hash"}, aad)
    nonce, ciphertext = hub._envelope.seal_sibling(context, aad, b"12345", "answer")
    async with space._tx(own=True) as tx:
        assert await tx.open_challenge(
            challenge_id,
            registered.identity_id,
            "sms_code",
            "Code",
            context,
            await tx.now() + timedelta(minutes=5),
        )
        assert await tx.answer_challenge(challenge_id, nonce, ciphertext)

    plaintext = await hub.doctor.convert_secrets(to="plaintext", space=space.slug, batch=2)
    for table in (
        "challenges",
        "verifications",
        "proxies",
        "webhooks",
        "notification_channels",
        "profiles",
        "import_items",
    ):
        assert plaintext.counts[table] == 1
    assert set(await hub.doctor.plaintext_secrets(space=space.slug)) == set(plaintext.counts)
    async with sessionmaker() as session:
        challenge = await session.get(ChallengeRecord, challenge_id)
        assert challenge is not None
        assert challenge.context_payload == {"phone_code_hash": "hash"}
        assert challenge.answer_payload == "12345" and challenge.answer_ciphertext is None

    encrypted = await hub.doctor.convert_secrets(to="encrypted", space=space.slug, batch=2)
    for table in (
        "challenges",
        "verifications",
        "proxies",
        "webhooks",
        "notification_channels",
        "profiles",
        "import_items",
    ):
        assert encrypted.counts[table] == 1
    assert await hub.doctor.plaintext_secrets(space=space.slug) == {}
    async with sessionmaker() as session:
        challenge = await session.get(ChallengeRecord, challenge_id)
        assert challenge is not None
        assert challenge.context_payload is None and challenge.context_ciphertext is not None
        assert challenge.answer_payload is None and challenge.answer_ciphertext is not None
    evidence = await kyc.evidence()
    assert isinstance(evidence, KycEvidence)
    assert evidence.document_number.get_secret_value() == "AB123456"
    await lease.release()
