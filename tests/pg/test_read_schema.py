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
    "services": {"name", "title", "url", "synced_at"},
    "identities": {
        "id", "space_id", "service", "external_ref", "normalized_ref", "lifecycle", "standing",
        "standing_code",
        "standing_until", "auth", "availability", "availability_until", "requirements",
        "verified_factors", "verification_tier_rank", "tags", "labels", "profile", "attributes",
        "note", "error_score",
        "revision", "created_at", "updated_at",
    },
    "leases": {
        "identity_id", "space_id", "lease_id", "pool", "holder", "acquired_at", "heartbeat_at",
        "expires_at", "purpose", "ref", "hold_deadline",
    },
    "lease_history": {
        "lease_id", "space_id", "identity_id", "pool", "holder", "proxy_id", "acquired_at",
        "last_heartbeat_at", "released_at", "duration", "end_reason", "operations", "succeeded",
        "failed", "last_error", "purpose", "ref", "hold_deadline",
    },
    "region_states": {
        "identity_id", "space_id", "region", "key", "state", "code", "until", "changed_at",
        "change_source", "changed_by", "change_reason", "synced_at", "observed_at", "raw_status",
        "pinned_until",
    },
    "events": {
        "event_id", "space_id", "type", "severity", "service", "identity_id", "proxy_id",
        "lease_id", "sequence", "revision", "actor", "data", "external", "occurred_at",
    },
    "proxies": {
        "id", "space_id", "group", "scheme", "host", "port", "status", "error_score", "fail_streak",
        "next_check_at", "status_changed_at", "status_source", "checked_at", "pinned_until",
        "max_identities", "bound_count", "created_at", "enabled", "disabled_reason", "disabled_by",
        "disabled_at",
    },
    "proxy_bindings": {"identity_id", "space_id", "proxy_id", "bound_at"},
    "identity_links": {"identity_id", "space_id", "role", "linked_identity_id", "created_at"},
    # C8.2: метаданные записей без секретов — колонки перечислены и в tests/pg/test_read_schema_access.py
    "credentials": {
        "id", "identity_id", "space_id", "kind", "name", "public_value", "lifecycle", "expires_at",
        "last_verified_at", "last_rejected_at", "reject_code", "revision", "created_at",
        "updated_at",
    },
    "sessions": {
        "id", "identity_id", "space_id", "slot", "status", "expires_at", "last_verified_at",
        "last_rejected_at", "reject_code", "revision", "created_at", "updated_at", "revoked_at",
    },
}  # fmt: skip

IDENTITY_V4_TYPES = {
    "id": "CHAR(32)",
    "space_id": "CHAR(32)",
    "service": "VARCHAR(64)",
    "external_ref": "VARCHAR(256)",
    "normalized_ref": "VARCHAR(256)",
    "lifecycle": "VARCHAR(16)",
    "standing": "VARCHAR(16)",
    "standing_code": "VARCHAR(64)",
    "standing_until": "DATETIME",
    "auth": "VARCHAR(24)",
    "availability": "VARCHAR(16)",
    "availability_until": "DATETIME",
    "requirements": "JSONB",
    "verified_factors": "ARRAY",
    "tags": "ARRAY",
    "labels": "JSONB",
    "profile": "JSONB",
    "attributes": "JSONB",
    "note": "TEXT",
    "error_score": "FLOAT",
    "revision": "BIGINT",
    "created_at": "DATETIME",
    "updated_at": "DATETIME",
}


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
        , actor="test")  # fmt: skip
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
    , actor="test")  # fmt: skip
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
        result = await conn.execute(select(read.events.c.type, read.events.c.revision))
        events = {event_type: revision for event_type, revision in result}
        regions = (await conn.execute(select(read.region_states))).mappings().all()
    # R2-D23: ревизия агрегата, а не счётчик событий — аренда её не меняет (D-205)
    assert row["auth"] == "authenticated" and row["revision"] == identity.revision == 1
    assert proxy["group"] == "res" and proxy["host"] == "9.9.9.9"
    assert lease["identity_id"] == identity.id and "token" not in lease
    assert len(history) == 1 and events["identity.registered"] == 1 and regions
    dumped = repr([dict(row), dict(proxy), dict(lease), *map(dict, history), *map(dict, regions)])
    assert "secret-pass" not in dumped and "proxy-pass" not in dumped


