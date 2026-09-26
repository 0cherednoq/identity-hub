"""Детерминированная in-memory реализация порта хранилища.

Она повторяет наблюдаемую семантику PostgreSQL-адаптера, но сериализует транзакции одним
``asyncio.Lock``. Перед транзакцией снимается глубокая копия состояния: исключение возвращает
снимок, поэтому тесты получают настоящую атомарность и rollback без SQL.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from identity_hub.clock import Clock, SystemClock
from identity_hub.crypto.envelope import EncryptedPayload, associated_data
from identity_hub.domain.regions import RegionState
from identity_hub.enums import Severity
from identity_hub.errors import LeaseUnavailable, SpaceMismatch
from identity_hub.events import Event, EventDraft
from identity_hub.store.base import (
    CandidateQuery,
    ChallengeRow,
    ChargeOutcome,
    DataRow,
    DoctorIssue,
    IdentityRow,
    LeaseCounters,
    LeaseHistoryRow,
    LeaseRow,
    LeaseStatsRow,
    LinkRow,
    MetricsSnapshot,
    NewIdentity,
    NewNotificationChannel,
    NewNotificationRule,
    NewProxy,
    NewWebhookEndpoint,
    NotificationAttempt,
    NotificationChannelRow,
    NotificationEnqueue,
    NotificationRuleRow,
    NotificationTargetRow,
    ProxyRow,
    QuotaCharge,
    QuotaWindow,
    SearchFilter,
    ServiceRow,
    SpaceRef,
    Store,
    StoreTx,
    VerificationRow,
    WebhookAttempt,
    WebhookDeliveryRow,
    WebhookEndpointRow,
)

__all__ = ["MemoryStore", "MemoryTx"]


@dataclass
class _MemoryState:
    spaces: dict[str, SpaceRef] = field(default_factory=dict)
    services: dict[str, ServiceRow] = field(default_factory=dict)
    identities: dict[UUID, IdentityRow] = field(default_factory=dict)
    regions: dict[tuple[UUID, str, str], RegionState] = field(default_factory=dict)
    credentials: dict[UUID, EncryptedPayload] = field(default_factory=dict)
    profile_secrets: dict[UUID, EncryptedPayload] = field(default_factory=dict)
    sessions: dict[tuple[UUID, str], tuple[EncryptedPayload, str, int, datetime | None]] = field(
        default_factory=dict
    )
    links: dict[tuple[UUID, str], UUID] = field(default_factory=dict)
    data: dict[tuple[UUID, str], DataRow] = field(default_factory=dict)
    leases: dict[UUID, LeaseRow] = field(default_factory=dict)
    history: dict[UUID, LeaseHistoryRow] = field(default_factory=dict)
    quotas: dict[tuple[UUID, str, int, datetime], int] = field(default_factory=dict)
    operation_keys: dict[str, tuple[UUID, str, str | None, datetime]] = field(default_factory=dict)
    proxies: dict[UUID, ProxyRow] = field(default_factory=dict)
    proxy_load: dict[UUID, int] = field(default_factory=dict)
    bindings: dict[UUID, UUID] = field(default_factory=dict)
    verifications: dict[tuple[UUID, str], VerificationRow] = field(default_factory=dict)
    challenges: dict[UUID, ChallengeRow] = field(default_factory=dict)
    lease_logs: list[dict[str, Any]] = field(default_factory=list)
    events: list[tuple[Event, UUID, bool]] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)
    webhook_endpoints: dict[UUID, WebhookEndpointRow] = field(default_factory=dict)
    webhook_deliveries: dict[int, WebhookDeliveryRow] = field(default_factory=dict)
    notification_channels: dict[UUID, tuple[NotificationChannelRow, EncryptedPayload, UUID]] = field(
        default_factory=dict
    )
    notification_rules: dict[UUID, NotificationRuleRow] = field(default_factory=dict)
    notification_rule_spaces: dict[UUID, UUID] = field(default_factory=dict)
    notifications: dict[int, dict[str, Any]] = field(default_factory=dict)
    next_event_id: int = 1
    next_webhook_delivery_id: int = 1
    next_notification_id: int = 1


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _window_start(now: datetime, charge: QuotaCharge) -> datetime:
    seconds = int((now - _utc(charge.origin)).total_seconds())
    return _utc(charge.origin) + timedelta(seconds=(seconds // charge.window_seconds) * charge.window_seconds)


def _merge_counters(row: LeaseHistoryRow, counters: LeaseCounters) -> dict[str, Any]:
    operations = dict(row.operations)
    for kind, count in counters.operations.items():
        operations[kind] = operations.get(kind, 0) + count
    return {
        "operations": operations,
        "succeeded": row.succeeded + counters.succeeded,
        "failed": row.failed + counters.failed,
        "last_error": counters.last_error if counters.last_error is not None else row.last_error,
    }


class MemoryTx(StoreTx):
    """Транзакция MemoryStore. Экземпляр живёт только внутри context manager."""

    def __init__(self, store: MemoryStore, space: SpaceRef, now: datetime) -> None:
        self.store = store
        self.space = space
        self._now = now
        self._notify = False

    @property
    def state(self) -> _MemoryState:
        return self.store._state

    async def now(self) -> datetime:
        return self._now

    def _own(self, identity_id: UUID) -> IdentityRow | None:
        row = self.state.identities.get(identity_id)
        return row if row is not None and row.space_id == self.space.id else None

    async def emit(self, drafts: Sequence[EventDraft]) -> list[UUID]:
        ids: list[UUID] = []
        for draft in drafts:
            sequence: int | None = None
            if draft.identity_id is not None:
                identity = self._own(draft.identity_id)
                if identity is not None:
                    sequence = identity.event_seq + 1
                    self.state.identities[identity.id] = replace(
                        identity, event_seq=sequence, updated_at=self._now
                    )
            event_id = uuid4()
            event = Event(
                id=self.state.next_event_id,
                event_id=event_id,
                type=draft.type,
                severity=draft.effective_severity,
                occurred_at=self._now,
                space=self.space.slug,
                service=draft.service,
                identity_id=draft.identity_id,
                proxy_id=draft.proxy_id,
                lease_id=draft.lease_id,
                sequence=sequence,
                actor=draft.actor,
                data=dict(draft.data),
            )
            self.state.next_event_id += 1
            self.state.events.append((event, self.space.id, draft.spec.external))
            self._notify |= draft.spec.external
            ids.append(event_id)
        return ids

    async def get_identity(self, identity_id: UUID) -> IdentityRow | None:
        return self._own(identity_id)

    async def update_identity(
        self, identity_id: UUID, values: dict[str, Any], expected: dict[str, Any] | None = None
    ) -> bool:
        row = self._own(identity_id)
        if row is None or any(getattr(row, key) != value for key, value in (expected or {}).items()):
            return False
        self.state.identities[identity_id] = replace(row, **values, updated_at=self._now)
        return True

    async def load_region(self, identity_id: UUID, region: str, key: str) -> RegionState | None:
        return self.state.regions.get((identity_id, region, key)) if self._own(identity_id) else None

    async def save_region(
        self, identity_id: UUID, region: str, key: str, previous: RegionState | None, new: RegionState
    ) -> bool:
        if self._own(identity_id) is None:
            return False
        compound = (identity_id, region, key)
        current = self.state.regions.get(compound)
        if previous is None:
            if current is not None:
                return False
        elif current is None or current.version != previous.version:
            return False
        self.state.regions[compound] = new
        return True

    async def find_identity(
        self, identity_id: UUID, service: str, external_ref: str
    ) -> tuple[UUID, UUID] | None:
        for row in self.state.identities.values():
            if row.id == identity_id or (row.service == service and row.external_ref == external_ref):
                return row.id, row.space_id
        return None

    async def insert_identity(self, row: NewIdentity) -> bool:
        if await self.find_identity(row.id, row.service, row.external_ref) is not None:
            return False
        identity = IdentityRow(
            id=row.id,
            space_id=self.space.id,
            service=row.service,
            external_ref=row.external_ref,
            lifecycle=row.lifecycle,
            standing=row.standing,
            standing_code=None,
            standing_until=None,
            auth_state=row.auth_state,
            availability="available",
            availability_until=None,
            requirements=[],
            tags=list(row.tags),
            attributes=deepcopy(row.attributes),
            profile=deepcopy(row.profile),
            labels=deepcopy(row.labels),
            note=row.note,
            search_text=row.search_text,
            verified_factors=[],
            error_score=0.0,
            fencing_seq=0,
            event_seq=0,
            created_at=self._now,
            updated_at=self._now,
        )
        self.state.identities[row.id] = identity
        if row.profile_secret is not None:
            self.state.profile_secrets[row.id] = row.profile_secret
        return True

    async def find_by_refs(self, refs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], tuple[UUID, UUID]]:
        wanted = set(refs)
        return {
            (row.service, row.external_ref): (row.id, row.space_id)
            for row in self.state.identities.values()
            if (row.service, row.external_ref) in wanted
        }

    async def existing_identities(self, ids: Sequence[UUID]) -> set[UUID]:
        return {identity_id for identity_id in ids if self._own(identity_id) is not None}

    async def put_credentials(self, identity_id: UUID, payload: EncryptedPayload) -> None:
        if self._own(identity_id):
            self.state.credentials[identity_id] = payload

    async def put_session(
        self,
        identity_id: UUID,
        slot: str,
        payload: EncryptedPayload,
        *,
        fencing: int,
        expires_at: datetime | None = None,
    ) -> bool:
        if self._own(identity_id) is None:
            return False
        old = self.state.sessions.get((identity_id, slot))
        if old is not None and old[2] > fencing:
            return False
        self.state.sessions[(identity_id, slot)] = (payload, "valid", fencing, expires_at)
        return True

    async def insert_link(self, identity_id: UUID, role: str, linked_identity_id: UUID) -> bool:
        key = (identity_id, role)
        if key in self.state.links or self._own(identity_id) is None or self._own(linked_identity_id) is None:
            return False
        self.state.links[key] = linked_identity_id
        return True

    async def get_profile_secret(self, identity_id: UUID) -> EncryptedPayload | None:
        return self.state.profile_secrets.get(identity_id) if self._own(identity_id) else None

    async def set_profile_secret(self, identity_id: UUID, payload: EncryptedPayload | None) -> None:
        if self._own(identity_id) is None:
            return
        if payload is None:
            self.state.profile_secrets.pop(identity_id, None)
        else:
            self.state.profile_secrets[identity_id] = payload

    async def incoming_links(self, identity_id: UUID) -> list[tuple[UUID, str]]:
        return [
            (source, role) for (source, role), target in self.state.links.items() if target == identity_id
        ]

    async def delete_identity(self, identity_id: UUID) -> bool:
        if self._own(identity_id) is None:
            return False
        del self.state.identities[identity_id]
        self.state.credentials.pop(identity_id, None)
        self.state.profile_secrets.pop(identity_id, None)
        self.state.bindings.pop(identity_id, None)
        for region_key in [item for item in self.state.regions if item[0] == identity_id]:
            del self.state.regions[region_key]
        for session_key in [item for item in self.state.sessions if item[0] == identity_id]:
            del self.state.sessions[session_key]
        for data_key in [item for item in self.state.data if item[0] == identity_id]:
            del self.state.data[data_key]
        for verification_key in [item for item in self.state.verifications if item[0] == identity_id]:
            del self.state.verifications[verification_key]
        for key, target in list(self.state.links.items()):
            if key[0] == identity_id or target == identity_id:
                del self.state.links[key]
        return True

    async def load_data(self, identity_id: UUID, namespace: str) -> DataRow | None:
        return self.state.data.get((identity_id, namespace)) if self._own(identity_id) else None

    async def save_data(
        self,
        identity_id: UUID,
        namespace: str,
        data: dict[str, Any],
        schema_version: int,
        expected_version: int | None,
    ) -> int | None:
        key = (identity_id, namespace)
        current = self.state.data.get(key)
        if self._own(identity_id) is None:
            return None
        if expected_version is None:
            if current is not None:
                return None
            version = 1
        else:
            if current is None or current.version != expected_version:
                return None
            version = expected_version + 1
        self.state.data[key] = DataRow(deepcopy(data), schema_version, version, self._now)
        return version

    async def delete_link(self, identity_id: UUID, role: str) -> UUID | None:
        return self.state.links.pop((identity_id, role), None) if self._own(identity_id) else None

    def _link(self, source: UUID, role: str, target: UUID, outgoing: bool) -> LinkRow:
        other = self.state.identities[target if outgoing else source]
        return LinkRow(source, role, target, other.service, other.external_ref, other.created_at)

    async def links_of(self, identity_id: UUID) -> tuple[list[LinkRow], list[LinkRow]]:
        outgoing = [self._link(s, r, t, True) for (s, r), t in self.state.links.items() if s == identity_id]
        incoming = [self._link(s, r, t, False) for (s, r), t in self.state.links.items() if t == identity_id]
        return outgoing, incoming

    async def link_counts(self, ids: Sequence[UUID]) -> dict[UUID, tuple[int, int, list[str]]]:
        result: dict[UUID, tuple[int, int, list[str]]] = {}
        for identity_id in ids:
            outgoing = [role for (source, role), _ in self.state.links.items() if source == identity_id]
            incoming = sum(target == identity_id for target in self.state.links.values())
            result[identity_id] = len(outgoing), incoming, sorted(outgoing)
        return result

    async def dependents(self, identity_id: UUID, max_depth: int) -> list[tuple[UUID, int, list[str]]]:
        found: dict[UUID, tuple[int, list[str]]] = {}
        frontier: list[tuple[UUID, list[str]]] = [(identity_id, [])]
        for depth in range(1, max_depth + 1):
            upcoming: list[tuple[UUID, list[str]]] = []
            for target, path in frontier:
                for (source, role), linked in self.state.links.items():
                    if linked == target and source not in found and source != identity_id:
                        found[source] = depth, [role, *path]
                        upcoming.append((source, [role, *path]))
            frontier = upcoming
        return [(key, depth, path) for key, (depth, path) in found.items()]

    def _candidate(self, row: IdentityRow, query: CandidateQuery) -> bool:
        now = self._now
        if (
            row.service != query.service
            or row.lifecycle not in query.lifecycles
            or row.standing not in query.standing
        ):
            return False
        if query.auth_states is not None:
            if row.auth_state not in query.auth_states:
                return False
        elif query.needs_auth and row.auth_state != "authenticated":
            return False
        if row.availability != "available" and not (row.availability_until and row.availability_until <= now):
            return False
        if not query.required_factors.issubset(row.verified_factors) or not query.tags.issubset(row.tags):
            return False
        if query.labels and any(row.labels.get(k) != v for k, v in query.labels.items()):
            return False
        if query.profile and any(row.profile.get(k) != v for k, v in query.profile.items()):
            return False
        if query.exclude and row.id in query.exclude:
            return False
        if query.among is not None and row.id not in set(query.among):
            return False
        lease = self.state.leases.get(row.id)
        if not query.shared and lease is not None and lease.expires_at > now:
            return False
        if query.proxy_wait:
            proxy_id = self.state.bindings.get(row.id)
            if proxy_id is not None and self.state.proxies[proxy_id].status != "alive":
                return False
        return True

    async def pick_candidate(self, query: CandidateQuery) -> IdentityRow | None:
        candidates = [
            row
            for row in self.state.identities.values()
            if row.space_id == self.space.id and self._candidate(row, query)
        ]
        last = {row.identity_id: row.acquired_at for row in self.state.history.values()}
        candidates.sort(key=lambda row: (last.get(row.id, datetime.min.replace(tzinfo=UTC)), row.id))
        return candidates[0] if candidates else None

    async def count_candidates(self, query: CandidateQuery, limit: int) -> int:
        return min(
            limit,
            sum(
                row.space_id == self.space.id and self._candidate(row, query)
                for row in self.state.identities.values()
            ),
        )

    async def find_live_lease(self, idempotency_key: str) -> LeaseRow | None:
        return next(
            (
                lease
                for lease in self.state.leases.values()
                if lease.idempotency_key == idempotency_key
                and lease.expires_at > self._now
                and self._own(lease.identity_id)
            ),
            None,
        )

    async def acquire_lease(
        self,
        identity: IdentityRow,
        *,
        pool: str,
        holder: str,
        ttl_seconds: float,
        idempotency_key: str | None,
        shared: bool,
        proxy_id: UUID | None = None,
    ) -> tuple[LeaseRow, LeaseRow | None]:
        current = self._own(identity.id)
        if current is None:
            raise LeaseUnavailable(f"аккаунт {identity.id} недоступен")
        reclaimed: LeaseRow | None = None
        old = self.state.leases.get(identity.id)
        if not shared and old is not None:
            if old.expires_at > self._now:
                raise LeaseUnavailable(f"аккаунт {identity.id} уже в аренде")
            reclaimed = old
            history = self.state.history[old.lease_id]
            if history.released_at is None:
                self.state.history[old.lease_id] = replace(
                    history,
                    released_at=old.expires_at,
                    duration=old.expires_at - history.acquired_at,
                    end_reason="expired",
                )
        fencing = current.fencing_seq + 1
        self.state.identities[identity.id] = replace(current, fencing_seq=fencing, updated_at=self._now)
        lease = LeaseRow(
            uuid4(),
            uuid4(),
            identity.id,
            fencing,
            pool,
            holder,
            self._now,
            self._now + timedelta(seconds=ttl_seconds),
            idempotency_key,
            proxy_id,
        )
        if not shared:
            self.state.leases[identity.id] = lease
        self.state.history[lease.lease_id] = LeaseHistoryRow(
            lease.lease_id,
            identity.id,
            fencing,
            pool,
            holder,
            proxy_id,
            self._now,
            self._now,
            None,
            None,
            None,
            {},
            0,
            0,
            None,
        )
        return lease, reclaimed

    async def extend_lease(
        self, lease: LeaseRow, ttl_seconds: float, counters: LeaseCounters, *, shared: bool
    ) -> datetime | None:
        if not shared:
            current = self.state.leases.get(lease.identity_id)
            if current is None or current.token != lease.token or current.expires_at <= self._now:
                return None
        history = self.state.history.get(lease.lease_id)
        if history is None or history.released_at is not None:
            return None
        expires_at = self._now + timedelta(seconds=ttl_seconds)
        if not shared and current is not None:
            self.state.leases[lease.identity_id] = replace(current, expires_at=expires_at)
        self.state.history[lease.lease_id] = replace(
            history, last_heartbeat_at=self._now, **_merge_counters(history, counters)
        )
        return expires_at

    async def release_lease(
        self, lease: LeaseRow, reason: str, counters: LeaseCounters, *, shared: bool
    ) -> bool:
        history = self.state.history.get(lease.lease_id)
        if history is None or history.released_at is not None:
            return False
        if not shared:
            current = self.state.leases.get(lease.identity_id)
            if current is None or current.token != lease.token:
                return False
            self.state.leases.pop(lease.identity_id)
        self.state.history[lease.lease_id] = replace(
            history,
            released_at=self._now,
            duration=self._now - history.acquired_at,
            end_reason=reason,
            **_merge_counters(history, counters),
        )
        return True

    async def current_fencing(self, identity_id: UUID, *, lock: bool = False) -> int | None:
        row = self._own(identity_id)
        return row.fencing_seq if row else None

    async def revoke_lease(self, identity_id: UUID) -> LeaseRow | None:
        lease = self.state.leases.get(identity_id)
        if lease is None or lease.expires_at <= self._now or self._own(identity_id) is None:
            return None
        self.state.leases.pop(identity_id)
        identity = self.state.identities[identity_id]
        self.state.identities[identity_id] = replace(
            identity, fencing_seq=identity.fencing_seq + 1, updated_at=self._now
        )
        history = self.state.history[lease.lease_id]
        self.state.history[lease.lease_id] = replace(
            history, released_at=self._now, duration=self._now - history.acquired_at, end_reason="revoked"
        )
        return lease

    async def charge_quotas(
        self, identity_id: UUID, charges: Sequence[QuotaCharge], *, operation_key: str | None, kind: str
    ) -> ChargeOutcome:
        if operation_key is not None and operation_key in self.state.operation_keys:
            return ChargeOutcome(duplicate=True)
        planned: list[tuple[tuple[UUID, str, int, datetime], int, QuotaCharge]] = []
        for charge in charges:
            start = _window_start(self._now, charge)
            key = (identity_id, charge.kind, charge.window_seconds, start)
            used = self.state.quotas.get(key, 0)
            if used >= charge.limit:
                return ChargeOutcome(exhausted=QuotaWindow(charge.kind, charge.window_seconds, start, used))
            planned.append((key, used + 1, charge))
        if operation_key is not None:
            self.state.operation_keys[operation_key] = (identity_id, kind, None, self._now)
        reached: list[QuotaWindow] = []
        for key, used, charge in planned:
            self.state.quotas[key] = used
            if used >= charge.limit:
                reached.append(QuotaWindow(charge.kind, charge.window_seconds, key[3], used))
        return ChargeOutcome(reached=tuple(reached))

    async def bump_pacing(self, identity_id: UUID, charge: QuotaCharge) -> bool:
        start = _window_start(self._now, charge)
        key = (identity_id, charge.kind, charge.window_seconds, start)
        used = self.state.quotas.get(key, 0) + 1
        reached = used >= charge.limit
        self.state.quotas[key] = 0 if reached else used
        return reached

    async def quota_usage(self, identity_id: UUID) -> list[QuotaWindow]:
        rows = [
            QuotaWindow(kind, seconds, start, used)
            for (owner, kind, seconds, start), used in self.state.quotas.items()
            if owner == identity_id and start + timedelta(seconds=seconds) > self._now
        ]
        return sorted(rows, key=lambda row: (row.kind, row.window_seconds))

    async def finish_operation_key(self, key: str, outcome: str) -> None:
        old = self.state.operation_keys.get(key)
        if old and self._own(old[0]):
            self.state.operation_keys[key] = (old[0], old[1], outcome, old[3])

    async def add_error_score(self, identity_id: UUID, delta: float) -> float:
        row = self._own(identity_id)
        if row is None:
            raise KeyError(identity_id)
        score = max(0.0, row.error_score + delta)
        self.state.identities[identity_id] = replace(row, error_score=score, updated_at=self._now)
        return score

    async def decay_error_score(self, identity_id: UUID, factor: float) -> None:
        row = self._own(identity_id)
        if row is not None and row.error_score > 0:
            score = row.error_score * factor
            self.state.identities[identity_id] = replace(
                row, error_score=0.0 if score < 0.01 else score, updated_at=self._now
            )

    async def set_session_status(self, identity_id: UUID, slot: str, status: str) -> None:
        key = (identity_id, slot)
        old = self.state.sessions.get(key)
        if old is not None and self._own(identity_id):
            self.state.sessions[key] = (old[0], status, old[2], old[3])

    async def insert_proxy(self, row: NewProxy) -> tuple[UUID, bool]:
        for proxy in self.state.proxies.values():
            if proxy.space_id == self.space.id and proxy.fingerprint == row.fingerprint:
                return proxy.id, False
        proxy = ProxyRow(
            id=row.id,
            space_id=self.space.id,
            pool=row.pool,
            scheme=row.scheme,
            host=row.host,
            port=row.port,
            fingerprint=row.fingerprint,
            status="unknown",
            error_score=0.0,
            fail_streak=0,
            next_check_at=None,
            status_changed_at=self._now,
            status_source="system",
            checked_at=None,
            pinned_until=None,
            pending_observation=None,
            max_identities=row.max_identities,
            created_at=self._now,
            secret=row.secret,
        )
        self.state.proxies[row.id] = proxy
        self.state.proxy_load[row.id] = 0
        return row.id, True

    async def get_proxy(self, proxy_id: UUID) -> ProxyRow | None:
        proxy = self.state.proxies.get(proxy_id)
        return proxy if proxy is not None and proxy.space_id == self.space.id else None

    async def update_proxy(
        self, proxy_id: UUID, values: dict[str, Any], expected: dict[str, Any] | None = None
    ) -> bool:
        row = await self.get_proxy(proxy_id)
        if row is None or any(getattr(row, key) != value for key, value in (expected or {}).items()):
            return False
        self.state.proxies[proxy_id] = replace(row, **values)
        return True

    async def get_binding(self, identity_id: UUID) -> ProxyRow | None:
        proxy_id = self.state.bindings.get(identity_id)
        return await self.get_proxy(proxy_id) if proxy_id is not None and self._own(identity_id) else None

    async def choose_proxy(self, pool: str, max_identities: int | None, *, claim: bool) -> ProxyRow | None:
        candidates = []
        for proxy in self.state.proxies.values():
            capacity = proxy.max_identities if max_identities is None else max_identities
            load = self.state.proxy_load.get(proxy.id, 0)
            if (
                proxy.space_id == self.space.id
                and proxy.pool == pool
                and proxy.status == "alive"
                and load < capacity
            ):
                candidates.append(proxy)
        candidates.sort(
            key=lambda proxy: (self.state.proxy_load.get(proxy.id, 0), proxy.error_score, proxy.id)
        )
        if not candidates:
            return None
        selected = candidates[0]
        if claim:
            self.state.proxy_load[selected.id] = self.state.proxy_load.get(selected.id, 0) + 1
        return selected

    async def bind_proxy(self, identity_id: UUID, proxy_id: UUID, previous: UUID | None) -> None:
        if self._own(identity_id) is None or await self.get_proxy(proxy_id) is None:
            return
        self.state.bindings[identity_id] = proxy_id
        if previous is not None and previous != proxy_id:
            self.state.proxy_load[previous] = max(0, self.state.proxy_load.get(previous, 0) - 1)

    async def adjust_proxy_load(self, proxy_id: UUID, delta: int) -> None:
        if await self.get_proxy(proxy_id) is not None:
            self.state.proxy_load[proxy_id] = max(0, self.state.proxy_load.get(proxy_id, 0) + delta)

    async def due_proxies(self, limit: int) -> list[UUID]:
        due = [
            row
            for row in self.state.proxies.values()
            if row.space_id == self.space.id
            and row.status == "quarantined"
            and row.next_check_at is not None
            and row.next_check_at <= self._now
        ]
        due.sort(key=lambda row: row.next_check_at or self._now)
        return [row.id for row in due[:limit]]

    async def load_verification(self, identity_id: UUID, factor: str) -> VerificationRow | None:
        return self.state.verifications.get((identity_id, factor)) if self._own(identity_id) else None

    async def save_verification(
        self, identity_id: UUID, factor: str, values: dict[str, Any], evidence: EncryptedPayload | None = None
    ) -> None:
        if self._own(identity_id) is None:
            return
        previous = self.state.verifications.get(
            (identity_id, factor), VerificationRow(0, None, None, None, None, None)
        )
        payload = asdict(previous)
        payload.update(values)
        if evidence is not None:
            payload["evidence"] = evidence
        self.state.verifications[(identity_id, factor)] = VerificationRow(**payload)

    async def due_regions(self, region: str, limit: int) -> list[tuple[UUID, str]]:
        rows = [
            (identity_id, key, state.until)
            for (identity_id, name, key), state in self.state.regions.items()
            if name == region
            and state.until is not None
            and state.until <= self._now
            and self._own(identity_id) is not None
        ]
        rows.sort(key=lambda item: item[2] or self._now)
        return [(identity_id, key) for identity_id, key, _ in rows[:limit]]

    async def stale_identities(
        self, service: str, region: str, key: str, older_than_seconds: float, limit: int
    ) -> list[UUID]:
        threshold = self._now - timedelta(seconds=older_than_seconds)
        rows = []
        for identity in self.state.identities.values():
            if identity.space_id != self.space.id or identity.service != service:
                continue
            state = self.state.regions.get((identity.id, region, key))
            if state is None or state.synced_at is None or state.synced_at < threshold:
                rows.append((state.synced_at if state else None, identity.id))
        rows.sort(
            key=lambda item: (item[0] is not None, item[0] or datetime.min.replace(tzinfo=UTC), item[1])
        )
        return [identity_id for _, identity_id in rows[:limit]]

    @staticmethod
    def _contains(container: Any, expected: Any) -> bool:
        if isinstance(expected, dict) and isinstance(container, dict):
            return all(
                key in container and MemoryTx._contains(container[key], value)
                for key, value in expected.items()
            )
        if isinstance(expected, list) and isinstance(container, list):
            return all(item in container for item in expected)
        return bool(container == expected)

    def _matches_search(self, row: IdentityRow, flt: SearchFilter) -> bool:
        if flt.exact_id is not None and row.id != flt.exact_id:
            return False
        if flt.services and row.service not in flt.services:
            return False
        if flt.tags and not flt.tags.issubset(row.tags):
            return False
        for column, values in flt.regions.items():
            if getattr(row, column) not in values:
                return False
        if flt.any_region and not (
            {row.lifecycle, row.standing, row.auth_state, row.availability} & flt.any_region
        ):
            return False
        for source, variants in flt.contains:
            value = getattr(row, source)
            if not any(self._contains(value, variant) for variant in variants):
                return False
        haystack = row.search_text.casefold()
        return all(term.casefold() in haystack for term in flt.terms)

    async def search_identities(
        self,
        flt: SearchFilter,
        *,
        limit: int,
        after: tuple[datetime, UUID] | None,
        count_limit: int | None,
    ) -> tuple[list[IdentityRow], int | None]:
        rows = [
            row
            for row in self.state.identities.values()
            if row.space_id == self.space.id and self._matches_search(row, flt)
        ]
        rows.sort(key=lambda row: (row.created_at, row.id), reverse=True)
        count = None if count_limit is None else min(len(rows), count_limit + 1)
        if after is not None:
            rows = [row for row in rows if (row.created_at, row.id) < after]
        return rows[:limit], count

    async def insert_lease_logs(self, rows: Sequence[dict[str, Any]]) -> None:
        for row in rows:
            identity_id = row.get("identity_id")
            if isinstance(identity_id, UUID) and self._own(identity_id):
                self.state.lease_logs.append(deepcopy(row) | {"space_id": self.space.id})

    async def list_regions(self, identity_id: UUID) -> list[tuple[str, str, RegionState]]:
        if self._own(identity_id) is None:
            return []
        return sorted(
            [
                (region, key, state)
                for (owner, region, key), state in self.state.regions.items()
                if owner == identity_id
            ],
            key=lambda item: (item[0], item[1]),
        )

    async def current_lease(self, identity_id: UUID) -> LeaseRow | None:
        lease = self.state.leases.get(identity_id)
        return (
            lease if lease is not None and lease.expires_at > self._now and self._own(identity_id) else None
        )

    async def data_namespaces(self, identity_id: UUID) -> list[tuple[str, int, int, datetime]]:
        if self._own(identity_id) is None:
            return []
        return sorted(
            [
                (namespace, row.schema_version, row.version, row.updated_at)
                for (owner, namespace), row in self.state.data.items()
                if owner == identity_id
            ]
        )

    async def region_counts(self, service: str | None) -> list[tuple[str, str, str, int]]:
        counts: dict[tuple[str, str, str], int] = defaultdict(int)
        for row in self.state.identities.values():
            if row.space_id != self.space.id or (service is not None and row.service != service):
                continue
            for column in ("lifecycle", "standing", "auth_state", "availability"):
                counts[(row.service, column, str(getattr(row, column)))] += 1
        return [(*key, count) for key, count in sorted(counts.items())]

    async def lease_history(
        self, identity_id: UUID, *, limit: int, before: datetime | None
    ) -> list[LeaseHistoryRow]:
        if self._own(identity_id) is None:
            return []
        rows = [
            row
            for row in self.state.history.values()
            if row.identity_id == identity_id and (before is None or row.acquired_at < before)
        ]
        return sorted(rows, key=lambda row: row.acquired_at, reverse=True)[:limit]

    async def lease_stats(self, identity_id: UUID, since: datetime) -> LeaseStatsRow:
        rows = [
            row
            for row in self.state.history.values()
            if row.identity_id == identity_id and row.acquired_at >= since
        ]
        durations = [row.duration or (self._now - row.acquired_at) for row in rows]
        total = sum(durations, timedelta())
        reasons: dict[str, int] = defaultdict(int)
        operations: dict[str, int] = defaultdict(int)
        for row in rows:
            if row.end_reason is not None:
                reasons[row.end_reason] += 1
            for kind, count in row.operations.items():
                operations[kind] += count
        return LeaseStatsRow(
            len(rows),
            total,
            total / len(durations) if durations else None,
            max(durations) if durations else None,
            dict(reasons),
            dict(operations),
            sum(row.succeeded for row in rows),
            sum(row.failed for row in rows),
        )

    async def identities_page(self, service: str, after: UUID | None, limit: int) -> list[IdentityRow]:
        rows = [
            row
            for row in self.state.identities.values()
            if row.space_id == self.space.id and row.service == service and (after is None or row.id > after)
        ]
        return sorted(rows, key=lambda row: row.id)[:limit]

    async def open_challenge(
        self,
        challenge_id: UUID,
        identity_id: UUID,
        kind: str,
        hint: str,
        context: EncryptedPayload,
        expires_at: datetime | None,
    ) -> bool:
        if self._own(identity_id) is None or await self.open_challenge_of(identity_id) is not None:
            return False
        self.state.challenges[challenge_id] = ChallengeRow(
            challenge_id, identity_id, kind, hint, context, None, None, self._now, expires_at, None, None
        )
        return True

    async def get_challenge(self, challenge_id: UUID) -> ChallengeRow | None:
        row = self.state.challenges.get(challenge_id)
        return row if row is not None and self._own(row.identity_id) else None

    async def open_challenge_of(self, identity_id: UUID) -> ChallengeRow | None:
        return (
            next(
                (
                    row
                    for row in self.state.challenges.values()
                    if row.identity_id == identity_id and row.resolved_at is None
                ),
                None,
            )
            if self._own(identity_id)
            else None
        )

    async def answer_challenge(self, challenge_id: UUID, nonce: bytes, ciphertext: bytes) -> bool:
        row = await self.get_challenge(challenge_id)
        if row is None or row.resolved_at is not None:
            return False
        self.state.challenges[challenge_id] = replace(
            row, answer_nonce=nonce, answer_ciphertext=ciphertext, answered_at=self._now
        )
        return True

    async def resolve_challenge(self, challenge_id: UUID) -> bool:
        row = await self.get_challenge(challenge_id)
        if row is None or row.resolved_at is not None:
            return False
        self.state.challenges[challenge_id] = replace(row, resolved_at=self._now)
        return True

    async def expired_challenges(self, limit: int) -> list[ChallengeRow]:
        rows = [
            row
            for row in self.state.challenges.values()
            if self._own(row.identity_id)
            and row.resolved_at is None
            and row.expires_at is not None
            and row.expires_at <= self._now
        ]
        return sorted(rows, key=lambda row: row.expires_at or self._now)[:limit]

    async def get_credentials(self, identity_id: UUID) -> EncryptedPayload | None:
        return self.state.credentials.get(identity_id) if self._own(identity_id) else None

    async def get_session(self, identity_id: UUID, slot: str) -> tuple[EncryptedPayload, str] | None:
        row = self.state.sessions.get((identity_id, slot)) if self._own(identity_id) else None
        return (row[0], row[1]) if row is not None else None

    async def insert_webhook_endpoint(self, row: NewWebhookEndpoint) -> WebhookEndpointRow:
        endpoint = WebhookEndpointRow(
            row.id,
            self.space.id,
            self.space.slug,
            row.url,
            row.description,
            list(row.event_types),
            deepcopy(row.filter),
            row.min_severity,
            True,
            None,
            0,
            self._now,
            row.secret,
        )
        self.state.webhook_endpoints[row.id] = endpoint
        return endpoint

    async def get_webhook_endpoint(self, endpoint_id: UUID) -> WebhookEndpointRow | None:
        row = self.state.webhook_endpoints.get(endpoint_id)
        return row if row is not None and row.space_id == self.space.id else None

    async def update_webhook_secret(self, endpoint_id: UUID, secret: EncryptedPayload) -> bool:
        row = await self.get_webhook_endpoint(endpoint_id)
        if row is None:
            return False
        self.state.webhook_endpoints[endpoint_id] = replace(row, secret=secret)
        return True

    async def set_webhook_enabled(self, endpoint_id: UUID, enabled: bool, reason: str | None) -> bool:
        row = await self.get_webhook_endpoint(endpoint_id)
        if row is None:
            return False
        self.state.webhook_endpoints[endpoint_id] = replace(
            row,
            enabled=enabled,
            disabled_reason=None if enabled else reason,
            failure_streak=0 if enabled else row.failure_streak,
        )
        return True

    async def retry_webhook_delivery(self, delivery_id: int) -> bool:
        row = self.state.webhook_deliveries.get(delivery_id)
        endpoint = await self.get_webhook_endpoint(row.endpoint_id) if row else None
        if row is None or endpoint is None:
            return False
        self.state.webhook_deliveries[delivery_id] = replace(
            row, status="pending", next_attempt_at=self._now, error=None, delivered_at=None
        )
        self.state.webhook_endpoints[endpoint.id] = replace(
            endpoint, enabled=True, disabled_reason=None, failure_streak=0
        )
        return True

    async def webhook_deliveries(self, endpoint_id: UUID, limit: int) -> list[WebhookDeliveryRow]:
        if await self.get_webhook_endpoint(endpoint_id) is None:
            return []
        rows = [row for row in self.state.webhook_deliveries.values() if row.endpoint_id == endpoint_id]
        return sorted(rows, key=lambda row: row.id, reverse=True)[:limit]

    async def insert_notification_channel(self, row: NewNotificationChannel) -> NotificationChannelRow:
        channel = NotificationChannelRow(row.id, row.title, deepcopy(row.config), True, self._now)
        self.state.notification_channels[row.id] = (channel, row.secret, self.space.id)
        return channel

    async def notification_channels(self) -> list[NotificationChannelRow]:
        return sorted(
            [
                channel
                for channel, _, space_id in self.state.notification_channels.values()
                if space_id == self.space.id
            ],
            key=lambda row: (row.created_at, row.id),
        )

    async def update_notification_channel(
        self, channel_id: UUID, values: dict[str, Any], secret: EncryptedPayload | None
    ) -> bool:
        item = self.state.notification_channels.get(channel_id)
        if item is None:
            return False
        channel, old_secret, space_id = item
        if space_id != self.space.id:
            return False
        self.state.notification_channels[channel_id] = (
            replace(channel, **values),
            secret or old_secret,
            space_id,
        )
        return True

    async def delete_notification_channel(self, channel_id: UUID) -> bool:
        item = self.state.notification_channels.get(channel_id)
        if item is None or item[2] != self.space.id:
            return False
        del self.state.notification_channels[channel_id]
        for rule_id in [
            rule.id for rule in self.state.notification_rules.values() if rule.channel_id == channel_id
        ]:
            del self.state.notification_rules[rule_id]
            self.state.notification_rule_spaces.pop(rule_id, None)
        return True

    async def insert_notification_rule(self, row: NewNotificationRule) -> NotificationRuleRow:
        item = self.state.notification_channels.get(row.channel_id)
        if item is None or item[2] != self.space.id:
            raise KeyError(row.channel_id)
        rule = NotificationRuleRow(
            row.id,
            row.channel_id,
            row.name,
            True,
            list(row.event_types),
            row.min_severity,
            deepcopy(row.filter),
            row.digest_seconds,
            row.template,
            row.created_by,
            self._now,
        )
        self.state.notification_rules[row.id] = rule
        self.state.notification_rule_spaces[row.id] = self.space.id
        return rule

    async def notification_rules(self) -> list[NotificationRuleRow]:
        return sorted(
            [
                row
                for row in self.state.notification_rules.values()
                if self.state.notification_rule_spaces.get(row.id) == self.space.id
            ],
            key=lambda row: (row.name, row.id),
        )

    async def update_notification_rule(self, rule_id: UUID, values: dict[str, Any]) -> bool:
        rule = self.state.notification_rules.get(rule_id)
        if rule is None or self.state.notification_rule_spaces.get(rule_id) != self.space.id:
            return False
        self.state.notification_rules[rule_id] = replace(rule, **values, updated_at=self._now)
        return True

    async def delete_notification_rule(self, rule_id: UUID) -> bool:
        if self.state.notification_rule_spaces.get(rule_id) != self.space.id:
            return False
        self.state.notification_rule_spaces.pop(rule_id, None)
        return self.state.notification_rules.pop(rule_id, None) is not None

    async def due_identity_timers(self, limit: int) -> list[tuple[UUID, str]]:
        due: list[tuple[datetime, UUID, str]] = []
        for row in self.state.identities.values():
            if row.space_id != self.space.id:
                continue
            if row.standing_until is not None and row.standing_until <= self._now:
                due.append((row.standing_until, row.id, "standing"))
            if row.availability_until is not None and row.availability_until <= self._now:
                due.append((row.availability_until, row.id, "availability"))
        due.sort()
        return [(identity_id, region) for _, identity_id, region in due[:limit]]

    async def expire_leases(self, limit: int) -> int:
        expired = sorted(
            [
                lease
                for lease in self.state.leases.values()
                if self._own(lease.identity_id) and lease.expires_at <= self._now
            ],
            key=lambda row: row.expires_at,
        )[:limit]
        drafts: list[EventDraft] = []
        for lease in expired:
            self.state.leases.pop(lease.identity_id, None)
            history = self.state.history[lease.lease_id]
            if history.released_at is None:
                self.state.history[lease.lease_id] = replace(
                    history,
                    released_at=lease.expires_at,
                    duration=lease.expires_at - history.acquired_at,
                    end_reason="expired",
                )
                identity = self.state.identities[lease.identity_id]
                drafts.append(
                    EventDraft(
                        "lease.expired",
                        "maintenance",
                        service=identity.service,
                        identity_id=identity.id,
                        lease_id=lease.lease_id,
                        data={"pool": lease.pool, "holder": lease.holder},
                    )
                )
        await self.emit(drafts)
        return len(expired)


class MemoryStore(Store):
    """Полный Store без внешней инфраструктуры, предназначенный для тестов и локальных примеров."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()
        self._state = _MemoryState()
        self._lock = asyncio.Lock()
        self._consumer_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._listeners: defaultdict[str, set[asyncio.Event]] = defaultdict(set)

    @asynccontextmanager
    async def transaction(self, space: SpaceRef, *, session: Any | None = None) -> AsyncIterator[MemoryTx]:
        if session is not None:
            raise TypeError("MemoryStore не поддерживает внешнюю SQL-сессию")
        async with self._lock:
            snapshot = deepcopy(self._state)
            tx = MemoryTx(self, space, self.clock.now())
            try:
                yield tx
            except BaseException:
                self._state = snapshot
                raise
            else:
                if tx._notify:
                    for event in self._listeners.get("ih_events", ()):
                        event.set()

    async def sync_services(self, services: Sequence[ServiceRow]) -> None:
        async with self._lock:
            self._state.services = {service.name: deepcopy(service) for service in services}

    async def ensure_space(
        self, slug: str, title: str | None = None, space_id: UUID | None = None
    ) -> SpaceRef:
        async with self._lock:
            existing = self._state.spaces.get(slug)
            if existing is not None:
                if space_id is not None and existing.id != space_id:
                    raise SpaceMismatch(f"пространство {slug!r} уже существует с другим id")
                return existing
            space = SpaceRef(space_id or uuid4(), slug, title or slug)
            self._state.spaces[slug] = space
            return space

    async def get_space(self, slug: str) -> SpaceRef | None:
        async with self._lock:
            return self._state.spaces.get(slug)

    async def process_events(
        self,
        consumer: str,
        handler: Callable[[list[Event]], Awaitable[None]],
        *,
        limit: int,
    ) -> int | None:
        consumer_lock = self._consumer_locks[consumer]
        if consumer_lock.locked():
            return None
        async with consumer_lock:
            async with self._lock:
                cursor = self._state.cursors.get(consumer, 0)
                events = [
                    event for event, _, external in self._state.events if external and event.id > cursor
                ][:limit]
            if not events:
                return 0
            await handler(events)
            async with self._lock:
                self._state.cursors[consumer] = max(self._state.cursors.get(consumer, 0), events[-1].id)
            return len(events)

    @asynccontextmanager
    async def listen(self, channel: str) -> AsyncIterator[asyncio.Event | None]:
        event = asyncio.Event()
        self._listeners[channel].add(event)
        try:
            yield event
        finally:
            self._listeners[channel].discard(event)

    async def enabled_webhook_endpoints(self) -> list[WebhookEndpointRow]:
        async with self._lock:
            return [row for row in self._state.webhook_endpoints.values() if row.enabled]

    async def identity_labels(self, identity_ids: Sequence[UUID]) -> dict[UUID, dict[str, Any]]:
        wanted = set(identity_ids)
        async with self._lock:
            return {
                row.id: deepcopy(row.labels) for row in self._state.identities.values() if row.id in wanted
            }

    async def enqueue_webhook_deliveries(self, pairs: Sequence[tuple[UUID, UUID]]) -> int:
        async with self._lock:
            existing = {(row.endpoint_id, row.event_id) for row in self._state.webhook_deliveries.values()}
            inserted = 0
            now = self.clock.now()
            for endpoint_id, event_id in pairs:
                if (endpoint_id, event_id) in existing or endpoint_id not in self._state.webhook_endpoints:
                    continue
                delivery_id = self._state.next_webhook_delivery_id
                self._state.next_webhook_delivery_id += 1
                self._state.webhook_deliveries[delivery_id] = WebhookDeliveryRow(
                    delivery_id, endpoint_id, event_id, "pending", 0, now, None, None, None, now, None
                )
                existing.add((endpoint_id, event_id))
                inserted += 1
            return inserted

    async def claim_webhook_deliveries(self, *, limit: int, claim_for: timedelta) -> list[WebhookAttempt]:
        async with self._lock:
            now = self.clock.now()
            rows = sorted(
                [
                    row
                    for row in self._state.webhook_deliveries.values()
                    if row.status in {"pending", "failed"} and row.next_attempt_at <= now
                ],
                key=lambda row: (row.next_attempt_at, row.id),
            )[:limit]
            attempts: list[WebhookAttempt] = []
            events = {event.event_id: event for event, _, _ in self._state.events}
            for row in rows:
                endpoint = self._state.webhook_endpoints.get(row.endpoint_id)
                event = events.get(row.event_id)
                if endpoint is None or event is None or not endpoint.enabled:
                    continue
                attempt = row.attempt + 1
                self._state.webhook_deliveries[row.id] = replace(
                    row, attempt=attempt, next_attempt_at=now + claim_for
                )
                attempts.append(
                    WebhookAttempt(row.id, attempt, endpoint.id, endpoint.url, endpoint.secret, event)
                )
            return attempts

    async def finish_webhook_delivery(
        self,
        delivery_id: int,
        attempt: int,
        *,
        succeeded: bool,
        response_code: int | None,
        response_ms: int,
        error: str | None,
        retry_after: timedelta,
        max_failures: int,
    ) -> bool:
        async with self._lock:
            row = self._state.webhook_deliveries.get(delivery_id)
            if row is None or row.status not in {"pending", "failed"} or row.attempt != attempt:
                return False
            now = self.clock.now()
            endpoint = self._state.webhook_endpoints[row.endpoint_id]
            failure_streak = 0 if succeeded else endpoint.failure_streak + 1
            disabled = not succeeded and failure_streak >= max_failures
            self._state.webhook_endpoints[endpoint.id] = replace(
                endpoint,
                enabled=endpoint.enabled and not disabled,
                disabled_reason="consecutive_failures" if disabled else None,
                failure_streak=failure_streak,
            )
            self._state.webhook_deliveries[delivery_id] = replace(
                row,
                status="succeeded" if succeeded else ("dead" if disabled else "failed"),
                next_attempt_at=now + retry_after,
                response_code=response_code,
                response_ms=response_ms,
                error=error,
                delivered_at=now if succeeded else None,
            )
            if disabled:
                space = next(item for item in self._state.spaces.values() if item.id == endpoint.space_id)
                event = Event(
                    self._state.next_event_id,
                    uuid4(),
                    "webhook.endpoint.disabled",
                    Severity.CRITICAL,
                    now,
                    space.slug,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "system:webhooks",
                    {"endpoint_id": str(endpoint.id), "failure_streak": failure_streak},
                )
                self._state.next_event_id += 1
                self._state.events.append((event, endpoint.space_id, True))
                for signal in self._listeners.get("ih_events", ()):
                    signal.set()
            return True

    async def enabled_notification_targets(self) -> list[NotificationTargetRow]:
        async with self._lock:
            spaces = {space.id: space.slug for space in self._state.spaces.values()}
            targets = []
            for rule in self._state.notification_rules.values():
                item = self._state.notification_channels.get(rule.channel_id)
                if (
                    not rule.enabled
                    or item is None
                    or not item[0].enabled
                    or self._state.notification_rule_spaces.get(rule.id) != item[2]
                ):
                    continue
                channel, secret, space_id = item
                slug = spaces[space_id]
                targets.append(NotificationTargetRow(rule, slug, deepcopy(channel.config), secret))
            return targets

    async def enqueue_notifications(self, items: Sequence[NotificationEnqueue]) -> int:
        async with self._lock:
            inserted = 0
            existing = {(row["rule_id"], row["event_ids"][0]) for row in self._state.notifications.values()}
            for item in items:
                key = (item.rule_id, item.event_id)
                if key in existing:
                    continue
                if item.digest_key is not None:
                    digest = next(
                        (
                            row
                            for row in self._state.notifications.values()
                            if row["rule_id"] == item.rule_id
                            and row["digest_key"] == item.digest_key
                            and row["status"] == "pending"
                        ),
                        None,
                    )
                    if digest is not None:
                        digest["event_ids"].append(item.event_id)
                        existing.add(key)
                        inserted += 1
                        continue
                notification_id = self._state.next_notification_id
                self._state.next_notification_id += 1
                self._state.notifications[notification_id] = {
                    "id": notification_id,
                    "rule_id": item.rule_id,
                    "channel_id": item.channel_id,
                    "event_ids": [item.event_id],
                    "digest_key": item.digest_key,
                    "scheduled_at": item.scheduled_at,
                    "status": "pending",
                    "attempt": 0,
                    "error": None,
                }
                existing.add(key)
                inserted += 1
            return inserted

    async def claim_notifications(self, *, limit: int, claim_for: timedelta) -> list[NotificationAttempt]:
        async with self._lock:
            now = self.clock.now()
            due = sorted(
                [
                    row
                    for row in self._state.notifications.values()
                    if row["status"] in {"pending", "failed"} and row["scheduled_at"] <= now
                ],
                key=lambda row: (row["scheduled_at"], row["id"]),
            )[:limit]
            events = {event.event_id: event for event, _, _ in self._state.events}
            attempts = []
            for row in due:
                rule = self._state.notification_rules.get(row["rule_id"])
                channel_item = self._state.notification_channels.get(row["channel_id"])
                if rule is None or channel_item is None or not rule.enabled or not channel_item[0].enabled:
                    continue
                row["status"] = "failed"
                row["attempt"] += 1
                row["scheduled_at"] = now + claim_for
                channel, secret, _ = channel_item
                attempts.append(
                    NotificationAttempt(
                        row["id"],
                        row["attempt"],
                        rule.id,
                        channel.id,
                        rule.template,
                        deepcopy(channel.config),
                        secret,
                        tuple(events[event_id] for event_id in row["event_ids"] if event_id in events),
                    )
                )
            return attempts

    async def finish_notification(
        self,
        notification_id: int,
        attempt: int,
        *,
        succeeded: bool,
        error: str | None,
        retry_after: timedelta,
        max_attempts: int,
    ) -> bool:
        async with self._lock:
            row = self._state.notifications.get(notification_id)
            if row is None or row["status"] not in {"pending", "failed"} or row["attempt"] != attempt:
                return False
            row["status"] = "succeeded" if succeeded else ("dead" if attempt >= max_attempts else "failed")
            row["scheduled_at"] = self.clock.now() + retry_after
            row["error"] = error
            return True

    async def maintenance_spaces(self) -> list[SpaceRef]:
        async with self._lock:
            return sorted(self._state.spaces.values(), key=lambda row: row.slug)

    async def cleanup_maintenance(
        self, retention: Mapping[str, timedelta], names: frozenset[str]
    ) -> dict[str, int]:
        async with self._lock:
            now = self.clock.now()
            result: dict[str, int] = {}
            if "quota_windows" in names:
                keys = [key for key in self._state.quotas if key[3] + timedelta(seconds=key[2]) <= now]
                for key in keys:
                    del self._state.quotas[key]
                result["quota_windows"] = len(keys)
            cleanups: dict[str, tuple[Any, Callable[[Any], datetime]]] = {
                "operation_keys": (self._state.operation_keys, lambda item: item[3]),
                "lease_logs": (self._state.lease_logs, lambda item: item["ts"]),
                "lease_history": (self._state.history, lambda item: item.released_at or now),
                "events": (self._state.events, lambda item: item[0].occurred_at),
                "webhook_deliveries": (self._state.webhook_deliveries, lambda item: item.created_at),
                "notifications": (self._state.notifications, lambda item: item["scheduled_at"]),
            }
            for name in names & cleanups.keys():
                collection, timestamp = cleanups[name]
                retention_key = "deliveries" if name in {"webhook_deliveries", "notifications"} else name
                cutoff = now - retention[retention_key]
                if isinstance(collection, list):
                    old_count = len(collection)
                    collection[:] = [item for item in collection if timestamp(item) >= cutoff]
                    result[name] = old_count - len(collection)
                else:
                    keys = [key for key, item in collection.items() if timestamp(item) < cutoff]
                    for key in keys:
                        del collection[key]
                    result[name] = len(keys)
            return result

    async def doctor(self, *, fix: bool) -> list[DoctorIssue]:
        async with self._lock:
            issues: list[DoctorIssue] = []
            spaces = {space.id: space.slug for space in self._state.spaces.values()}
            event_seq: dict[UUID, int] = defaultdict(int)
            for event, _, _ in self._state.events:
                if event.identity_id is not None and event.sequence is not None:
                    event_seq[event.identity_id] = max(event_seq[event.identity_id], event.sequence)
            for identity_id, row in list(self._state.identities.items()):
                changes: dict[str, Any] = {}
                standing = self._state.regions.get((identity_id, "standing", ""))
                if standing is not None:
                    expected = standing.state, standing.code, standing.until
                    actual = row.standing, row.standing_code, row.standing_until
                    if actual != expected:
                        issues.append(
                            DoctorIssue("standing", spaces[row.space_id], identity_id, expected, actual)
                        )
                        changes |= {
                            "standing": standing.state,
                            "standing_code": standing.code,
                            "standing_until": standing.until,
                        }
                auth = self._state.regions.get((identity_id, "auth", "default"))
                if auth is not None and row.auth_state != auth.state:
                    issues.append(
                        DoctorIssue(
                            "auth_state", spaces[row.space_id], identity_id, auth.state, row.auth_state
                        )
                    )
                    changes["auth_state"] = auth.state
                verified = sorted(
                    key
                    for (owner, region, key), state in self._state.regions.items()
                    if owner == identity_id and region == "verification" and state.state == "verified"
                )
                if sorted(row.verified_factors) != verified:
                    issues.append(
                        DoctorIssue(
                            "verified_factors",
                            spaces[row.space_id],
                            identity_id,
                            verified,
                            list(row.verified_factors),
                        )
                    )
                    changes["verified_factors"] = verified
                expected_seq = event_seq.get(identity_id, 0)
                if row.event_seq < expected_seq:
                    issues.append(
                        DoctorIssue(
                            "event_seq", spaces[row.space_id], identity_id, expected_seq, row.event_seq
                        )
                    )
                    changes["event_seq"] = expected_seq
                if fix and changes:
                    self._state.identities[identity_id] = replace(row, **changes)
            for proxy_id, proxy in self._state.proxies.items():
                expected_load = sum(bound == proxy_id for bound in self._state.bindings.values())
                actual_load = self._state.proxy_load.get(proxy_id, 0)
                if actual_load != expected_load:
                    issues.append(
                        DoctorIssue(
                            "proxy.bound_count",
                            spaces[proxy.space_id],
                            proxy_id,
                            expected_load,
                            actual_load,
                        )
                    )
                    if fix:
                        self._state.proxy_load[proxy_id] = expected_load
            return issues

    async def rotate_key(self, envelope: Any, *, limit: int) -> dict[str, int]:
        active = envelope.active_key_id
        remaining = max(0, limit)
        counts: dict[str, int] = {}

        def rotate(name: str, mapping: Any, aad: Callable[[Any, Any], bytes]) -> None:
            nonlocal remaining
            count = 0
            for key, payload in list(mapping.items()):
                if remaining == 0:
                    break
                if payload.key_id == active:
                    continue
                mapping[key] = envelope.rewrap(payload, aad(key, payload))
                count += 1
                remaining -= 1
            if count:
                counts[name] = count

        async with self._lock:
            rotate(
                "credentials", self._state.credentials, lambda key, _: associated_data("ih_credentials", key)
            )
            rotate(
                "sessions",
                _PayloadView(self._state.sessions, 0),
                lambda key, _: associated_data("ih_sessions", key[0], key[1]),
            )
            rotate(
                "challenges",
                _AttributePayloadView(self._state.challenges, "context"),
                lambda key, _: associated_data(
                    "ih_challenges", self._state.challenges[key].identity_id, str(key)
                ),
            )
            rotate(
                "verifications",
                _AttributePayloadView(self._state.verifications, "evidence", skip_none=True),
                lambda key, _: associated_data("ih_verifications", key[0], key[1]),
            )
            rotate(
                "proxies",
                _AttributePayloadView(self._state.proxies, "secret"),
                lambda key, _: associated_data("ih_proxies", key),
            )
            rotate(
                "webhooks",
                _AttributePayloadView(self._state.webhook_endpoints, "secret"),
                lambda key, _: associated_data("ih_webhook_endpoints", key),
            )
            rotate(
                "notification_channels",
                _PayloadView(self._state.notification_channels, 1),
                lambda key, _: associated_data("ih_notification_channels", key),
            )
            rotate(
                "profiles",
                self._state.profile_secrets,
                lambda key, _: associated_data("ih_identities", key, "profile"),
            )
        return counts

    async def metrics_snapshot(self) -> MetricsSnapshot:
        async with self._lock:
            external_ids = [event.id for event, _, external in self._state.events if external]
            cursor_lag = {
                consumer: sum(event_id > cursor for event_id in external_ids)
                for consumer, cursor in self._state.cursors.items()
            }
            return MetricsSnapshot(
                cursor_lag,
                {
                    "webhooks": sum(
                        row.status in {"pending", "failed"} for row in self._state.webhook_deliveries.values()
                    ),
                    "notifications": sum(
                        row["status"] in {"pending", "failed"} for row in self._state.notifications.values()
                    ),
                },
            )


class _PayloadView:
    """Минимальное изменяемое отображение payload внутри tuple-записей для rotate_key."""

    def __init__(self, source: dict[Any, tuple[Any, ...]], index: int) -> None:
        self.source = source
        self.index = index

    def items(self) -> list[tuple[Any, EncryptedPayload]]:
        return [(key, row[self.index]) for key, row in self.source.items()]

    def __setitem__(self, key: Any, value: EncryptedPayload) -> None:
        row = list(self.source[key])
        row[self.index] = value
        self.source[key] = tuple(row)


class _AttributePayloadView:
    """Изменяемое представление dataclass-поля с зашифрованным payload."""

    def __init__(self, source: dict[Any, Any], attribute: str, *, skip_none: bool = False) -> None:
        self.source = source
        self.attribute = attribute
        self.skip_none = skip_none

    def items(self) -> list[tuple[Any, EncryptedPayload]]:
        rows = [(key, getattr(row, self.attribute)) for key, row in self.source.items()]
        return [(key, value) for key, value in rows if value is not None]

    def __setitem__(self, key: Any, value: EncryptedPayload) -> None:
        self.source[key] = replace(self.source[key], **{self.attribute: value})
