"""F3.8: захват и завершение элементов фонового импорта одинаковы на обоих хранилищах."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from identity_hub import ImportRecord, SpaceHandle
from identity_hub.store.base import ImportItemResult

pytestmark = pytest.mark.conformance

CLAIM = timedelta(minutes=5)


async def _stage(space: SpaceHandle, count: int) -> UUID:
    return await space.imports.stage(
        service="fake_tg",
        records=[ImportRecord(f"+{n}") for n in range(count)],
        source="queue.csv",
        actor="test",
    )


async def _claim(
    space: SpaceHandle, run_id: UUID, *, limit: int = 10, claim_for: timedelta = CLAIM
) -> list[Any]:
    async with space._tx(own=True) as tx:
        return await tx.claim_import_items(run_id, worker="w", limit=limit, claim_for=claim_for)


async def _finish(space: SpaceHandle, run_id: UUID, results: list[ImportItemResult]) -> Any:
    async with space._tx(own=True) as tx:
        return await tx.finish_import_items(run_id, results)


async def test_claim_takes_pending_items_in_order_once(space: SpaceHandle) -> None:
    run_id = await _stage(space, 5)

    first = await _claim(space, run_id, limit=2)
    second = await _claim(space, run_id, limit=10)
    third = await _claim(space, run_id)

    assert [(item.position, item.claim_token) for item in first] == [(1, 1), (2, 1)]
    assert [item.position for item in second] == [3, 4, 5]
    assert third == []  # всё захвачено, срок захвата не истёк
    assert (await space.imports.get(run_id)).state == "running"


async def test_expired_claim_is_taken_again_with_a_new_token(space: SpaceHandle) -> None:
    """Захват истёк (воркер упал): элемент выдаётся снова, а результат по старому токену отклоняется."""
    run_id = await _stage(space, 1)
    (stale,) = await _claim(space, run_id, claim_for=timedelta(0))

    (fresh,) = await _claim(space, run_id)

    assert (fresh.position, fresh.claim_token) == (1, stale.claim_token + 1)
    late = await _finish(space, run_id, [ImportItemResult(1, stale.claim_token, "created")])
    assert (late.processed, late.state) == (0, "running")  # опоздавший результат не засчитан
    done = await _finish(
        space, run_id, [ImportItemResult(1, fresh.claim_token, "skipped", error_code="already_exists")]
    )
    assert (done.processed, done.skipped, done.state) == (1, 1, "succeeded")


async def test_finish_counts_outcomes_and_closes_the_run(space: SpaceHandle) -> None:
    run_id = await _stage(space, 4)
    items = await _claim(space, run_id)
    outcomes = ["created", "updated", "skipped", "failed"]

    partial = await _finish(
        space,
        run_id,
        [
            ImportItemResult(item.position, item.claim_token, outcome)
            for item, outcome in zip(items[:2], outcomes[:2], strict=True)
        ],
    )
    assert (partial.state, partial.processed) == ("running", 2)

    final = await _finish(
        space,
        run_id,
        [
            ImportItemResult(item.position, item.claim_token, outcome, error_code="x", error="reason")
            for item, outcome in zip(items[2:], outcomes[2:], strict=True)
        ],
    )

    assert (final.state, final.processed) == ("succeeded", 4)
    assert (final.created, final.updated, final.skipped, final.failed) == (1, 1, 1, 1)
    assert [entry["position"] for entry in final.report["failed"]] == [4]
    assert final.finished_at is not None
    assert await _claim(space, run_id) == []  # завершённый прогон не выдаёт элементы


async def test_cancel_stops_new_claims_and_waits_for_the_claimed_batch(space: SpaceHandle) -> None:
    run_id = await _stage(space, 3)
    (claimed,) = await _claim(space, run_id, limit=1)

    assert await space.imports.cancel(run_id) is True
    assert (await space.imports.get(run_id)).state == "cancelling"
    assert await _claim(space, run_id) == []

    finished = await _finish(
        space, run_id, [ImportItemResult(claimed.position, claimed.claim_token, "created")]
    )
    assert (finished.state, finished.created) == ("cancelled", 1)