async def test_proxy_bindings_join_accounts_with_proxies(hub: IdentityHub, db: AsyncEngine) -> None:
    """C3.1: аккаунты с их прокси и число аккаунтов на прокси — одним запросом с join."""
    space = hub.space(f"read-{uuid4().hex[:6]}", create=True)
    ref = await space.ref()
    ids = [
        (
            await space.identities.create(
                IdentityCreate(
                    service="fake_tg", external_ref=f"{space.slug}+{n}", lifecycle=Lifecycle.READY
                ),
                actor="test",
            )
        ).id
        for n in range(3)
    ]
    busy = await space.proxies.create("socks5://10.1.0.1:1080", group="res")
    idle = await space.proxies.create("socks5://10.1.0.2:1080", group="res")
    for identity_id in ids[:2]:
        await space.proxies.rebind(identity_id, busy.id, reason="тест", actor="test")

    read = hub.read
    accounts = (
        select(read.identities.c.id, read.proxies.c.host)
        .join(read.proxy_bindings, read.proxy_bindings.c.identity_id == read.identities.c.id, isouter=True)
        .join(read.proxies, read.proxies.c.id == read.proxy_bindings.c.proxy_id, isouter=True)
        .where(read.identities.c.space_id == ref.id)
    )
    counts = select(read.proxies.c.id, read.proxies.c.bound_count).where(read.proxies.c.space_id == ref.id)
    async with db.connect() as conn:
        hosts = {identity_id: host for identity_id, host in await conn.execute(accounts)}
        bound = {proxy_id: count for proxy_id, count in await conn.execute(counts)}
        binding = (
            (await conn.execute(select(read.proxy_bindings).where(read.proxy_bindings.c.space_id == ref.id)))
            .mappings()
            .first()
        )
    assert hosts == {ids[0]: "10.1.0.1", ids[1]: "10.1.0.1", ids[2]: None}
    assert bound == {busy.id: 2, idle.id: 0}
    assert binding is not None and binding["proxy_id"] == busy.id and binding["bound_at"] is not None


async def test_identity_links_support_public_anti_join(hub: IdentityHub, db: AsyncEngine) -> None:
    space = hub.space(f"read-links-{uuid4().hex[:6]}", create=True)
    ref = await space.ref()
    identities = [
        await space.identities.create(
            IdentityCreate(service="fake_tg", external_ref=f"{space.slug}-{index}"),
            actor="test",
        )
        for index in range(3)
    ]
    await space.links.add(identities[0].id, "backup", identities[2].id, actor="test")
    read = hub.read
    stmt = (
        select(read.identities.c.id)
        .join(
            read.identity_links,
            (read.identity_links.c.identity_id == read.identities.c.id)
            & (read.identity_links.c.role == "backup"),
            isouter=True,
        )
        .where(
            read.identities.c.space_id == ref.id,
            read.identity_links.c.linked_identity_id.is_(None),
        )
    )
    async with db.connect() as conn:
        assert set((await conn.execute(stmt)).scalars()) == {identities[1].id, identities[2].id}


def test_column_allowlist_and_version() -> None:
    read = ReadSchema()
    assert READ_SCHEMA_VERSION == 5 and read.version == READ_SCHEMA_VERSION
    assert set(READ_TABLES) == set(EXPECTED_COLUMNS)
    for name, expected in EXPECTED_COLUMNS.items():
        table = getattr(read, name)
        assert set(table.c.keys()) == expected, name
        assert not {column.name for column in table.c} & FORBIDDEN, name
    assert {name: str(read.identities.c[name].type) for name in IDENTITY_V4_TYPES} == IDENTITY_V4_TYPES


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
