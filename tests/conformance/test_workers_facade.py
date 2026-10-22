"""I5.2 (`IMP-20`, `D-327`): `hub.workers.*.run_once(limit)` как единое ядро фоновых работ."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle, ValidationFailed
from identity_hub.workers import WorkerRun

pytestmark = [pytest.mark.conformance]

WORKERS = ("dispatch", "maintenance", "reconcile", "provider_operations", "acceptance")


def _facades(hub: IdentityHub) -> list[object]:
    return [getattr(hub.workers, name) for name in WORKERS]


async def test_every_worker_exposes_run_once_with_limit(hub_under_test: IdentityHub) -> None:
    for name, facade in zip(WORKERS, _facades(hub_under_test), strict=True):
        run = await facade.run_once(limit=5)
        assert isinstance(run, WorkerRun)
        assert run.worker == name and run.limit == 5
        assert run.processed == 0 and not run.truncated  # пустая очередь


async def test_limit_zero_does_no_work_and_negative_is_rejected(hub_under_test: IdentityHub) -> None:
    for facade in _facades(hub_under_test):
        run = await facade.run_once(limit=0)
        assert run.processed == 0 and run.limit == 0 and not run.truncated
        with pytest.raises(ValidationFailed):
            await facade.run_once(limit=-1)


async def test_report_is_serializable_and_safe(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    identity = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+70000000001", session={"auth_key": "secret-key"}),
        actor="test",
    )
    run = await hub_under_test.workers.dispatch.run_once(limit=10)
    dumped = run.dump()
    assert json.loads(json.dumps(dumped)) == dumped
    assert dumped["worker"] == "dispatch" and dumped["limit"] == 10
    assert all(isinstance(value, int) for value in run.counts.values())
    text = f"{dumped} {run!r}"
    assert "secret-key" not in text and str(identity.id) not in text


async def test_limit_one_processes_single_item(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    seen: list[int] = []

    async def count(event: object) -> None:
        seen.append(1)

    hub_under_test.dispatcher.subscribe("counter", count)
    for index in range(3):
        await space.identities.create(
            IdentityCreate(service="fake_tg", external_ref=f"+7000000111{index}"), actor="test"
        )

    # бюджет прохода — одна единица работы, и он делится между потребителями (`D-327`)
    first = await hub_under_test.workers.dispatch.run_once(limit=1)
    assert first.processed == 1 and first.truncated

    for _ in range(20):
        if (await hub_under_test.workers.dispatch.run_once(limit=100)).processed == 0:
            break
    assert len(seen) == 3  # за несколько проходов подписчик получил все события


async def test_two_workers_do_not_process_the_same_work(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    handled: list[object] = []

    async def remember(event: Any) -> None:
        handled.append(event.id)

    hub_under_test.dispatcher.subscribe("once", remember)
    for index in range(4):
        await space.identities.create(
            IdentityCreate(service="fake_tg", external_ref=f"+7000000222{index}"), actor="test"
        )

    runs = await asyncio.gather(
        hub_under_test.workers.dispatch.run_once(limit=10),
        hub_under_test.workers.dispatch.run_once(limit=10),
    )
    assert sum(run.processed for run in runs) >= 4
    assert len(handled) == len(set(handled))  # каждое событие обработано один раз


async def test_hub_start_creates_no_background_tasks(hub_under_test: IdentityHub) -> None:
    before = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    await hub_under_test.start()
    await hub_under_test.workers.maintenance.run_once(limit=1)
    after = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    assert after == before, [task.get_name() for task in after - before]


async def test_dispatcher_loop_runs_over_run_once_and_shuts_down(hub_under_test: IdentityHub) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(hub_under_test.dispatcher.run(stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert task.done() and not task.cancelled()
