"""Проверка и исправление денормализации, ротация KEK (ТЗ G-6.2, S-3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from identity_hub.store.base import DoctorIssue

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

__all__ = ["Doctor", "DoctorIssue", "RotationReport"]


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
