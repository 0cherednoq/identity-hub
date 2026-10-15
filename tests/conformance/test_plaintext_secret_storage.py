"""S5.1: dual-format credentials/session storage on every backend (D-313)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG

from identity_hub import (
    IdentityCreate,
    IdentityHub,
    ImportRecord,
    SecretRef,
    StaticSecretsProvider,
    ValidationFailed,
)
from identity_hub.clock import FakeClock
from identity_hub.testing import MemoryStore

pytestmark = pytest.mark.conformance


@pytest.fixture
async def plaintext_hub(
    backend_sessionmaker: async_sessionmaker[AsyncSession] | None,
) -> AsyncIterator[IdentityHub]:
    secrets = StaticSecretsProvider(TEST_KEYS, "k1")
    if backend_sessionmaker is None:
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        hub = IdentityHub(
            secrets=secrets,
            services=[FAKE_TG, FAKE_EXCHANGE],
            store=MemoryStore(clock),
            clock=clock,
            secret_storage="plaintext",
        )
    else:
        hub = IdentityHub(
            backend_sessionmaker,
            secrets,
            [FAKE_TG, FAKE_EXCHANGE],
            secret_storage="plaintext",
        )
    await hub.start()
    yield hub
    await hub.aclose()


def test_unknown_secret_storage_is_rejected() -> None:
    with pytest.raises(ValidationFailed, match="secret_storage"):
        IdentityHub(
            secrets=StaticSecretsProvider(TEST_KEYS, "k1"),
            store=MemoryStore(),
            secret_storage="cleartext",
        )


async def test_plaintext_public_flows_and_mixed_storage(plaintext_hub: IdentityHub) -> None:
    slug = f"plain-{uuid4().hex[:8]}"
    space = plaintext_hub.space(slug, create=True)
    plain = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="+plain",
            lifecycle="ready",
            credentials={"phone": "+plain", "password": "plain-pass"},
            session={"auth_key": "plain-session"},
        ),
        actor="test",
    )

    async with space.pools.define("plain", service="fake_tg", holder="worker").lease() as lease:
        credentials = await lease.credentials()
        session = await lease.session()
        assert credentials is not None and credentials.password.get_secret_value() == "plain-pass"
        assert session is not None and session.auth_key.get_secret_value() == "plain-session"

    revealed = await space.secrets.reveal(plain.id, SecretRef.CREDENTIALS, actor="admin", reason="проверка")
    assert revealed["password"] == "plain-pass"

    report = await space.imports.run(
        service="fake_tg",
        records=[
            ImportRecord(
                external_ref="+imported",
                credentials={"phone": "+imported", "password": "import-pass"},
                sessions={"default": {"auth_key": "import-session"}},
            )
        ],
        actor="test",
    )
    assert len(report.created) == 1 and not report.failed
    imported = await space.identities.get_by_ref("fake_tg", "+imported")
    assert (
        await space.secrets.reveal(imported.id, SecretRef.session(), actor="admin", reason="проверка импорта")
    )["auth_key"] == "import-session"

    # Другой процесс может выбрать encrypted: обе формы остаются читаемыми в одной базе.
    secrets = StaticSecretsProvider(TEST_KEYS, "k1")
    if plaintext_hub.sessionmaker is None:
        encrypted_hub = IdentityHub(
            secrets=secrets,
            services=[FAKE_TG, FAKE_EXCHANGE],
            store=plaintext_hub._store,
            clock=plaintext_hub.clock,
        )
    else:
        encrypted_hub = IdentityHub(plaintext_hub.sessionmaker, secrets, [FAKE_TG, FAKE_EXCHANGE])
    await encrypted_hub.start()
    encrypted_space = encrypted_hub.space(slug)
    encrypted = await encrypted_space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="+encrypted",
            lifecycle="ready",
            credentials={"phone": "+encrypted", "password": "encrypted-pass"},
            session={"auth_key": "encrypted-session"},
        ),
        actor="test",
    )
    for identity_id, password, auth_key in (
        (plain.id, "plain-pass", "plain-session"),
        (encrypted.id, "encrypted-pass", "encrypted-session"),
    ):
        value = await encrypted_space.secrets.reveal(
            identity_id, SecretRef.CREDENTIALS, actor="admin", reason="смешанная база"
        )
        assert value["password"] == password
        session = await encrypted_space.secrets.reveal(
            identity_id, SecretRef.session(), actor="admin", reason="смешанная база"
        )
        assert session["auth_key"] == auth_key
    await encrypted_hub.aclose()


async def test_plaintext_secret_size_limit(plaintext_hub: IdentityHub) -> None:
    space = plaintext_hub.space(f"plain-limit-{uuid4().hex[:8]}", create=True)
    with pytest.raises(ValidationFailed, match="512 KiB"):
        await space.identities.create(
            IdentityCreate(
                service="fake_tg",
                external_ref="+too-large",
                credentials={"phone": "+too-large", "password": "x" * (512 * 1024)},
            ),
            actor="test",
        )
