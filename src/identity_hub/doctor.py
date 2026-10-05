"""Проверка и исправление денормализации, ротация KEK (ТЗ G-6.2, S-3)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from identity_hub.crypto import DecryptionFailed, associated_data
from identity_hub.errors import ValidationFailed
from identity_hub.proxies import ProxySecret, fingerprint_value
from identity_hub.service import Service
from identity_hub.store.base import ROTATION_TABLES, DoctorIssue, UnreadableSecret

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

__all__ = [
    "Doctor",
    "DoctorIssue",
    "FingerprintRekeyReport",
    "RenormalizeReport",
    "RotationReport",
    "UnreadableSecret",
]

UNREADABLE_REPORT_LIMIT = 1000  # в отчёте перечисляются первые строки, счётчик — полный


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
class FingerprintRekeyReport:
    """Итог `Doctor.rekey_fingerprints`: пересчитано, занятые и нерасшифрованные прокси (только id).

    `conflicts` — прокси, чей новый отпечаток уже у другого прокси пространства: это дубли одного
    адреса и логина, их нужно удалить или объединить вручную. Пока список не пуст, прежний ключ
    отпечатков из конфигурации не убирают.
    """

    updated: int
    conflicts: list[UUID]
    unreadable: list[UUID]

    @property
    def complete(self) -> bool:
        return not self.conflicts and not self.unreadable


@dataclass(frozen=True, slots=True)
class RotationReport:
    """Итог ротации мастер-ключа.

    `counts` — переобёрнуто строк по наборам. `remaining` — набор → `key_id` → строки, оставшиеся
    под неактивным ключом: заблокированные другим процессом, нечитаемые или записанные воркером
    со старой конфигурацией. `unreadable` — строки, которые не удалось расшифровать (первые
    `UNREADABLE_REPORT_LIMIT`), `unreadable_total` — их полное число. Старый ключ можно удалять
    из конфигурации только при `complete`.
    """

    counts: dict[str, int]
    remaining: dict[str, dict[str, int]] = field(default_factory=dict)
    unreadable: tuple[UnreadableSecret, ...] = ()
    unreadable_total: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def complete(self) -> bool:
        return not self.remaining


class Doctor:
    def __init__(self, hub: IdentityHub) -> None:
        self.hub = hub

    async def check(self, *, fix: bool = False) -> list[DoctorIssue]:
        return await self.hub._store.doctor(fix=fix)

    async def inactive_keys(self) -> dict[str, dict[str, int]]:
        """Набор → `key_id` → число строк, зашифрованных не активным мастер-ключом.

        Пусто — ротация завершена, прежние ключи можно убирать из конфигурации.
        """
        return await self.hub._store.inactive_key_usage(self.hub._envelope.active_key_id)

    async def rotate_key(
        self, *, limit: int = 500, lock_retries: int = 5, retry_delay: float = 0.5
    ) -> RotationReport:
        """Переобернуть все DEK активным мастер-ключом пачками по `limit` строк.

        Проходит все наборы до конца. Если после прохода остались строки под прежним ключом
        (их держала другая транзакция либо их дописал воркер со старой конфигурацией), проход
        повторяется — до `lock_retries` раз с паузой `retry_delay`. Нечитаемая строка пачку не
        обрывает: она остаётся как есть и попадает в отчёт.
        """
        if limit < 1:
            raise ValidationFailed("размер пачки ротации должен быть положительным")
        store, envelope = self.hub._store, self.hub._envelope
        counts: Counter[str] = Counter()
        unreadable: dict[tuple[str, str], UnreadableSecret] = {}
        remaining: dict[str, dict[str, int]] = {}
        for attempt in range(max(0, lock_retries) + 1):
            progressed = False
            for table in ROTATION_TABLES:
                cursor: tuple[object, ...] | None = None
                while True:
                    batch = await store.rotate_key(envelope, table=table, limit=limit, after=cursor)
                    if batch.rotated:
                        counts[table] += batch.rotated
                        progressed = True
                    for row in batch.unreadable:
                        unreadable.setdefault((row.table, row.id), row)
                    cursor = batch.cursor
                    if cursor is None:
                        break
            remaining = await store.inactive_key_usage(envelope.active_key_id)
            pending = sum(sum(keys.values()) for keys in remaining.values())
            if pending <= len(unreadable) or attempt == lock_retries:
                break
            if not progressed and retry_delay > 0:
                await self.hub.clock.sleep(retry_delay)
        return RotationReport(
            counts=dict(counts),
            remaining=remaining,
            unreadable=tuple(unreadable.values())[:UNREADABLE_REPORT_LIMIT],
            unreadable_total=len(unreadable),
        )

    async def rekey_fingerprints(self, *, batch_size: int = 500) -> FingerprintRekeyReport:
        """Пересчитать отпечатки прокси активным ключом кольца отпечатков (D-207, R2-S16).

        Нужен после миграции 0018 (прежний SHA-256 без ключа) и после смены активного ключа
        отпечатков. Идемпотентно: уже пересчитанные строки не трогает.
        """
        if batch_size < 1:
            raise ValidationFailed("размер пачки должен быть положительным")
        hub = self.hub
        active = hub._fingerprints.active_key_id
        updated = 0
        conflicts: list[UUID] = []
        unreadable: list[UUID] = []
        after: UUID | None = None
        while batch := await hub._store.stale_proxy_fingerprints(active, batch_size, after):
            for slug, row in batch:
                after = row.id
                try:
                    secret = hub._envelope.open_model(
                        ProxySecret, row.secret, associated_data("ih_proxies", row.id)
                    )
                except DecryptionFailed:
                    unreadable.append(row.id)
                    continue
                value = fingerprint_value(row.scheme, row.host, row.port, secret.username)
                async with hub.space(slug)._tx(own=True) as tx:
                    if await tx.set_proxy_fingerprint(row.id, hub._fingerprints.tag(value), active):
                        updated += 1
                    else:
                        conflicts.append(row.id)
        return FingerprintRekeyReport(updated, conflicts, unreadable)

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
                for row in await self.hub._store.stale_normalizations(
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
            found = await self.hub._store.apply_normalizations(svc.name, version, updates)
            if found:
                collisions.extend(found)
                blocked.update(identity_id for identity_id, _ref in stale)
                continue
            updated += len(updates)
        return RenormalizeReport(updated, collisions, invalid)
