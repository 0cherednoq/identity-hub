"""F4.1a: индексы под FK и фоновые запросы, ограничения целостности (R2-D15, R2-D16, R2-D17)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from identity_hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import ProxyRecord, VerificationRecord
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg

# FK на справочник сервисов индекс не нужен: строки `ih_services` библиотека не удаляет
_FK_WITHOUT_INDEX = """
SELECT c.conrelid::regclass::text AS tbl, c.conname, a.attname
FROM pg_constraint c
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
WHERE c.contype = 'f'
  AND c.connamespace = 'public'::regnamespace
  AND c.conrelid::regclass::text LIKE 'ih\\_%'
  AND c.confrelid <> 'ih_services'::regclass
  AND NOT EXISTS (
      SELECT 1 FROM pg_index i
      WHERE i.indrelid = c.conrelid
        AND i.indkey[0] = c.conkey[1]
        AND (
            i.indpred IS NULL
            OR pg_get_expr(i.indpred, i.indrelid) = '(' || a.attname || ' IS NOT NULL)'
        )
  )
ORDER BY 1, 2
"""


async def test_every_foreign_key_has_an_index(prepared_db: AsyncEngine) -> None:
    """R2-D16: удаление родительской строки не должно перебирать дочернюю таблицу целиком."""
    async with prepared_db.connect() as conn:
        missing = [tuple(row) for row in await conn.execute(text(_FK_WITHOUT_INDEX))]
    assert missing == []


async def test_integrity_constraints_exist(prepared_db: AsyncEngine) -> None:
    """R2-D17: недостающие FK и CHECK."""
    async with prepared_db.connect() as conn:
        names = set(
            (
                await conn.execute(
                    text("SELECT conname FROM pg_constraint WHERE connamespace = 'public'::regnamespace")
                )
            ).scalars()
        )
    assert {
        "fk_ih_notifications_channel_id_ih_notification_channels",
        "fk_ih_verifications_via_identity_id_ih_identities",
        "ck_ih_proxies_bound_count_nonnegative",
        "ck_ih_proxies_max_identities_positive",
        "ck_ih_proxies_port_valid",
    } <= names


@pytest.mark.parametrize(
    "columns",
    [{"port": 0}, {"port": 65536}, {"max_identities": 0}, {"bound_count": -1}],
    ids=["port-zero", "port-too-big", "no-capacity", "negative-bound-count"],
)
async def test_proxy_row_with_impossible_numbers_is_rejected(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession], columns: dict[str, int]
) -> None:
    ref = await space.ref()
    values: dict[str, Any] = {
        "space_id": ref.id,
        "pool": "p",
        "scheme": "socks5",
        "host": "proxy.example.test",
        "port": 1080,
        "fingerprint": uuid4().hex,
        "key_id": "k1",
        "wrapped_dek": b"",
        "nonce": b"",
        "ciphertext": b"",
        **columns,
    }
    with pytest.raises(IntegrityError):
        async with sessionmaker.begin() as session:
            await session.execute(insert(ProxyRecord).values(**values))


async def test_deleting_identity_clears_verification_reference(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Подтверждающий аккаунт удалён — верификация остаётся, ссылка на него обнуляется."""
    account = await make_identity(space)
    mailbox = await make_identity(space)
    ref = await space.ref()
    async with sessionmaker.begin() as session:
        await session.execute(
            insert(VerificationRecord).values(
                identity_id=account, factor="email", space_id=ref.id, via_identity_id=mailbox
            )
        )
        await session.execute(text("DELETE FROM ih_identities WHERE id = :id"), {"id": mailbox})

    async with sessionmaker() as session:
        rows = (
            await session.execute(text("SELECT identity_id, via_identity_id FROM ih_verifications"))
        ).all()
    assert [tuple(row) for row in rows] == [(account, None)]

    with pytest.raises(IntegrityError):
        async with sessionmaker.begin() as session:
            await session.execute(
                insert(VerificationRecord).values(
                    identity_id=account, factor="phone", space_id=ref.id, via_identity_id=uuid4()
                )
            )


# ---------------------------------------------------------------- планы фоновых запросов

_SEED_IDENTITIES = """
INSERT INTO ih_identities (id, space_id, service, external_ref, normalized_ref, lifecycle, standing,
                           auth_state, availability, error_score, fencing_seq, event_seq,
                           probe_attempted_at)
SELECT gen_random_uuid(), :space, 'fake_tg', 'ref-' || g, 'ref-' || g, 'ready', 'good',
       'authenticated', 'available', 0, 0, 0, now() - g * interval '1 second'
FROM generate_series(1, 20000) g
"""

_SEED_REGIONS = """
INSERT INTO ih_region_states (identity_id, region, key, space_id, state, change_source, version,
                              synced_at, until)
SELECT id, 'standing', '', space_id, 'good', 'command', 1, now() - random() * interval '3 days',
       CASE WHEN random() < 0.002 THEN now() - interval '1 minute' END
FROM ih_identities
"""


async def _plan(hub: IdentityHub, db: AsyncEngine, marker: str, call: Any) -> str:
    """План первого запроса вызова `call`, в тексте которого есть `marker`."""
    captured: list[tuple[str, Any]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_rest: Any) -> None:
        if marker in statement:
            captured.append((statement, parameters))

    event.listen(db.sync_engine, "before_cursor_execute", capture)
    try:
        await call()
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", capture)
    statement, parameters = captured[0]
    async with db.connect() as conn:
        rows = await conn.exec_driver_sql("EXPLAIN " + statement, parameters)
        return "\n".join(str(row[0]) for row in rows)


@pytest.fixture
async def big_space(hub: IdentityHub, space: SpaceHandle, db: AsyncEngine) -> SpaceHandle:
    ref = await space.ref()
    async with db.begin() as conn:
        await conn.execute(text(_SEED_IDENTITIES), {"space": ref.id})
        await conn.execute(text(_SEED_REGIONS))
        await conn.execute(text("ANALYZE ih_identities"))
        await conn.execute(text("ANALYZE ih_region_states"))
    return space


async def test_due_regions_reads_only_rows_with_deadline(
    hub: IdentityHub, big_space: SpaceHandle, db: AsyncEngine
) -> None:
    """R2-D15: снятие временных состояний не перебирает все строки региона."""

    async def call() -> None:
        async with big_space._tx(own=True) as tx:
            assert await tx.due_regions("standing", 500)

    plan = await _plan(hub, db, "ih_region_states", call)

    assert "ix_ih_region_states_due" in plan, plan
    assert "Seq Scan" not in plan, plan


async def test_stale_identities_walks_probe_queue_index(
    hub: IdentityHub, big_space: SpaceHandle, db: AsyncEngine
) -> None:
    """R2-D15: очередь сверки читается по индексу, а не сортировкой всех аккаунтов сервиса."""

    async def call() -> None:
        async with big_space._tx(own=True) as tx:
            assert len(await tx.stale_identities("fake_tg", "standing", "", 3600, 100)) == 100

    plan = await _plan(hub, db, "probe_attempted_at", call)

    assert "ix_ih_identities_probe_queue" in plan, plan
    assert "Seq Scan" not in plan, plan
