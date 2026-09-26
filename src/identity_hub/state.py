"""Регионы состояния в хранилище: команды, наблюдения, ручные правки (ТЗ FR-9, G-4.1, G-6.1).

Изменение региона, денормализованные колонки ih_identities и события пишутся одной транзакцией.
Синхронизируемые регионы — строки ih_region_states с оптимистической блокировкой по `version`;
lifecycle и availability — только колонки ih_identities с compare-and-set (D-107).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from identity_hub._space import SpaceBase, logger
from identity_hub.domain import regions as machine
from identity_hub.domain.regions import Change, Command, Observation, RegionDecision, RegionState, Timer
from identity_hub.enums import Lifecycle
from identity_hub.errors import ConcurrentModification, ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.store.base import IdentityRow, StoreTx

__all__ = ["COLUMN_REGIONS", "DEFAULT_SLOT", "Override", "StateMixin", "apply_region_change"]

DEFAULT_SLOT = "default"
MAX_RETRIES = 5

# регионы без строки ih_region_states: колонка состояния и колонка срока
COLUMN_REGIONS: dict[str, tuple[str, str | None]] = {
    "lifecycle": ("lifecycle", None),
    "availability": ("availability", "availability_until"),
}


@dataclass(frozen=True, slots=True)
class Override:
    """Ручная правка региона из админки (ТЗ §11): причина и автор обязательны, `pin_for` — закрепление."""

    region: str
    key: str
    state: str
    reason: str
    actor: str
    pin_for: timedelta | None = None
    code: str | None = None
    until: datetime | None = None


def _denormalized(region: str, key: str, state: RegionState) -> dict[str, Any]:
    """Колонки ih_identities, производные от синхронизируемого региона (G-6.1)."""
    if region == "standing":
        return {"standing": state.state, "standing_code": state.code, "standing_until": state.until}
    if region == "auth" and key == DEFAULT_SLOT:
        return {"auth_state": state.state}
    return {}


def _initial(region: str, key: str, identity: IdentityRow) -> RegionState:
    """Состояние синхронизируемого региона, у которого ещё нет строки (из колонок аккаунта)."""
    if region == "standing":
        return RegionState(
            state=identity.standing,
            code=identity.standing_code,
            until=identity.standing_until,
            changed_at=identity.created_at,
        )
    if region == "auth":
        state = identity.auth_state if key == DEFAULT_SLOT else "no_session"
        return RegionState(state=state, changed_at=identity.created_at)
    if region == "verification":
        return RegionState(state="missing", changed_at=identity.created_at)
    raise ValidationFailed(f"регион {region!r} не хранится в ih_region_states")


async def apply_region_change(
    space: SpaceBase,
    tx: StoreTx,
    identity_id: UUID,
    region: str,
    key: str,
    change: Change,
    *,
    lease_id: UUID | None = None,
    extra_columns: dict[str, Any] | None = None,
) -> RegionDecision:
    """Применить изменение к региону в транзакции `tx`: машина → запись → денормализация → события.

    Гонка с параллельной записью — повтор с перечитыванием (D-108).
    """
    now = await tx.now()
    for _ in range(MAX_RETRIES):
        identity = await space._identity(tx, identity_id)
        service = space.hub.service(identity.service)
        spec = service.region_spec(region)
        if region in COLUMN_REGIONS:
            column, until_column = COLUMN_REGIONS[region]
            current = RegionState(
                state=getattr(identity, column),
                until=getattr(identity, until_column) if until_column else None,
                changed_at=identity.updated_at,
            )
            decision = machine.apply(spec, current, change, now, key)
            if not decision.changed:
                return decision
            values: dict[str, Any] = {column: decision.state.state, **(extra_columns or {})}
            expected: dict[str, Any] = {column: current.state}
            if until_column:
                values[until_column] = decision.state.until
                expected[until_column] = current.until
            saved = await tx.update_identity(identity_id, values, expected)
        else:
            previous = await tx.load_region(identity_id, region, key)
            current = previous or _initial(region, key, identity)
            decision = machine.apply(spec, current, change, now, key)
            if not decision.changed:
                return decision
            saved = await tx.save_region(identity_id, region, key, previous, decision.state)
            columns = {**_denormalized(region, key, decision.state), **(extra_columns or {})}
            if saved and columns:
                await tx.update_identity(identity_id, columns)
        if not saved:
            continue
        if decision.warning:
            logger.warning(
                "%s",
                decision.warning,
                extra={"identity_id": str(identity_id), "raw_status": decision.state.raw_status},
            )
        if decision.events:
            await space._emit(
                tx,
                *(
                    EventDraft(
                        type=e.type,
                        actor=decision.state.changed_by or _actor(change),
                        data=e.data,
                        severity=e.severity,
                        service=identity.service,
                        identity_id=identity_id,
                        lease_id=lease_id,
                    )
                    for e in decision.events
                ),
            )
        return decision
    raise ConcurrentModification("region", f"{identity_id}/{region}/{key}")


def _actor(change: Change) -> str:
    return change.actor if not isinstance(change, Timer) else "timer"


class StateMixin(SpaceBase):
    """Регионы состояния аккаунта (ТЗ §3, FR-9)."""

    async def set_lifecycle(
        self, identity_id: UUID, state: Lifecycle | str, *, actor: str, reason: str | None = None
    ) -> RegionDecision:
        """Решение владельца (команда lifecycle). Выполняется в транзакции пользователя, если она передана."""
        async with self._tx() as tx:
            return await apply_region_change(
                self, tx, identity_id, "lifecycle", "", Command(str(state), actor=actor, reason=reason)
            )

    async def retire(self, identity_id: UUID, *, actor: str, reason: str | None = None) -> RegionDecision:
        return await self.set_lifecycle(identity_id, Lifecycle.RETIRED, actor=actor, reason=reason)

    async def override(self, identity_id: UUID, override: Override) -> RegionDecision:
        """Ручная правка из админки (FR-9.1, 9.3). Для lifecycle/availability закрепления нет."""
        if override.pin_for is not None and override.region in COLUMN_REGIONS:
            raise ValidationFailed(f"регион {override.region} не синхронизируется: pin_for бессмыслен")
        change = machine.Override(
            to=override.state,
            reason=override.reason,
            actor=override.actor,
            pin_for=override.pin_for,
            code=override.code,
            until=override.until,
        )
        async with self._tx() as tx:
            return await apply_region_change(self, tx, identity_id, override.region, override.key, change)

    # --------------------------------------------------------- внутреннее: факты внешнего мира

    async def _observe(
        self,
        identity_id: UUID,
        region: str,
        observation: Observation,
        *,
        key: str = "",
        lease_id: UUID | None = None,
    ) -> RegionDecision:
        """Наблюдение (проба, ошибка, вебхук) — всегда своя транзакция (ТЗ §7.2)."""
        async with self._tx(own=True) as tx:
            return await apply_region_change(
                self, tx, identity_id, region, key, observation, lease_id=lease_id
            )

    async def _command(
        self,
        identity_id: UUID,
        region: str,
        command: Command,
        *,
        key: str = "",
        lease_id: UUID | None = None,
        own: bool = True,
    ) -> RegionDecision:
        async with self._tx(own=own) as tx:
            return await apply_region_change(self, tx, identity_id, region, key, command, lease_id=lease_id)

    async def _expire(self, identity_id: UUID, region: str, *, key: str = "") -> RegionDecision:
        async with self._tx(own=True) as tx:
            return await apply_region_change(self, tx, identity_id, region, key, Timer())
