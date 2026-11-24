"""PA5.2: факт приложения и confirm из примера имеют один commit/rollback."""

from uuid import uuid4

import pytest
from examples.proxy_assignment import (
    FakeSite,
    account,
    configure_proxy_group,
    confirm_in_transaction,
    site_service,
)
from sqlalchemy import Column, MetaData, String, Table, func, select
from sqlalchemy.types import Uuid

pytestmark = pytest.mark.pg


@pytest.mark.parametrize("rollback", [False, True])
async def test_public_example_transaction(hub, db, sessionmaker, rollback):
    hub.register_service(site_service(FakeSite()))
    await hub.start()
    space = hub.space("atomic-proxy-example", create=True)
    proxy = await configure_proxy_group(space)
    identity = await account(space, "atomic")
    lease = await space.pools.define("checks", service="proxy_example_site").acquire(among=[identity])
    facts = Table(
        f"app_proxy_checks_{uuid4().hex[:8]}",
        MetaData(),
        Column("identity_id", Uuid, primary_key=True),
        Column("verdict", String, nullable=False),
    )
    async with db.begin() as conn:
        await conn.run_sync(facts.metadata.create_all)

    async def write_result(session, checked_identity):
        await session.execute(facts.insert().values(identity_id=checked_identity, verdict="valid"))

    try:
        try:
            async with sessionmaker() as session, session.begin():
                result = await confirm_in_transaction(space, lease, session, write_result)
                assert result.state == "bound" and lease.proxy_assignment_state == "reserved"
                if rollback:
                    raise RuntimeError("application rollback")
        except RuntimeError as exc:
            assert rollback and str(exc) == "application rollback"
        async with db.connect() as conn:
            assert (await conn.execute(select(func.count()).select_from(facts))).scalar_one() == int(
                not rollback
            )
        detail = (await space.admin.proxy_detail(proxy)).proxy
        assert (detail.bound_count, detail.reserved_count) == ((0, 1) if rollback else (1, 0))
        assert lease.proxy_assignment_state == "reserved"
    finally:
        await lease.release()
        async with db.begin() as conn:
            await conn.run_sync(facts.metadata.drop_all)
