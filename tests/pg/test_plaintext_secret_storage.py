"""S5.1: plaintext действительно доступен обычным SQL, а envelope-колонки пусты."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub import IdentityCreate, IdentityHub, StaticSecretsProvider
from identity_hub.postgres.models import CredentialsRecord, SessionRecord
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_TG


async def test_plaintext_payload_is_readable_by_sql(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    hub = IdentityHub(
        sessionmaker,
        StaticSecretsProvider(TEST_KEYS, "k1"),
        [FAKE_TG],
        secret_storage="plaintext",
    )
    await hub.start()
    space = hub.space("plaintext-sql", create=True)
    identity = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="+sql",
            credentials={"phone": "+sql", "password": "sql-pass"},
            session={"auth_key": "sql-session"},
        ),
        actor="test",
    )

    credentials = CredentialsRecord.__table__.c
    sessions = SessionRecord.__table__.c
    async with sessionmaker() as session:
        credential = (
            await session.execute(
                select(
                    credentials.payload,
                    credentials.key_id,
                    credentials.wrapped_dek,
                    credentials.nonce,
                    credentials.ciphertext,
                ).where(credentials.identity_id == identity.id)
            )
        ).one()
        stored_session = (
            await session.execute(
                select(sessions.payload, sessions.key_id, sessions.ciphertext).where(
                    sessions.identity_id == identity.id
                )
            )
        ).one()

    assert credential.payload["password"] == "sql-pass"
    assert credential[1:] == (None, None, None, None)
    assert stored_session.payload["auth_key"] == "sql-session"
    assert stored_session[1:] == (None, None)
    await hub.aclose()
