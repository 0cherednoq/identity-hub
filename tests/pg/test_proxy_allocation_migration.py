"""PA1.1: обновление сохраняет выданный proxy и не превращает старые binding в резервы."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from identity_hub import IdentityHub, ProxyBindingPolicy, ProxyPolicy, StaticSecretsProvider
from identity_hub.postgres.migrate import SCHEMA_HEAD, alembic_config, migrate
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.pg


@pytest.fixture
async def allocation_schema(db: AsyncEngine) -> AsyncIterator[str]:
    name = f"ih_pa_{uuid4().hex[:10]}"
    yield name
    async with db.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


async def test_upgrade_preserves_legacy_binding_and_actual_lease_proxy(
    db: AsyncEngine, allocation_schema: str
) -> None:
    schema = allocation_schema
    await migrate(db, db_schema=schema, revision="0043")
    space, identity, bound, issued, lease, token = (uuid4() for _ in range(6))
    async with db.begin() as conn:
        await conn.execute(text(f"INSERT INTO \"{schema}\".ih_services (name, title) VALUES ('svc', 'Svc')"))
        await conn.execute(
            text(f"INSERT INTO \"{schema}\".ih_spaces (id, slug, title) VALUES (:id, 's', 'S')"),
            {"id": space},
        )
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_identities '
                "(id, space_id, service, external_ref, normalized_ref, lifecycle, standing, auth_state, "
                "availability, error_score, fencing_seq, event_seq) "
                "VALUES (:id, :space, 'svc', 'a', 'a', 'ready', 'good', 'authenticated', "
                "'available', 0, 1, 0)"
            ),
            {"id": identity, "space": space},
        )
        for proxy_id, host in ((bound, "127.0.0.1"), (issued, "127.0.0.2")):
            await conn.execute(
                text(
                    f'INSERT INTO "{schema}".ih_proxies '
                    "(id, space_id, pool, scheme, host, port, fingerprint, status, error_score, fail_streak, "
                    "status_source, max_identities, bound_count, payload) "
                    "VALUES (:id, :space, 'g', 'http', :host, 80, :fingerprint, 'alive', 0, 0, "
                    "'command', 1, :n, '{}'::jsonb)"
                ),
                {
                    "id": proxy_id,
                    "space": space,
                    "host": host,
                    "fingerprint": proxy_id.hex,
                    "n": 1 if proxy_id == bound else 0,
                },
            )
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_bindings (identity_id, space_id, proxy_id) '
                "VALUES (:id, :space, :proxy)"
            ),
            {"id": identity, "space": space, "proxy": bound},
        )
        params = {
            "id": identity,
            "space": space,
            "lease": lease,
            "token": token,
            "proxy": issued,
            "until": datetime.now(UTC) + timedelta(minutes=5),
        }
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_lease_history '
                "(lease_id, space_id, identity_id, token, fencing, pool, holder, proxy_id, "
                "succeeded, failed) "
                "VALUES (:lease, :space, :id, :token, 1, 'p', 'w', :proxy, 0, 0)"
            ),
            params,
        )
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_leases '
                "(identity_id, space_id, lease_id, token, fencing, pool, holder, expires_at) "
                "VALUES (:id, :space, :lease, :token, 1, 'p', 'w', :until)"
            ),
            params,
        )
    await migrate(db, db_schema=schema)
    async with db.connect() as conn:
        group = (
            await conn.execute(
                text(f'SELECT name, capacity_accounting, revision FROM "{schema}".ih_proxy_groups')
            )
        ).one()
        assert tuple(group) == ("g", "bindings", 1)
        row = (
            await conn.execute(text(f'SELECT proxy_id, proxy_allocation_id FROM "{schema}".ih_leases'))
        ).one()
        assert tuple(row) == (issued, None)
        binding = (
            await conn.execute(text(f'SELECT proxy_id, allocation_id FROM "{schema}".ih_bindings'))
        ).one()
        assert tuple(binding) == (bound, None)
        assert (
            await conn.execute(text(f'SELECT count(*) FROM "{schema}".ih_proxy_allocations'))
        ).scalar_one() == 0
        assert (
            await conn.execute(
                text(
                    "SELECT sum(bound_count), sum(reserved_count), sum(draining_count) "
                    f'FROM "{schema}".ih_proxies'
                )
            )
        ).one() == (1, 0, 0)

    allocation = uuid4()
    async with db.begin() as conn:
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_proxy_allocations '
                "(id, space_id, identity_id, proxy_id, state) "
                "VALUES (:allocation, :space, :identity, :proxy, 'bound')"
            ),
            {"allocation": allocation, "space": space, "identity": identity, "proxy": bound},
        )
    with pytest.raises(RuntimeError, match="drain active assignments"):
        async with db.begin() as conn:
            await conn.run_sync(lambda sync: command.downgrade(alembic_config(schema, sync), "0043"))
    async with db.begin() as conn:
        await conn.execute(
            text(f'DELETE FROM "{schema}".ih_proxy_allocations WHERE id = :id'), {"id": allocation}
        )
        await conn.run_sync(lambda sync: command.downgrade(alembic_config(schema, sync), "0043"))
    async with db.connect() as conn:
        assert (
            await conn.execute(text(f'SELECT proxy_id FROM "{schema}".ih_bindings'))
        ).scalar_one() == bound


async def test_downgrade_requires_disabling_empty_managed_group(
    db: AsyncEngine, allocation_schema: str
) -> None:
    schema = allocation_schema
    await migrate(db, db_schema=schema)
    hub = IdentityHub(
        async_sessionmaker(db, expire_on_commit=False),
        StaticSecretsProvider({"k": b"\x33" * 32}, "k"),
        [FAKE_TG],
        db_schema=schema,
    )
    await hub.start()
    space = hub.space("switch", create=True)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="ops", reason="enable")
    with pytest.raises(RuntimeError, match="switch groups to bindings"):
        async with db.begin() as conn:
            await conn.run_sync(lambda sync: command.downgrade(alembic_config(schema, sync), "0044"))
    await space.proxies.configure_group("g", capacity_accounting="bindings", actor="ops", reason="disable")
    async with db.begin() as conn:
        await conn.run_sync(lambda sync: command.downgrade(alembic_config(schema, sync), "0044"))
    async with db.connect() as conn:
        assert (
            await conn.execute(text(f'SELECT version_num FROM "{schema}".ih_alembic_version'))
        ).scalar_one() == "0044"


async def test_active_foreign_keys_and_retained_tombstones_survive_resource_delete(
    db: AsyncEngine, allocation_schema: str
) -> None:
    schema = allocation_schema
    await migrate(db, db_schema=schema)

    hub = IdentityHub(
        async_sessionmaker(db, expire_on_commit=False),
        StaticSecretsProvider({"k": b"\x33" * 32}, "k"),
        [FAKE_TG],
        db_schema=schema,
    )
    await hub.start()
    space = hub.space("tombstones", create=True)
    proxy = await space.proxies.create("http://127.0.0.1:8942", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="ops", reason="enable")
    identity = (await register(space, service="fake_tg", external_ref="a")).identity_id
    await space.proxies.rebind(identity, proxy.id, actor="ops", reason="trusted")
    async with space._tx() as tx:
        allocation = (await tx.proxy_allocations(identity_id=identity))[0]
    for table, resource_id in (("ih_proxies", proxy.id), ("ih_identities", identity)):
        with pytest.raises(IntegrityError):
            async with db.begin() as conn:
                await conn.execute(
                    text(f'DELETE FROM "{schema}".{table} WHERE id = :id'), {"id": resource_id}
                )
    await space.proxies.delete(proxy.id, unbind=True, actor="ops")
    await space.identities.delete(identity, actor="ops")
    async with space._tx() as tx:
        row = await tx.proxy_allocation(allocation.id)
        assert row.state == "released" and row.proxy_id == proxy.id and row.identity_id == identity
    async with db.connect() as conn:
        refs = (
            await conn.execute(
                text(f'SELECT active_identity_id, active_proxy_id FROM "{schema}".ih_proxy_allocations')
            )
        ).one()
        assert tuple(refs) == (None, None)
    with pytest.raises(RuntimeError, match="retained tombstones"):
        async with db.begin() as conn:
            await conn.run_sync(lambda sync: command.downgrade(alembic_config(schema, sync), "0045"))
    async with db.begin() as conn:
        assert (
            await conn.execute(text(f'SELECT version_num FROM "{schema}".ih_alembic_version'))
        ).scalar_one() == SCHEMA_HEAD
        # Имитация явного retention после исчезновения всех ссылок, не часть downgrade.
        await conn.execute(
            text(f'DELETE FROM "{schema}".ih_proxy_allocations WHERE id = :id'), {"id": allocation.id}
        )
        await conn.run_sync(lambda sync: command.downgrade(alembic_config(schema, sync), "0045"))
    await migrate(db, db_schema=schema)


async def test_request_snapshot_downgrade_requires_drain(db: AsyncEngine, allocation_schema: str) -> None:
    schema = allocation_schema
    await migrate(db, db_schema=schema)
    hub = IdentityHub(
        async_sessionmaker(db, expire_on_commit=False),
        StaticSecretsProvider({"k": b"\x33" * 32}, "k"),
        [FAKE_TG],
        db_schema=schema,
    )
    await hub.start()
    space = hub.space("lease-request", create=True)
    await space.proxies.create("http://127.0.0.1:8973", group="g", max_identities=1)
    await space.proxies.configure_group("g", capacity_accounting="allocations", actor="test", reason="enable")
    identity = (
        await register(
            space, service="fake_tg", external_ref="a", lifecycle="ready", session={"auth_key": "k"}
        )
    ).identity_id
    pool = space.pools.define(
        "p", service="fake_tg", proxy_policy=ProxyPolicy(group="g", binding=ProxyBindingPolicy())
    )
    lease = await pool.acquire(among=[identity])
    with pytest.raises(RuntimeError, match="drain live leases"):
        async with db.begin() as conn:
            await conn.run_sync(lambda sync: command.downgrade(alembic_config(schema, sync), "0046"))
    await lease.release()
    async with db.begin() as conn:
        await conn.run_sync(lambda sync: command.downgrade(alembic_config(schema, sync), "0046"))
    await migrate(db, db_schema=schema)
