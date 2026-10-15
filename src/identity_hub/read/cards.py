"""Карточка аккаунта, сводка пула, история и статистика аренд (ТЗ FR-11.5–11.8).

Read-модели не содержат секретов и `token` аренды (FR-5.2, FR-6.4, S-8).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.domain.credentials import is_expired
from identity_hub.domain.data import build_search_text
from identity_hub.domain.regions import RegionState
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.links import Links
from identity_hub.store.base import IdentityRow, LeaseHistoryRow, QuotaWindow

__all__ = [
    "AccessCard",
    "CardsMixin",
    "CredentialInfo",
    "DataNamespaceInfo",
    "IdentityCard",
    "LeaseInfo",
    "LeaseStats",
    "ProxyInfo",
    "RegionInfo",
    "SessionInfo",
]


@dataclass(frozen=True, slots=True)
class RegionInfo:
    region: str
    key: str
    state: str
    code: str | None
    until: datetime | None
    changed_at: datetime
    source: str
    changed_by: str | None
    reason: str | None
    synced_at: datetime | None
    raw_status: str | None
    pinned_until: datetime | None
    pending_observation: str | None
    version: int


@dataclass(frozen=True, slots=True)
class LeaseInfo:
    lease_id: UUID
    pool: str
    holder: str
    fencing: int
    acquired_at: datetime
    expires_at: datetime
    heartbeat_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProxyInfo:
    id: UUID
    pool: str
    scheme: str
    host: str
    port: int
    status: str


@dataclass(frozen=True, slots=True)
class VerificationInfo:
    factor: str
    state: str
    code: str | None
    attempts: int
    started_at: datetime | None
    submitted_at: datetime | None
    verified_at: datetime | None
    expires_at: datetime | None
    via_identity_id: UUID | None
    details: dict[str, Any]
    has_evidence: bool


@dataclass(frozen=True, slots=True)
class CredentialInfo:
    present: bool
    version: int | None
    updated_at: datetime | None
    last_verified_at: datetime | None = None
    last_rejected_at: datetime | None = None
    reject_code: str | None = None


@dataclass(frozen=True, slots=True)
class SessionInfo:
    slot: str
    status: str
    expires_at: datetime | None
    version: int
    written_by_fencing: int
    updated_at: datetime | None
    last_verified_at: datetime | None = None
    last_rejected_at: datetime | None = None
    reject_code: str | None = None


@dataclass(frozen=True, slots=True)
class Credential:
    """Запись credentials без секрета (IMP-09): вид, имя, открытая часть, жизненный цикл, срок."""

    id: UUID
    kind: str
    name: str
    public_value: dict[str, Any] | None
    lifecycle: str
    expires_at: datetime | None
    expired: bool  # срок вышел — считается по времени, не переходом (D-215)
    revision: int
    version: int
    created_at: datetime
    updated_at: datetime
    last_verified_at: datetime | None
    last_rejected_at: datetime | None
    reject_code: str | None

    @classmethod
    def from_row(cls, row: Any, now: datetime) -> Credential:
        return cls(
            id=row.id,
            kind=row.kind,
            name=row.name,
            public_value=row.public_value,
            lifecycle=row.lifecycle,
            expires_at=row.expires_at,
            expired=is_expired(row.lifecycle, row.expires_at, now),
            revision=row.revision,
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_verified_at=row.last_verified_at,
            last_rejected_at=row.last_rejected_at,
            reject_code=row.reject_code,
        )


@dataclass(frozen=True, slots=True)
class AccessCard:
    credential: CredentialInfo
    sessions: list[SessionInfo]
    # все записи credentials без секретов: вид, имя, открытая часть, жизненный цикл (D-217)
    credential_records: list[Credential] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DataNamespaceInfo:
    namespace: str
    schema_version: int
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class IdentityCard:
    """Карточка для админки (FR-11.5)."""

    identity: IdentityRow
    regions: list[RegionInfo]
    verifications: list[VerificationInfo]
    links: Links
    lease: LeaseInfo | None
    proxy: ProxyInfo | None
    quotas: list[QuotaWindow]
    data_namespaces: list[DataNamespaceInfo]
    open_challenge: dict[str, Any] | None
    admin_url: str | None
    revision: int
    eligible: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LeaseStats:
    """Статистика аренд за период (FR-11.7)."""

    since: datetime
    count: int
    total: timedelta
    average: timedelta | None
    longest: timedelta | None
    utilization: float  # доля времени периода под арендой
    by_reason: dict[str, int] = field(default_factory=dict)
    operations: dict[str, int] = field(default_factory=dict)
    succeeded: int = 0
    failed: int = 0


def _region_info(region: str, key: str, state: RegionState) -> RegionInfo:
    return RegionInfo(
        region=region,
        key=key,
        state=state.state,
        code=state.code,
        until=state.until,
        changed_at=state.changed_at,
        source=str(state.change_source),
        changed_by=state.changed_by,
        reason=state.change_reason,
        synced_at=state.synced_at,
        raw_status=state.raw_status,
        pinned_until=state.pinned_until,
        pending_observation=state.pending_observation,
        version=state.version,
    )


class CardsMixin(SpaceBase):
    def _with_limits(self, service_name: str, windows: list[QuotaWindow]) -> list[QuotaWindow]:
        """Дописать окнам лимит из правила операции сервиса с тем же окном (оно у вида одно)."""
        try:
            operations = self.hub.service(service_name).operations
        except NotFound:
            return windows  # сервис больше не зарегистрирован: расход показываем без лимита
        result = []
        for window in windows:
            rule = operations.get(window.kind)
            limits = (
                [q.limit for q in rule.quotas if q.window_seconds == window.window_seconds] if rule else []
            )
            result.append(replace(window, limit=min(limits) if limits else None))
        return result

    async def _identity_card(self, identity_id: UUID) -> IdentityCard:
        async with self._tx(own=True) as tx:
            identity = await self._identity(tx, identity_id)
            regions = await tx.list_regions(identity_id)
            lease = await tx.current_lease(identity_id)
            proxy = await tx.get_binding(identity_id)
            quotas = self._with_limits(identity.service, await tx.quota_usage(identity_id))
            namespaces = await tx.data_namespaces(identity_id)
            challenge = await tx.open_challenge_of(identity_id)
            now = await tx.now()
            verifications = []
            for region, key, state in regions:
                if region == "verification":
                    row = await tx.load_verification(identity_id, key)
                    verifications.append(
                        VerificationInfo(
                            key,
                            state.state,
                            state.code,
                            row.attempts if row else 0,
                            row.started_at if row else None,
                            row.submitted_at if row else None,
                            row.verified_at if row else None,
                            row.expires_at if row else state.until,
                            row.via_identity_id if row else None,
                            dict(row.details) if row else {},
                            row is not None and row.evidence is not None,
                        )
                    )
        links = await self._links_of(identity_id)  # type: ignore[attr-defined]
        column_regions = [
            RegionInfo(
                "lifecycle",
                "",
                identity.lifecycle,
                None,
                None,
                identity.updated_at,
                "command",
                None,
                None,
                None,
                None,
                None,
                None,
                1,
            ),
            RegionInfo(
                "availability",
                "",
                identity.availability,
                None,
                identity.availability_until,
                identity.updated_at,
                "command",
                None,
                None,
                None,
                None,
                None,
                None,
                1,
            ),
        ]
        base = self.hub.admin_base_url
        blockers = _blockers(identity, now)
        return IdentityCard(
            identity=identity,
            regions=column_regions + [_region_info(r, k, s) for r, k, s in regions if r != "verification"],
            verifications=verifications,
            links=links,
            lease=LeaseInfo(
                lease.lease_id,
                lease.pool,
                lease.holder,
                lease.fencing,
                lease.acquired_at,
                lease.expires_at,
                lease.heartbeat_at,
            )
            if lease
            else None,
            proxy=ProxyInfo(proxy.id, proxy.pool, proxy.scheme, proxy.host, proxy.port, proxy.status)
            if proxy
            else None,
            quotas=quotas,
            data_namespaces=[DataNamespaceInfo(*namespace) for namespace in namespaces],
            open_challenge={
                "id": str(challenge.id),
                "kind": challenge.kind,
                "hint": challenge.hint,
                "expires_at": challenge.expires_at,
            }
            if challenge
            else None,
            admin_url=f"{base}/identities/{identity_id}" if base else None,
            revision=identity.revision,
            eligible=not blockers,
            blockers=blockers,
        )

    async def _access_card(self, identity_id: UUID) -> AccessCard:
        """Secret-free credential/session metadata for an account card."""
        async with self._tx(own=True) as tx:
            await self._identity(tx, identity_id)
            credential, sessions = await tx.access_metadata(identity_id)
            records = await tx.list_credentials(identity_id)
            now = await tx.now()
        return AccessCard(
            CredentialInfo(
                credential.present,
                credential.version,
                credential.updated_at,
                credential.last_verified_at,
                credential.last_rejected_at,
                credential.reject_code,
            ),
            [
                SessionInfo(
                    row.slot,
                    row.status,
                    row.expires_at,
                    row.version,
                    row.written_by_fencing,
                    row.updated_at,
                    row.last_verified_at,
                    row.last_rejected_at,
                    row.reject_code,
                )
                for row in sessions
            ],
            [Credential.from_row(row, now) for row in records],
        )

    async def _pool_summary(self, service: str | None = None) -> dict[str, dict[str, dict[str, int]]]:
        """Число аккаунтов по сервису и значениям регионов (FR-11.6): service → регион → значение → n."""
        async with self._tx(own=True) as tx:
            rows = await tx.region_counts(service)
        summary: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
        for svc, column, value, count in rows:
            summary[svc]["auth" if column == "auth_state" else column][value] = count
        return {svc: dict(regions) for svc, regions in summary.items()}

    async def _lease_history(
        self, identity_id: UUID, *, limit: int = 50, before: datetime | None = None
    ) -> list[LeaseHistoryRow]:
        """История аренд аккаунта, новые первыми (FR-11.7). Без token."""
        if not 1 <= limit <= 500:
            raise ValidationFailed("limit: от 1 до 500")
        async with self._tx(own=True) as tx:
            return await tx.lease_history(identity_id, limit=limit, before=before)

    async def _lease_stats(self, identity_id: UUID, since: datetime) -> LeaseStats:
        async with self._tx(own=True) as tx:
            row = await tx.lease_stats(identity_id, since)
            now = await tx.now()
        period = max((now - since).total_seconds(), 1e-9)
        return LeaseStats(
            since=since,
            count=row.count,
            total=row.total,
            average=row.average,
            longest=row.longest,
            utilization=min(1.0, row.total.total_seconds() / period),
            by_reason=row.by_reason,
            operations=row.operations,
            succeeded=row.succeeded,
            failed=row.failed,
        )

    async def _reindex_search(self, service: str, *, batch_size: int = 500) -> int:
        """Пересчитать search_text после изменения searchable-полей сервиса (FR-11.8)."""
        svc = self.hub.service(service)
        after: UUID | None = None
        changed = 0
        while True:
            async with self._tx(own=True) as tx:
                page = await tx.identities_page(svc.name, after, batch_size)
                for row in page:
                    text = build_search_text(svc, row.id, row.external_ref, row.profile, row.labels, row.note)
                    if text != row.search_text:
                        await tx.update_identity(row.id, {"search_text": text})
                        changed += 1
            if len(page) < batch_size:
                return changed
            after = page[-1].id


def _blockers(identity: IdentityRow, now: datetime) -> tuple[str, ...]:
    blockers: list[str] = []
    if identity.lifecycle != "ready":
        blockers.append(f"lifecycle:{identity.lifecycle}")
    if identity.standing not in ("good", "limited"):
        blockers.append(f"standing:{identity.standing}")
    if identity.auth_state != "authenticated":
        blockers.append(f"auth:{identity.auth_state}")
    available = identity.availability == "available" or (
        identity.availability in ("paused", "cooling")
        and identity.availability_until is not None
        and identity.availability_until <= now
    )
    if not available:
        blockers.append(f"availability:{identity.availability}")
    return tuple(blockers)
