from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.importing import ImportRecord, ImportRunner
from identity_hub.postgres.models import IdentityRecord, ImportItemRecord

pytestmark = pytest.mark.pg


async def test_postgres_staging_is_encrypted_and_removed_after_success(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    secret = "+380990001111"
    run_id = await space.stage_import(
        service="fake_tg",
        source="encrypted.jsonl",
        actor="admin:operator",
        labels={"batch": "postgres"},
        records=[
            ImportRecord(
                external_ref=secret,
                credentials={"phone": secret},
                note="sensitive staging note",
            )
        ],
    )

    async with sessionmaker() as session:
        item = (await session.execute(select(ImportItemRecord))).scalar_one()
        assert secret.encode() not in item.ciphertext
        assert b"sensitive staging note" not in item.ciphertext

    assert await hub.import_runner.run_once() == 1
    completed = await space.import_run(run_id)

    assert completed is not None
    assert completed.state == "succeeded"
    assert (completed.total, completed.processed, completed.created) == (1, 1, 1)
    assert secret not in repr(completed)
    async with sessionmaker() as session:
        remaining = (await session.execute(select(func.count()).select_from(ImportItemRecord))).scalar_one()
    assert remaining == 0


async def test_postgres_expired_claim_resumes_without_duplicate_identity(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    external_ref = "+380990002222"
    run_id = await space.stage_import(
        service="fake_tg",
        source="resume.jsonl",
        records=[ImportRecord(external_ref=external_ref, credentials={"phone": external_ref})],
    )
    runner = ImportRunner(hub, batch_size=1, claim_for=timedelta(minutes=5))
    async with space._tx(own=True) as tx:
        run = (await tx.active_import_runs(limit=1))[0]
        claimed = await tx.claim_import_items(
            run_id,
            worker="worker-that-crashed",
            limit=1,
            claim_for=timedelta(minutes=5),
        )
    results = await runner._process(space, run, claimed)
    assert len(results) == 1 and results[0].outcome == "created"

    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(ImportItemRecord)
            .where(ImportItemRecord.run_id == run_id)
            .values(claim_expires_at=func.now() - timedelta(seconds=1))
        )

    assert await ImportRunner(hub, batch_size=1).run_once() == 1
    completed = await space.import_run(run_id)
    assert completed is not None
    assert completed.state == "succeeded"
    assert (completed.processed, completed.created) == (1, 1)
    async with sessionmaker() as session:
        identities = (
            await session.execute(
                select(func.count())
                .select_from(IdentityRecord)
                .where(IdentityRecord.external_ref == external_ref)
            )
        ).scalar_one()
    assert identities == 1
