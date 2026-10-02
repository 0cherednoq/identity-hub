"""Синхронизация регионов с платформой через пробы сервиса (ТЗ FR-9.7, 9.8, 9.6).

`resync(id, regions)` — внеочередная проба под арендой; `reconcile(...)` — фоновая сверка давно
не сверявшихся аккаунтов с `wait=False` (занятые пропускаются). Пробы тратят квоту вида `probe`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel

from identity_hub._space import SpaceBase, logger
from identity_hub.api._base import legacy
from identity_hub.auth import ALL_LIFECYCLES
from identity_hub.domain.regions import Observation, Outcome
from identity_hub.enums import ChangeSource, Standing
from identity_hub.errors import LeaseUnavailable, ValidationFailed
from identity_hub.protocols import ProbeResult
from identity_hub.service import PROBE_OPERATION, Service
from identity_hub.state import DEFAULT_SLOT
from identity_hub.store.base import CandidateQuery

if TYPE_CHECKING:
    from identity_hub.lease import Lease
    from identity_hub.pool import Pool

__all__ = ["SYNC_POOL", "ReconcileReport", "SyncMixin"]

SYNC_POOL = "identity-hub:sync"


@dataclass
class ReconcileReport:
    probed: dict[UUID, dict[str, str]] = field(default_factory=dict)
    skipped_busy: list[UUID] = field(default_factory=list)
    failed: dict[UUID, str] = field(default_factory=dict)


class _ProbeContext:
    def __init__(self, lease: Lease) -> None:
        self._lease = lease

    @property
    def identity_id(self) -> UUID:
        return self._lease.identity_id

    @property
    def proxy_url(self) -> str | None:
        return self._lease._proxy_url()

    async def credentials(self) -> BaseModel | None:
        return await self._lease.credentials()

    async def session(self, slot: str = DEFAULT_SLOT) -> BaseModel | None:
        return await self._lease.session(slot)


def _parse_regions(service: Service, regions: Iterable[str]) -> list[tuple[str, str]]:
    """`auth`, `standing`, `verification:<factor>` или `verification` (все факторы сервиса)."""
    parsed: list[tuple[str, str]] = []
    for item in regions:
        name, _, key = item.partition(":")
        if name == "verification":
            factors = [key] if key else sorted(service.factor_names)
            for factor in factors:
                service.verification(factor)
                parsed.append(("verification", factor))
        elif name in ("auth", "standing") and not key:
            parsed.append((name, DEFAULT_SLOT if name == "auth" else ""))
        else:
            raise ValidationFailed(f"регион {item!r} не синхронизируется пробами")
    return parsed


class SyncMixin(SpaceBase):
    async def _resync(
        self, identity_id: UUID, regions: Iterable[str] = ("auth", "standing")
    ) -> dict[str, str]:
        """Внеочередная проба регионов аккаунта (FR-9.7). Возвращает исход по региону."""
        async with self._tx(own=True) as tx:
            identity = await self._identity(tx, identity_id)
        service = self.hub.service(identity.service)
        targets = _parse_regions(service, regions)
        lease = await self._probe_pool(service).acquire(
            query=self._probe_query(service, [identity_id]), wait=30
        )
        try:
            return await self._probe(lease, service, targets)
        finally:
            await lease.release()

    async def _reconcile(
        self,
        regions: Iterable[str] = ("auth", "standing"),
        *,
        service: Service | str | None = None,
        not_synced_for: timedelta = timedelta(hours=6),
        limit: int = 50,
    ) -> ReconcileReport:
        """Фоновая сверка: аккаунты, чей регион не сверялся дольше `not_synced_for` (FR-9.7)."""
        services = [self.hub.service(service)] if service is not None else list(self.hub.services)
        report = ReconcileReport()
        region_list = list(regions)
        for svc in services:
            if svc.probes is None:
                continue
            targets = _parse_regions(svc, region_list)
            region, key = targets[0]
            async with self._tx(own=True) as tx:
                candidates = await tx.stale_identities(
                    svc.name, region, key, not_synced_for.total_seconds(), limit - len(report.probed)
                )
            for identity_id in candidates:
                try:
                    lease = await self._probe_pool(svc).acquire(query=self._probe_query(svc, [identity_id]))
                except LeaseUnavailable:
                    report.skipped_busy.append(identity_id)  # wait=False: занят воркером
                    continue
                try:
                    report.probed[identity_id] = await self._probe(lease, svc, targets)
                except Exception as exc:
                    logger.warning("сверка аккаунта %s не удалась", identity_id, exc_info=True)
                    report.failed[identity_id] = f"{type(exc).__name__}: {exc}"[:500]
                finally:
                    await lease.release()
            if len(report.probed) >= limit:
                break
        return report

    # --------------------------------------------------------- внутреннее

    def _probe_pool(self, service: Service) -> Pool:
        pool: Pool = self._pool(SYNC_POOL, service=service)  # type: ignore[attr-defined]
        return pool

    @staticmethod
    def _probe_query(service: Service, among: list[UUID]) -> CandidateQuery:
        return CandidateQuery(
            service=service.name,
            standing=frozenset(Standing),
            needs_auth=False,
            among=among,
            lifecycles=ALL_LIFECYCLES,
        )

    async def _probe(self, lease: Lease, service: Service, targets: list[tuple[str, str]]) -> dict[str, str]:
        probes = service.probes
        if probes is None:
            raise ValidationFailed(f"у сервиса {service.name} нет Probes")
        ctx = _ProbeContext(lease)
        results: dict[str, str] = {}
        async with lease.operation(PROBE_OPERATION):
            for region, key in targets:
                label = f"{region}:{key}" if region == "verification" else region
                method: Any = getattr(probes, region, None)
                if method is None:
                    results[label] = "not_probed"
                    continue
                result: ProbeResult | None = await (
                    method(ctx, key) if region == "verification" else method(ctx)
                )
                if result is None:
                    results[label] = "not_probed"
                    continue
                results[label] = await self._apply_probe(lease, service, region, key, result)
        return results

    async def _apply_probe(
        self, lease: Lease, service: Service, region: str, key: str, result: ProbeResult
    ) -> str:
        if region == "verification":
            status = await lease.verification(key).reconcile(result)
            return status.state
        state = result.state
        if region == "standing" and state is None and result.code is not None:
            state = service.standing_for_code(result.code)  # коды платформы объявляет сервис (§3.2)
        async with self._tx(own=True) as tx:
            observed_at = result.observed_at or await tx.now()
        decision = await self._observe(  # type: ignore[attr-defined]
            lease.identity_id,
            region,
            Observation(
                state,
                observed_at=observed_at,
                source=ChangeSource.PROBE,
                raw_status=result.raw_status,
                code=result.code,
                until=result.until,
                actor=lease.holder,
            ),
            key=key,
            lease_id=lease.lease_id,
        )
        outcome: Outcome = decision.outcome
        return str(outcome)

    resync = legacy(_resync, "resync")
    reconcile = legacy(_reconcile, "reconcile")
