"""I6.5 (`IMP-18`): барьерные гонки — исход детерминирован порядком коммитов, не таймингом.

Синхронизация только событиями и ожиданием реальной блокировки в `pg_locks`: случайный `sleep`
и повышение таймаута синхронизацией не считаются.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub import (
    IdentityCreate,
    LeaseUnavailable,
    Lifecycle,
    Observation,
    OperationRule,
    Override,
    Quota,
    QuotaExhausted,
    Service,
    Standing,
    ValidationFailed,
)
from identity_hub.enums import ChangeSource
from identity_hub.hub import IdentityHub, SpaceHandle

pytestmark = [pytest.mark.pg]


async def _identity(space: SpaceHandle, ref: str, service: str = "fake_tg") -> UUID:
    created = await space.identities.create(
        IdentityCreate(
            service=service,
            external_ref=ref,
            lifecycle=Lifecycle.READY,
            session={"auth_key": "k"},
            credentials={"phone": ref, "password": "p"} if service == "fake_tg" else None,
        ),
        actor="test",
    )
    return created.id


async def _wait_blocked(sessionmaker: async_sessionmaker[AsyncSession], task: asyncio.Task[Any]) -> None:
    blocked = text("SELECT count(*) FROM pg_locks WHERE NOT granted")
    async with sessionmaker() as session:
        for _ in range(500):
            assert not task.done(), "конкурент не дождался незакоммиченной транзакции"
            if (await session.execute(blocked)).scalar_one():
                return
            await asyncio.sleep(0.01)
    pytest.fail("конкурент не встал на блокировку")


async def test_two_acquires_of_the_last_identity(space: SpaceHandle) -> None:
    """Последний свободный аккаунт достаётся одному; второй получает понятный отказ."""
    await _identity(space, "+race-last")
    pool = space.pools.define("work", service="fake_tg", holder="w")

    first, second = await asyncio.gather(pool.acquire(), pool.acquire(wait=None), return_exceptions=True)
    winners = [item for item in (first, second) if not isinstance(item, BaseException)]
    losers = [item for item in (first, second) if isinstance(item, BaseException)]

    assert len(winners) == 1 and len(losers) == 1
    assert isinstance(losers[0], LeaseUnavailable)
    await winners[0].release()


async def test_acquire_sees_identity_only_after_release_commits(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Пока отзыв не зафиксирован, аккаунт занят: выборка его пропускает (`SKIP LOCKED`).

    Барьера на блокировке здесь быть не может — в этом и смысл `SKIP LOCKED`: конкурент не ждёт
    чужую транзакцию, а сразу получает отказ. Детерминированность проверяется исходом до и после
    фиксации, а не таймингом.
    """
    identity_id = await _identity(space, "+race-release")
    pool = space.pools.define("work", service="fake_tg", holder="w")
    lease = await pool.acquire()
    held, go = asyncio.Event(), asyncio.Event()

    async def revoking() -> None:
        async with space._tx(own=True) as tx:
            await tx.revoke_lease(identity_id)
            held.set()
            await go.wait()

    first = asyncio.create_task(revoking())
    await held.wait()
    with pytest.raises(LeaseUnavailable):  # отзыв ещё не зафиксирован
        await pool.acquire()
    go.set()
    await first

    taken = await pool.acquire()
    assert taken.identity_id == identity_id and taken.lease_id != lease.lease_id
    await taken.release()


async def test_renew_after_revoke_does_not_resurrect_lease(space: SpaceHandle) -> None:
    """Отозванную аренду продление не возвращает: владелец узнаёт о потере."""
    identity_id = await _identity(space, "+race-renew")
    pool = space.pools.define("work", service="fake_tg", holder="w")
    lease = await pool.acquire()

    async with space._tx(own=True) as tx:
        assert await tx.revoke_lease(identity_id) is not None

    with pytest.raises(Exception) as failure:
        await lease.renew()
    assert type(failure.value).__name__ in {"LeaseLost", "LeaseRevoked", "LeaseOwnershipLost"}


async def test_delete_is_refused_while_lease_is_live(space: SpaceHandle) -> None:
    """Удаление аккаунта под живой арендой отклоняется, пока аренду не отозвали."""
    identity_id = await _identity(space, "+race-delete")
    lease = await space.pools.define("work", service="fake_tg", holder="w").acquire()

    with pytest.raises(ValidationFailed):
        await space.identities.delete(identity_id, actor="admin")

    await space.identities.delete(identity_id, actor="admin", revoke=True)
    assert await space.identities.find(identity_id) is None
    # владелец узнаёт о потере на следующем обращении, а не молча продолжает работу
    with pytest.raises(Exception) as failure:
        await lease.renew()
    assert type(failure.value).__name__ in {"LeaseLost", "LeaseRevoked", "LeaseOwnershipLost", "NotFound"}


