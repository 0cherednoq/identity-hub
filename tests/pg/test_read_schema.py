"""IMP-12 (I5.1): публичная read-схема Core — join с таблицами приложения, `among=`, db_schema."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.types import Uuid

from identity_hub import IdentityCreate, IdentityHub, Lifecycle, Override, StaticSecretsProvider
from identity_hub.postgres.migrate import migrate
from identity_hub.postgres.read_schema import READ_SCHEMA_VERSION, READ_TABLES, ReadSchema
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.pg

# Колонки, которых не должно быть ни в одной read-таблице и ни в одном SQL read-схемы.
FORBIDDEN = {
    "token",
    "fencing",
    "fencing_seq",
    "idempotency_key",
    "ciphertext",
    "nonce",
    "wrapped_dek",
    "key_id",
    "profile_ciphertext",
    "profile_nonce",
    "profile_wrapped_dek",
    "profile_key_id",
    "fingerprint",
    "version",
    "pending_observation",
    "tx_id",
    "search_text",
}

EXPECTED_COLUMNS = {
    "identities": {
        "id", "space_id", "service", "external_ref", "normalized_ref", "lifecycle", "standing",
        "standing_code",
        "standing_until", "auth", "availability", "availability_until", "requirements",
        "verified_factors", "tags", "labels", "profile", "attributes", "note", "error_score",
        "revision", "created_at", "updated_at",
    },
    "leases": {
        "identity_id", "space_id", "lease_id", "pool", "holder", "acquired_at", "heartbeat_at",
        "expires_at",
    },
    "lease_history": {
        "lease_id", "space_id", "identity_id", "pool", "holder", "proxy_id", "acquired_at",
        "last_heartbeat_at", "released_at", "duration", "end_reason", "operations", "succeeded",
        "failed", "last_error",
    },
    "region_states": {
        "identity_id", "space_id", "region", "key", "state", "code", "until", "changed_at",
        "change_source", "changed_by", "change_reason", "synced_at", "observed_at", "raw_status",
        "pinned_until",
    },
    "events": {
        "event_id", "space_id", "type", "severity", "service", "identity_id", "proxy_id",
        "lease_id", "sequence", "actor", "data", "external", "occurred_at",
    },
    "proxies": {
        "id", "space_id", "group", "scheme", "host", "port", "status", "error_score", "fail_streak",
        "next_check_at", "status_changed_at", "status_source", "checked_at", "pinned_until",
        "max_identities", "bound_count", "created_at",
    },
}  # fmt: skip


def _farm_table(schema: str | None) -> Table:
    """Таблица приложения: «энергия» аккаунта в игре."""
    return Table(
        f"farm_accounts_{uuid4().hex[:8]}",
        MetaData(),
        Column("identity_id", Uuid, primary_key=True),
        Column("energy", Integer, nullable=False),
        schema=schema,
    )


@pytest.fixture
async def custom_schema(db: AsyncEngine) -> AsyncIterator[str]:
    name = f"ih_read_{uuid4().hex[:10]}"
    yield name
    async with db.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


async def _seed(hub: IdentityHub, farm: Table, engine: AsyncEngine) -> dict[str, object]:
    space = hub.space(f"read-{uuid4().hex[:6]}", create=True)
    ids = {}
    for ref, energy in (("+1", 900), ("+2", 100), ("+3", 700)):
        identity = await space.identities.create(
            IdentityCreate(
                service="fake_tg", external_ref=f"{space.slug}{ref}", session={"auth_key": "k"},
                lifecycle=Lifecycle.READY,
            )
        )  # fmt: skip
        ids[ref] = identity.id
        async with engine.begin() as conn:
            await conn.execute(farm.insert().values(identity_id=identity.id, energy=energy))
    await space.identities.override(
        ids["+3"], Override("standing", "", "banned", reason="тест", actor="test")
    )
    return {"space": space, **ids}


async def _join_example(hub: IdentityHub, engine: AsyncEngine, farm: Table) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(farm.metadata.create_all)
    try:
        seeded = await _seed(hub, farm, engine)
        space = seeded["space"]
        ref = await space.ref()  # type: ignore[attr-defined]
        identities = hub.read.identities
        # пример из IMP-12: join с таблицей приложения
        stmt = (
            select(farm.c.identity_id)
            .join(identities, identities.c.id == farm.c.identity_id)
            .where(identities.c.space_id == ref.id, identities.c.standing == "good", farm.c.energy >= 500)
        )
        async with engine.connect() as conn:
            assert set((await conn.execute(stmt)).scalars()) == {seeded["+1"]}

        pool = space.pools.define("farmers", service="fake_tg")  # type: ignore[attr-defined]
        async with pool.lease(among=stmt) as lease:
            assert lease.identity_id == seeded["+1"]
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(farm.metadata.drop_all)


async def test_join_with_application_table(hub: IdentityHub, db: AsyncEngine) -> None:
    await _join_example(hub, db, _farm_table(None))


async def test_join_in_custom_db_schema(db: AsyncEngine, custom_schema: str) -> None:
    await migrate(db, db_schema=custom_schema)
    hub = IdentityHub(
        async_sessionmaker(db, expire_on_commit=False),
        StaticSecretsProvider(TEST_KEYS, "k1"),
        [FAKE_TG],
        db_schema=custom_schema,
    )
    await hub.start()
    assert hub.read.identities.schema == custom_schema
    # таблица приложения в своей схеме; библиотека транслирует только таблицы без схемы
    await _join_example(hub, db, _farm_table("public"))


async def test_read_tables_expose_state_without_secrets(hub: IdentityHub, db: AsyncEngine) -> None:
    space = hub.space(f"read-{uuid4().hex[:6]}", create=True)
    identity = await space.identities.create(
        IdentityCreate(
            service="fake_tg", external_ref="+700", session={"auth_key": "k"},
            credentials={"phone": "+700", "password": "secret-pass"}, lifecycle=Lifecycle.READY,
        )
    )  # fmt: skip
    await space.proxies.create("socks5://user:proxy-pass@9.9.9.9:1080", group="res")
    async with space.pools.define("p", service="fake_tg").lease(), db.connect() as conn:
        lease = (await conn.execute(select(hub.read.leases))).mappings().one()
    read = hub.read
    async with db.connect() as conn:
        row = (
            (await conn.execute(select(read.identities).where(read.identities.c.id == identity.id)))
            .mappings()
            .one()
        )
        proxy = (await conn.execute(select(read.proxies))).mappings().one()
        history = (await conn.execute(select(read.lease_history))).mappings().all()
        events = (await conn.execute(select(read.events.c.type))).scalars().all()
        regions = (await conn.execute(select(read.region_states))).mappings().all()
    assert row["auth"] == "authenticated" and row["revision"] > identity.revision  # события аренды
    assert proxy["group"] == "res" and proxy["host"] == "9.9.9.9"
    assert lease["identity_id"] == identity.id and "token" not in lease
    assert len(history) == 1 and "identity.registered" in events and regions
    dumped = repr([dict(row), dict(proxy), dict(lease), *map(dict, history), *map(dict, regions)])
    assert "secret-pass" not in dumped and "proxy-pass" not in dumped


def test_column_allowlist_and_version() -> None:
    read = ReadSchema()
    assert READ_SCHEMA_VERSION == 1 and read.version == READ_SCHEMA_VERSION
    assert set(READ_TABLES) == set(EXPECTED_COLUMNS)
    for name, expected in EXPECTED_COLUMNS.items():
        table = getattr(read, name)
        assert set(table.c.keys()) == expected, name
        assert not {column.name for column in table.c} & FORBIDDEN, name


def test_sql_snapshot_has_no_secret_columns() -> None:
    read = ReadSchema("custom")
    for name in READ_TABLES:
        sql = str(select(getattr(read, name)).compile(dialect=postgresql.dialect()))
        assert f"custom.{READ_TABLES[name][0]}" in sql
        words = set(sql.replace(",", " ").replace(".", " ").split())
        assert not words & FORBIDDEN, (name, words & FORBIDDEN)


def test_public_import_contract() -> None:
    import identity_hub.postgres.read_schema as module

    assert set(module.__all__) == {"READ_SCHEMA_VERSION", "READ_TABLES", "ReadSchema"}
    hub_read = IdentityHub.read
    assert hub_read.__doc__ and "IMP-12" in hub_read.__doc__
