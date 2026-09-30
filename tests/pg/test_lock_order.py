"""IMP-14 (I0.2b): единый порядок блокировок — пересекающиеся транзакции без deadlock."""

from __future__ import annotations

import asyncio

import pytest

from identity_hub import IdentityCreate, ImportRecord, SpaceHandle
from identity_hub.importing import ImportReport

pytestmark = [pytest.mark.anyio, pytest.mark.pg]

REFS = [f"+5{n:03d}" for n in range(40)]


def _sessions(refs: list[str], key: str) -> list[ImportRecord]:
    return [ImportRecord(external_ref=ref, sessions={"default": {"auth_key": key}}) for ref in refs]


async def test_reverse_order_no_deadlock(space: SpaceHandle) -> None:
    """Два импорта обновляют сессии одних и тех же identity в противоположном порядке."""
    await space.identities.create_many([IdentityCreate(service="fake_tg", external_ref=ref) for ref in REFS])

    reports: list[ImportReport] = await asyncio.wait_for(
        asyncio.gather(
            space.imports.run(
                service="fake_tg", records=_sessions(REFS, "a"), on_existing="update_session", batch_size=100
            ),
            space.imports.run(
                service="fake_tg",
                records=_sessions(list(reversed(REFS)), "b"),
                on_existing="update_session",
                batch_size=100,
            ),
        ),
        timeout=60,
    )

    for report in reports:
        assert report.failed == [], report.failed[:1]
        assert len(report.updated) == len(REFS)


async def test_lock_identities_orders_by_uuid(space: SpaceHandle) -> None:
    """Явная блокировка набора identity в двух транзакциях с разным входным порядком."""
    created = await space.identities.create_many(
        [IdentityCreate(service="fake_tg", external_ref=f"+6{n:02d}") for n in range(20)]
    )
    ids = [identity.id for identity in created.created]

    async def hold(order: list) -> None:  # type: ignore[type-arg]
        async with space._tx(own=True) as tx:
            await tx.lock_identities(order)
            for identity_id in order:
                await tx.update_identity(identity_id, {"note": "x"})
            await asyncio.sleep(0)

    await asyncio.wait_for(asyncio.gather(hold(ids), hold(list(reversed(ids)))), timeout=30)
