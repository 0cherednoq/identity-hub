"""IMP-16 (I0.3): строки `1.0.0a1` читаются, rotate-key переводит их на AAD v1."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub import IdentityCreate, IdentityHub, SecretRef, SpaceHandle
from identity_hub.crypto import EncryptedPayload, Envelope, StaticSecretsProvider, associated_data
from identity_hub.postgres.models import CredentialsRecord
from tests.conftest import TEST_KEYS

pytestmark = [pytest.mark.pg]


async def test_rotate_key_upgrades_legacy_credentials(
    hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+aad", credentials={"phone": "+aad"}), actor="test"
    )
    aad = associated_data("ih_credentials", identity.id)
    legacy = hub._envelope.seal(b'{"phone":"+aad","password":"legacy-pass"}', aad.legacy)
    async with space._tx(own=True) as tx:
        await tx.put_credentials(identity.id, legacy)

    revealed = await space.secrets.reveal(identity.id, SecretRef.CREDENTIALS, actor="admin", reason="тест")
    assert revealed["password"] == "legacy-pass"

    hub._envelope = Envelope(StaticSecretsProvider(TEST_KEYS, "k2"))
    report = await hub.doctor.rotate_key(limit=100)
    assert report.counts.get("credentials") == 1

    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(CredentialsRecord).where(CredentialsRecord.identity_id == identity.id)
            )
        ).scalar_one()
    stored = EncryptedPayload(row.key_id, row.wrapped_dek, row.nonce, row.ciphertext)
    assert stored.key_id == "k2"
    # строка читается строго текущим форматом AAD, без legacy-запасного пути
    assert b"legacy-pass" in hub._envelope.open(stored, bytes(aad))
