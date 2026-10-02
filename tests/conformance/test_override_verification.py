"""F3.9: override согласует производные поля, срок фактора проверяется сразу (R1-08, R2-F10, R2-F13)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

from identity_hub import (
    IdentityHub,
    OperationRule,
    Override,
    SpaceHandle,
    ValidationFailed,
    VerificationMode,
    VerificationSpec,
)
from identity_hub.clock import FakeClock
from identity_hub.errors import AttemptsExhausted, LeaseUnavailable, OperationNotAllowed, TransitionNotAllowed

pytestmark = pytest.mark.conformance

VALID_FOR = timedelta(seconds=0.4)


async def _service(hub: IdentityHub, **spec: Any) -> None:
    options = {"mode": VerificationMode.MANUAL, **spec}
    hub.register_service(
        replace(
            FAKE_TG,
            name="kyc_tg",
            verifications=(VerificationSpec("kyc", **options),),
            operations={"work": OperationRule(verified=frozenset({"kyc"}))},
        )
    )
    await hub.start()


async def _account(space: SpaceHandle, ref: str = "+1") -> UUID:
    reg = await register(
        space, service="kyc_tg", external_ref=ref, lifecycle="ready", session={"auth_key": "k"}
    )
    return reg.identity_id


async def _pass_time(hub: IdentityHub) -> None:
    if isinstance(hub.clock, FakeClock):
        hub.clock.advance(timedelta(days=3))
    else:
        await asyncio.sleep(0.7)


# ---------------------------------------------------------------- R2-F10, R1-08


async def test_override_of_verification_grants_admission(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """S6: override фактора в `verified` обновляет `verified_factors` — операция допускается."""
    await _service(hub_under_test)
    identity_id = await _account(space)

    await space.identities.override(
        identity_id, Override("verification", "kyc", "verified", reason="проверили вручную", actor="op")
    )

    assert (await space.verifications.status(identity_id, "kyc")).state == "verified"
    assert set((await space.identities.get(identity_id)).verified_factors) == {"kyc"}
    lease = await space.pools.define("p", service="kyc_tg").acquire(for_operation="work")
    async with lease.operation("work"):
        pass
    await lease.release()

    await space.identities.override(
        identity_id, Override("verification", "kyc", "revoked", reason="отозвали", actor="op")
    )
    assert set((await space.identities.get(identity_id)).verified_factors) == set()


async def test_override_validates_region_key(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _service(hub_under_test)
    identity_id = await _account(space)

    for bad in (
        Override("standing", "typo", "banned", reason="r", actor="op"),
        Override("availability", "x", "paused", reason="r", actor="op"),
        Override("lifecycle", "x", "retired", reason="r", actor="op"),
        Override("verification", "nonexistent_factor", "verified", reason="r", actor="op"),
        Override("verification", None, "verified", reason="r", actor="op"),
    ):
        with pytest.raises(ValidationFailed):
            await space.identities.override(identity_id, bad)
    assert (await space.identities.get(identity_id)).standing == "good"


async def test_override_key_defaults_per_region(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """R1-08: `auth` без ключа — слот `default`; денормализованное поле обновляется."""
    await _service(hub_under_test)
    identity_id = await _account(space)

    await space.identities.override(
        identity_id, Override("auth", None, "session_invalid", reason="r", actor="op")
    )
    assert str((await space.identities.get(identity_id)).auth) == "session_invalid"
    await space.identities.override(identity_id, Override("auth", "", "no_session", reason="r", actor="op"))
    assert str((await space.identities.get(identity_id)).auth) == "no_session"

    await space.identities.override(identity_id, Override("standing", None, "banned", reason="r", actor="op"))
    assert (await space.identities.get(identity_id)).standing == "banned"
    await space.identities.restore_standing(identity_id, actor="op", reason="ошибка")
    assert (await space.identities.get(identity_id)).standing == "good"


# ---------------------------------------------------------------- R2-F13: срок действия


async def test_expired_factor_blocks_lease_and_operation_before_maintenance(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """Просроченный фактор не даёт допуска сразу, не дожидаясь прогона обслуживания."""
    await _service(hub_under_test, valid_for=VALID_FOR)
    identity_id = await _account(space)
    pool = space.pools.define("p", service="kyc_tg")
    await space.identities.override(
        identity_id, Override("verification", "kyc", "verified", reason="r", actor="op", until=None)
    )
    lease = await pool.acquire(for_operation="probe")
    await lease.verification("kyc").reconcile("verified")  # срок действия ставит сервис: valid_for
    await lease.release()
    held = await pool.acquire(for_operation="work")  # пока срок не вышел — допуск есть
    await held.release()

    await _pass_time(hub_under_test)

    probe = await pool.acquire(for_operation="probe")  # аренда без требования фактора
    with pytest.raises(OperationNotAllowed) as exc:
        await probe.operation("work").__aenter__()
    assert [blocker.code for blocker in exc.value.blockers] == ["verification"]
    await probe.release()
    with pytest.raises(LeaseUnavailable):
        await pool.acquire(for_operation="work")
    assert set((await space.identities.get(identity_id)).verified_factors) == {
        "kyc"
    }  # обслуживание ещё не шло


# ---------------------------------------------------------------- R2-F13: approve, reset_attempts


async def test_manual_approve_requires_under_review(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """В режиме MANUAL `approve` из `pending` минует `submit` — запрещено."""
    await _service(hub_under_test)
    identity_id = await _account(space)
    pool = space.pools.define("p", service="kyc_tg")
    lease = await pool.acquire(for_operation="probe")
    kyc = lease.verification("kyc")
    await kyc.start()

    with pytest.raises(TransitionNotAllowed):
        await space.verifications.review(
            identity_id, "kyc", "approve", actor="op", reason="выглядит нормально"
        )
    with pytest.raises(TransitionNotAllowed):
        await kyc.approve()

    await kyc.submit()
    await lease.release()
    status = await space.verifications.review(identity_id, "kyc", "approve", actor="op", reason="проверено")
    assert (status.state, status.attempts) == ("verified", 1)


async def test_reset_attempts_gives_a_way_out_after_exhaustion(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    await _service(hub_under_test, max_attempts=1)
    identity_id = await _account(space)
    pool = space.pools.define("p", service="kyc_tg")
    lease = await pool.acquire(for_operation="probe")
    kyc = lease.verification("kyc")
    await kyc.start()
    await kyc.submit()
    await lease.release()
    await space.verifications.review(
        identity_id, "kyc", "reject", actor="op", reason="нечитаемо", code="blurry"
    )
    lease = await pool.acquire(for_operation="probe")
    with pytest.raises(AttemptsExhausted):
        await lease.verification("kyc").start()

    status = await space.verifications.review(
        identity_id, "kyc", "reset_attempts", actor="op", reason="разрешаем ещё раз"
    )

    assert (status.state, status.attempts) == ("rejected", 0)
    assert (await lease.verification("kyc").start()).state == "pending"
    await lease.release()


# ---------------------------------------------------------------- R2-F13: одна ложная проба


async def test_single_missing_probe_does_not_revoke_verified(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """Снятие `verified` по пробе требует подтверждения вторым наблюдением подряд."""
    await _service(hub_under_test)
    identity_id = await _account(space)
    await space.identities.override(
        identity_id, Override("verification", "kyc", "verified", reason="r", actor="op")
    )
    lease = await space.pools.define("p", service="kyc_tg").acquire(for_operation="probe")
    kyc = lease.verification("kyc")

    assert (await kyc.reconcile("missing")).state == "verified"  # первая проба — ждём подтверждения
    assert set((await space.identities.get(identity_id)).verified_factors) == {"kyc"}
    assert (await kyc.reconcile("verified")).state == "verified"  # платформа снова видит фактор: сброс
    assert (await kyc.reconcile("missing")).state == "verified"
    assert (await kyc.reconcile("missing")).state == "revoked"  # два наблюдения подряд
    assert set((await space.identities.get(identity_id)).verified_factors) == set()
    await lease.release()
