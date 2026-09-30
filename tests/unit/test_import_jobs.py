from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from identity_hub.clock import FakeClock
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub
from identity_hub.importing import ImportRecord, ImportRunner
from identity_hub.testing import MemoryStore
from tests.fixtures.services import FAKE_TG


@pytest.fixture
async def import_hub() -> IdentityHub:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"k1": b"1" * 32}, "k1"),
        services=[FAKE_TG],
        store=MemoryStore(clock),
        clock=clock,
        strict_events=True,
    )
    await hub.start()
    return hub


async def test_staged_import_finishes_and_removes_encrypted_payloads(import_hub: IdentityHub) -> None:
    space = import_hub.space("jobs")
    run_id = await space.stage_import(
        service="fake_tg",
        source="accounts.jsonl",
        actor="admin:operator",
        labels={"batch": "nightly"},
        records=[
            ImportRecord(external_ref="+100", credentials={"phone": "+100"}),
            ImportRecord(external_ref="+101", credentials={"phone": "+101"}),
        ],
    )

    assert (await space.import_run(run_id)).state == "pending"  # type: ignore[union-attr]
    assert await import_hub.import_runner.run_once() == 2
    completed = await space.import_run(run_id)

    assert completed is not None
    assert completed.state == "succeeded"
    assert (completed.total, completed.processed, completed.created) == (2, 2, 2)
    created_ids = completed.report["created_ids"]
    assert isinstance(created_ids, list) and len(created_ids) == 2
    store = import_hub.store
    assert isinstance(store, MemoryStore)
    assert store._state.import_items == {}
    assert "phone" not in repr(completed)
    assert any(
        event.type == "identity.imported" and event.data.get("run_id") == str(run_id)
        for event, _, _ in store._state.events
    )


async def test_expired_claim_is_resumed_without_duplicate(import_hub: IdentityHub) -> None:
    space = import_hub.space("resume")
    run_id = await space.stage_import(
        service="fake_tg",
        source="resume.csv",
        records=[ImportRecord(external_ref="+200", credentials={"phone": "+200"})],
    )
    runner = ImportRunner(import_hub, batch_size=1, claim_for=timedelta(minutes=5))
    async with space._tx(own=True) as tx:
        run = (await tx.active_import_runs())[0]
        claimed = await tx.claim_import_items(
            run_id,
            worker="crashed-worker",
            limit=1,
            claim_for=timedelta(minutes=5),
        )
    assert len(await runner._process(space, run, claimed)) == 1

    clock = import_hub.clock
    assert isinstance(clock, FakeClock)
    clock.advance(timedelta(minutes=6))
    assert await runner.run_once() == 1

    completed = await space.import_run(run_id)
    assert completed is not None
    assert completed.state == "succeeded" and completed.created == 1
    async with space._tx() as tx:
        found = await tx.find_by_refs([("fake_tg", "+200")])
    assert len(found) == 1


async def test_cancel_waits_for_claimed_batch_boundary(import_hub: IdentityHub) -> None:
    space = import_hub.space("cancel")
    run_id = await space.stage_import(
        service="fake_tg",
        source="cancel.jsonl",
        records=[ImportRecord(external_ref="+300"), ImportRecord(external_ref="+301")],
    )
    async with space._tx(own=True) as tx:
        claimed = await tx.claim_import_items(
            run_id,
            worker="worker",
            limit=1,
            claim_for=timedelta(minutes=5),
        )
    assert await space.cancel_import(run_id)
    assert (await space.import_run(run_id)).state == "cancelling"  # type: ignore[union-attr]

    run = await space.import_run(run_id)
    assert run is not None
    results = await import_hub.import_runner._process(space, run, claimed)
    async with space._tx(own=True) as tx:
        completed = await tx.finish_import_items(run_id, results)

    assert completed is not None
    assert completed.state == "cancelled"
    assert completed.processed == 1
