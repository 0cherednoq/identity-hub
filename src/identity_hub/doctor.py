"""Проверка и исправление денормализации, ротация KEK (ТЗ G-6.2, S-3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from identity_hub.errors import ValidationFailed
from identity_hub.service import Service
from identity_hub.store.base import DoctorIssue

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

__all__ = ["Doctor", "DoctorIssue", "RenormalizeReport", "RotationReport"]


@dataclass(frozen=True, slots=True)
class RenormalizeReport:
    """Итог `Doctor.renormalize`: сколько обновлено, коллизии и ref, которые нормализатор отверг.

    Коллизии и отказы перечислены только id: значения ref (телефоны, email) в отчёт не попадают.
    """

    updated: int
    collisions: list[list[UUID]]
    invalid: list[UUID]

    @property
    def complete(self) -> bool:
        return not self.collisions and not self.invalid


@dataclass(frozen=True, slots=True)
class RotationReport:
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class Doctor:
    def __init__(self, hub: IdentityHub) -> None:
        self.hub = hub

    async def check(self, *, fix: bool = False) -> list[DoctorIssue]:
        return await self.hub.store.doctor(fix=fix)

    async def rotate_key(self, *, limit: int = 500) -> RotationReport:
        return RotationReport(await self.hub.store.rotate_key(self.hub.envelope, limit=limit))

    async def renormalize(self, service: Service | str, *, batch_size: int = 1000) -> RenormalizeReport:
        """Пересчитать `normalized_ref` identity сервиса после смены `normalize_ref_version` (D-149).

        Пачка с коллизией не применяется целиком: разрешите дубли (слияние или удаление
        identity) и запустите снова. Идемпотентно: уже нормализованные строки не трогает.
        """
        svc = self.hub.service(service)
        version = svc.normalize_ref_version
        updated = 0
        collisions: list[list[UUID]] = []
        invalid: list[UUID] = []
        blocked: set[UUID] = set()
        while True:
            stale = [
                row
                for row in await self.hub.store.stale_normalizations(
                    svc.name, version, batch_size + len(blocked)
                )
                if row[0] not in blocked
            ][:batch_size]
            if not stale:
                break
            updates: list[tuple[UUID, str]] = []
            for identity_id, external_ref in stale:
                try:
                    updates.append((identity_id, svc.normalize_external_ref(external_ref)))
                except ValidationFailed:
                    invalid.append(identity_id)
                    blocked.add(identity_id)
            found = await self.hub.store.apply_normalizations(svc.name, version, updates)
            if found:
                collisions.extend(found)
                blocked.update(identity_id for identity_id, _ref in stale)
                continue
            updated += len(updates)
        return RenormalizeReport(updated, collisions, invalid)
