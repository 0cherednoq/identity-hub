"""A4.1: space timezone and secret-free durable admin read contracts."""

from __future__ import annotations

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import ValidationFailed
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import ImportRunRecord, ProviderOperationRecord
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg


async def test_space_timezone_defaults_and_uses_hub_setting(hub: IdentityHub) -> None:
    default = await hub.space("default-timezone").ref()
    custom = await hub._store.ensure_space("custom-timezone", timezone="Europe/Simferopol")

    assert default.timezone == "UTC"
    assert custom.timezone == "Europe/Simferopol"
    listed = {space.slug: space.timezone for space in await hub.list_spaces()}
    assert listed["default-timezone"] == "UTC"
    assert listed["custom-timezone"] == "Europe/Simferopol"


async def test_durable_admin_reads_are_space_scoped(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    identity_id = await make_identity(space)
    other = hub.space("admin-state-other")
    other_identity_id = await make_identity(other, external_ref="other-admin-state")
    ref = await space.ref()
    other_ref = await other.ref()
    async with sessionmaker() as session, session.begin():
        own_run = (
            await session.execute(
                insert(ImportRunRecord)
                .values(
                    space_id=ref.id,
                    service="fake_tg",
                    actor="admin:operator",
                    source="accounts.jsonl",
                    state="running",
                    total=3,
                    processed=1,
                    report={"errors": []},
                )
                .returning(ImportRunRecord.id)
            )
        ).scalar_one()
        await session.execute(
            insert(ImportRunRecord).values(
                space_id=other_ref.id,
                service="fake_tg",
                actor="admin:other",
                source="other.jsonl",
            )
        )
        await session.execute(
            insert(ProviderOperationRecord),
            [
                {
                    "space_id": ref.id,
                    "identity_id": identity_id,
                    "kind": "login",
                    "state": "retryable",
                    "attempt": 2,
                    "result": {"outcome": "temporary_error"},
                    "error_code": "provider_unavailable",
                },
                {
                    "space_id": other_ref.id,
                    "identity_id": other_identity_id,
                    "kind": "login",
                    "state": "pending",
                },
            ],
        )

    runs = await space.admin.import_runs()
    operation = await space.admin.provider_operations(states=frozenset({"retryable"}))

    assert [run.id for run in runs] == [own_run]
    assert (await space.imports.get(own_run)) == runs[0]
    assert len(operation) == 1
    assert operation[0].identity_id == identity_id
    assert operation[0].result == {"outcome": "temporary_error"}
    assert "other.jsonl" not in repr(runs)
    assert str(other_identity_id) not in repr(operation)
    with pytest.raises(ValidationFailed, match="unknown provider operation states"):
        await space.admin.provider_operations(states=frozenset({"mystery"}))
    with pytest.raises(ValidationFailed, match="limit"):
        await space.admin.import_runs(limit=0)