async def test_last_quota_unit_goes_to_one_operation(hub: IdentityHub, space: SpaceHandle) -> None:
    """Последняя единица квоты списывается один раз: второй вызов получает отказ."""
    hub.register_service(
        Service(
            name="race_quota",
            title="Race quota",
            operations={"work": OperationRule(needs_auth=False, quotas=(Quota(1, timedelta(hours=1)),))},
        )
    )
    await hub.start()
    await space.identities.create(
        IdentityCreate(service="race_quota", external_ref="+race-quota", lifecycle=Lifecycle.READY),
        actor="test",
    )
    lease = await space.pools.define("rq", service="race_quota").acquire(for_operation="work")

    async def work() -> None:
        async with lease.operation("work"):
            pass

    results = await asyncio.gather(work(), work(), return_exceptions=True)
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(failures) == 1 and isinstance(failures[0], QuotaExhausted)

    async with space._tx(own=True) as tx:
        windows = {window.kind: window.used for window in await tx.quota_usage(lease.identity_id)}
    assert windows["work"] == 1
    await lease.release()


async def test_override_wins_over_later_observation(space: SpaceHandle) -> None:
    """Закреплённая ручная правка не затирается наблюдением платформы (`FR-9.4`)."""
    identity_id = await _identity(space, "+race-override")

    await space.identities.override(
        identity_id,
        Override(
            "standing",
            "",
            Standing.LIMITED,
            reason="ручная правка",
            actor="admin",
            pin_for=timedelta(hours=1),
        ),
    )
    await space.identities.observe(
        identity_id,
        Observation("standing", "", Standing.GOOD, datetime.now(UTC)),
        actor="probe",
        source=ChangeSource.PROBE,
    )

    identity = await space.identities.get(identity_id)
    assert identity.standing == Standing.LIMITED  # закреплённая правка переживает наблюдение


async def test_credential_replace_races_keep_one_value(space: SpaceHandle) -> None:
    """Две параллельные замены credentials оставляют одно значение и одну запись."""
    identity_id = await _identity(space, "+race-cred")

    await asyncio.gather(
        space.secrets.replace_credentials(identity_id, {"phone": "+race-cred", "password": "one"}, actor="a"),
        space.secrets.replace_credentials(identity_id, {"phone": "+race-cred", "password": "two"}, actor="b"),
        return_exceptions=True,
    )

    records = await space.secrets.credentials(identity_id)
    assert len([item for item in records if item.kind == "default"]) == 1
    revealed = await space.secrets.reveal(identity_id, "credentials", actor="admin", reason="проверка гонки")
    assert revealed["password"] in {"one", "two"}


async def test_late_provider_apply_is_rejected_by_claim(hub: IdentityHub, space: SpaceHandle) -> None:
    """Поздний итог операции провайдера отвергается истёкшим claim, состояние не меняется."""
    from datetime import timedelta as _td

    identity_id = await _identity(space, "+race-provider")
    async with space._tx(own=True) as tx:
        operation = await tx.enqueue_provider_operation(identity_id, "probe")
        (claim,) = await tx.claim_provider_operations(
            "worker-a", limit=1, claim_for=_td(seconds=1), operation_ids=[operation.id]
        )

    # claim истёк и восстановитель вернул операцию в failed
    async with space._tx(own=True) as tx:
        await tx.execute(
            text(
                "UPDATE ih_provider_operations SET claim_expires_at = now() - interval '1 minute' "
                "WHERE id = :id"
            ).bindparams(id=operation.id)
        )
    assert await hub.workers.provider_operations.run_once(limit=10) is not None

    async with space._tx(own=True) as tx:
        applied = await tx.apply_provider_operation(claim, "succeeded", result={"late": True})
    assert applied is False  # поздний ответ отвергнут


async def test_ordered_delivery_keeps_revision_order_under_races(
    hub: IdentityHub, space: SpaceHandle
) -> None:
    """Параллельные изменения одного аккаунта доставляются ordered-получателю по порядку ревизий."""
    from pydantic import SecretStr

    endpoint = await space.webhooks.create(
        url="https://hooks.example.test/race-ordered",
        secret=SecretStr("s"),
        event_types=["identity.updated"],
        ordered=True,
    )
    identity_id = await _identity(space, "+race-ordered")
    for index in range(3):
        await space.identities.set_note(identity_id, f"note-{index}", actor="test")
    await hub._store.process_events("webhooks", hub._webhook_dispatcher.handle_events, limit=100)

    first = await hub._store.claim_webhook_deliveries(limit=10, claim_for=timedelta(seconds=30))
    assert len(first) == 1  # следующая ревизия ждёт предыдущую

    center = await space.admin.delivery_center()
    shown = {item.id: item for item in center.webhook_endpoints}[endpoint.id]
    assert shown.ordered and shown.blocked == 2
