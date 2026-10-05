"""N1.1 (задача 6): назначение аренды — `purpose` и `ref` в аренде, истории, событиях и сводке."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityCreate, IdentityHub, LeaseMode, SpaceHandle, ValidationFailed
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]

SINCE = datetime(2000, 1, 1, tzinfo=UTC)


async def _ready(space: SpaceHandle, count: int = 1, service: str = "fake_tg") -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service=service, external_ref=f"+{n}", lifecycle="ready", session={"auth_key": "k"}
            )
            for n in range(count)
        ],
        actor="test",
    )


async def _history(space: SpaceHandle, lease_id: Any) -> Any:
    async with space._tx(own=True) as tx:
        rows = await tx.lease_read_rows(SINCE)
    (row,) = [row for row in rows if row.lease_id == lease_id]
    return row


async def _event(space: SpaceHandle, lease_id: Any, event_type: str) -> Any:
    page = await space.admin.event_page(filters=EventFilters(lease_id=lease_id, include_journal=True))
    (item,) = [item for item in page.items if item.type == event_type]
    return item


async def test_purpose_and_ref_reach_lease_history_and_events(space: SpaceHandle) -> None:
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w")

    lease = await pool.acquire(purpose="download", ref="job:42")

    assert (lease.purpose, lease.ref) == ("download", "job:42")
    async with space._tx(own=True) as tx:
        live = await tx.current_lease(lease.identity_id)
    assert live is not None and (live.purpose, live.ref) == ("download", "job:42")
    history = await _history(space, lease.lease_id)
    assert (history.purpose, history.ref) == ("download", "job:42")
    acquired = await _event(space, lease.lease_id, "lease.acquired")
    assert acquired.data["purpose"] == "download" and acquired.data["ref"] == "job:42"

    await lease.release()
    released = await _event(space, lease.lease_id, "lease.released")
    assert released.data["purpose"] == "download" and released.data["ref"] == "job:42"


async def test_fields_are_empty_by_default(space: SpaceHandle) -> None:
    await _ready(space)
    async with space.pools.define("work", service="fake_tg", holder="w").lease() as lease:
        assert lease.purpose is None and lease.ref is None
        history = await _history(space, lease.lease_id)
        assert history.purpose is None and history.ref is None


async def test_pool_default_purpose_and_call_override(space: SpaceHandle) -> None:
    await _ready(space, 2)
    pool = space.pools.define("work", service="fake_tg", holder="w", purpose="warmup")

    async with pool.lease() as default, pool.lease(purpose="collect", ref="task/7") as custom:
        assert default.purpose == "warmup" and default.ref is None
        assert (custom.purpose, custom.ref) == ("collect", "task/7")


@pytest.mark.parametrize(
    "options",
    [
        {"purpose": ""},
        {"purpose": "Download"},
        {"purpose": "with space"},
        {"purpose": "x" * 65},
        {"ref": ""},
        {"ref": "x" * 257},
    ],
)
async def test_invalid_values_are_rejected_before_acquire(
    space: SpaceHandle, options: dict[str, str]
) -> None:
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w")

    with pytest.raises(ValidationFailed):
        await pool.acquire(**options)

    lease = await pool.acquire()  # аккаунт не занят неудачной попыткой
    await lease.release()


async def test_invalid_pool_purpose_is_rejected_at_define(space: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed):
        space.pools.define("work", service="fake_tg", purpose="Не метка")


async def test_ref_is_cleaned_like_event_text(space: SpaceHandle) -> None:
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w")

    lease = await pool.acquire(ref="https://user:hunter2@example.com/job/1")

    assert lease.ref is not None and "hunter2" not in lease.ref
    assert "hunter2" not in (await _history(space, lease.lease_id)).ref
    await lease.release()


async def test_idempotent_replay_keeps_original_values(space: SpaceHandle) -> None:
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w")

    first = await pool.acquire(idempotency_key="k-1", purpose="download", ref="job:1")
    again = await pool.acquire(idempotency_key="k-1", purpose="collect", ref="job:2")

    assert again.lease_id == first.lease_id
    assert (again.purpose, again.ref) == ("download", "job:1")
    await first.release()


async def test_shared_lease_keeps_fields_in_history(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    hub_under_test.register_service(replace(FAKE_TG, name="fake_shared", lease_mode=LeaseMode.SHARED))
    await hub_under_test.start()  # строка сервиса в БД
    await _ready(space, service="fake_shared")

    lease = await space.pools.define("read", service="fake_shared", holder="r").acquire(
        purpose="read", ref="box:1"
    )

    assert (lease.purpose, lease.ref) == ("read", "box:1")
    history = await _history(space, lease.lease_id)
    assert (history.purpose, history.ref) == ("read", "box:1")
    await lease.release()


async def test_dashboard_groups_by_purpose(space: SpaceHandle) -> None:
    await _ready(space, 3)
    pool = space.pools.define("work", service="fake_tg", holder="w")
    leases = [
        await pool.acquire(purpose="download"),
        await pool.acquire(purpose="download"),
        await pool.acquire(),
    ]

    dashboard = await space.admin.lease_dashboard(group_by="purpose")

    assert {item.value: item.metrics.acquired for item in dashboard.slices} == {"download": 2, "": 1}
    for lease in leases:
        await lease.release()
