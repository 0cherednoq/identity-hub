"""Транзакционные назначения прокси. Владение сетью и бизнес-вердикт остаются у вызывающего."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID

from identity_hub._ids import new_id
from identity_hub.domain.idempotency import fingerprint
from identity_hub.enums import LeaseUnavailableReason
from identity_hub.errors import (
    ConcurrentModification,
    LeaseOwnershipLost,
    LeaseUnavailable,
    NotFound,
    ProxyAssignmentConflict,
    ValidationFailed,
)
from identity_hub.events import EventDraft
from identity_hub.store.base import LeaseFence, LeaseRow, ProxyAllocationRow, ProxyGroupRow, StoreTx

_COUNTER = {"bound": "bound", "reserved": "reserved", "draining": "draining"}
_TRANSITIONS = {
    "reserved": {"bound", "draining", "released"},
    "bound": {"draining", "released"},
    "draining": {"released"},
    "released": set(),
}


class ProxyCapacityChanged(LeaseUnavailable):
    """Внутренний сигнал bounded перевыбора после rollback, если snapshot proxy уже заполнен."""

    def __init__(self) -> None:
        super().__init__("proxy capacity changed during selection", reason=LeaseUnavailableReason.NO_PROXY)


async def require_legacy(tx: StoreTx, proxy_id: UUID) -> None:
    """Прямые старые команды не могут обойти ledger строгой группы."""
    row = await tx.get_proxy(proxy_id)
    if row is not None:
        group = await tx.proxy_group(row.pool)
        if group is not None and group.capacity_accounting == "allocations":
            raise ValidationFailed("managed proxy requires allocator")


async def validate_proxy_change(tx: StoreTx, proxy_id: UUID, values: dict[str, Any]) -> None:
    """Смена endpoint и группы сериализуется с назначением под proxy-lock.

    Изменение вместимости не может сделать занятые места невидимыми. Переход между
    режимами учёта выполняется только через configure_group с его барьером пространства.
    """
    row = await tx.get_proxy(proxy_id)
    if row is None:
        return
    source = await tx.proxy_group(row.pool)
    destination = await tx.proxy_group(values.get("pool", row.pool))
    source_mode = source.capacity_accounting if source else "bindings"
    target_mode = destination.capacity_accounting if destination else "bindings"
    if source_mode == target_mode == "bindings":
        return
    await tx.proxy_lock_rows([proxy_id])
    row = await tx.get_proxy(proxy_id)
    if row is None:
        raise NotFound("proxy", proxy_id)
    source = await tx.proxy_group(row.pool)
    source_mode = source.capacity_accounting if source else "bindings"
    if source_mode != target_mode:
        raise ValidationFailed("move across proxy accounting modes requires configure_group drain")
    allocations = await tx.proxy_allocations(proxy_id=proxy_id)
    if {"scheme", "host", "port", "credentials", "pool"} & values.keys():
        for allocation in allocations:
            if allocation.state != "bound" or await tx.proxy_allocation_in_use(allocation.id):
                raise ValidationFailed("managed proxy mutation requires drain")
    if "max_identities" in values and values["max_identities"] < (
        row.bound_count + row.reserved_count + row.draining_count
    ):
        raise ValidationFailed("proxy capacity cannot be lower than occupied slots")


async def _write(
    tx: StoreTx,
    row: ProxyAllocationRow,
    previous: ProxyAllocationRow | None,
    *,
    reason: str = "allocation_transition",
    lease_id: UUID | None = None,
    expired: bool = False,
    cleanup_delay: float | None = None,
) -> None:
    """Caller держит identity и все proxy операции; ledger и счётчики меняются вместе."""
    if previous is not None and previous.state != row.state and row.state not in _TRANSITIONS[previous.state]:
        raise ProxyAssignmentConflict("assignment_changed", row.id)
    proxy = await tx.get_proxy(row.proxy_id)
    if proxy is None:
        raise NotFound("proxy", row.proxy_id)
    counts = {"bound": proxy.bound_count, "reserved": proxy.reserved_count, "draining": proxy.draining_count}
    if previous is not None and previous.state in _COUNTER:
        counts[_COUNTER[previous.state]] -= 1
    if row.state in _COUNTER:
        counts[_COUNTER[row.state]] += 1
    if min(counts.values()) < 0:
        raise ValidationFailed("proxy allocation counters require reconciliation")
    before = proxy.bound_count + proxy.reserved_count + proxy.draining_count
    if sum(counts.values()) > before and sum(counts.values()) > proxy.max_identities:
        raise LeaseUnavailable("proxy has no available capacity", reason=LeaseUnavailableReason.NO_PROXY)
    await tx.proxy_store_allocation(row)
    await tx.proxy_set_counters(row.proxy_id, **counts)
    await _transition_events(tx, row, previous, proxy.pool, reason, lease_id, expired, cleanup_delay)


async def _transition_events(
    tx: StoreTx,
    row: ProxyAllocationRow,
    previous: ProxyAllocationRow | None,
    group: str,
    reason: str,
    lease_id: UUID | None,
    expired: bool,
    cleanup_delay: float | None,
) -> None:
    source = previous.state if previous else None
    if source == row.state:
        return
    types = []
    if source is None and row.state == "reserved":
        types.append("proxy.reservation.created")
    if source == "reserved":
        types.append("proxy.reservation.confirmed" if row.state == "bound" else "proxy.reservation.released")
    if row.state == "draining":
        types.append("proxy.allocation.draining")
    if row.state == "released":
        types.append("proxy.allocation.released")
    if not types:
        return
    identity = await tx.get_identity(row.identity_id)
    owner = lease_id or row.owner_lease_id or (previous.owner_lease_id if previous else None)
    if owner is None:
        lease = await tx.current_lease(row.identity_id)
        if lease is not None and lease.proxy_allocation_id == row.id:
            owner = lease.lease_id
    revision = (row.policy or {}).get("service_revision")
    was_bound = (
        row.state == "bound"
        or source == "bound"
        or (source == "draining" and previous is not None and previous.owner_lease_id is None)
    )
    data = {
        "allocation_id": str(row.id),
        "group": group,
        "binding_id": str(row.id) if was_bound else None,
        "from": source,
        "to": row.state,
        "reason": reason,
        "policy_revision": revision if type(revision) is int else None,
        "reservation_seconds": max(0.0, (row.updated_at - row.created_at).total_seconds())
        if source == "reserved"
        else None,
        "expired": expired,
        "cleanup_delay_seconds": cleanup_delay,
    }
    # Данные фиксированы здесь; outbox пишется после lease/history и откатывается с TX/savepoint.
    tx.defer_events(
        [
            EventDraft(
                event_type,
                actor="proxy_allocator",
                service=identity.service if identity else None,
                identity_id=row.identity_id,
                proxy_id=row.proxy_id,
                lease_id=owner,
                data=data,
            )
            for event_type in types
        ]
    )


def validate_group_configuration(name: str, accounting: str) -> None:
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 128
        or not isinstance(accounting, str)
        or accounting not in {"bindings", "allocations"}
    ):
        raise ValidationFailed("invalid proxy group configuration")


async def configure_group(tx: StoreTx, name: str, accounting: str) -> tuple[ProxyGroupRow, bool]:
    """Вызывается только под эксклюзивным барьером конфигурации пространства."""
    validate_group_configuration(name, accounting)
    current = await tx.proxy_group(name)
    if current is not None and current.capacity_accounting == accounting:
        return current, False
    if await tx.proxy_has_live_leases():
        raise ValidationFailed("proxy group configuration requires drain of space leases")
    proxies = await tx.proxy_group_proxies(name)
    bindings = await tx.proxy_bindings(group=name)
    await tx.lock_identities(sorted({row.identity_id for row in bindings}))
    await tx.proxy_lock_rows([row.id for row in proxies])
    for proxy in proxies:
        if await tx.proxy_allocations(proxy_id=proxy.id):
            raise ValidationFailed("drain active proxy allocations before switching accounting")
    counts = Counter(row.proxy_id for row in bindings)
    if accounting == "allocations":
        if any(counts[row.id] > row.max_identities for row in proxies):
            raise ValidationFailed("legacy bindings exceed physical proxy capacity")
        now = await tx.now()
        for binding in bindings:
            allocation = ProxyAllocationRow(
                new_id(), tx.space.id, binding.identity_id, binding.proxy_id, "bound", binding.bound_at, now
            )
            await tx.proxy_store_allocation(allocation)
            await tx.proxy_store_binding(binding.identity_id, allocation)
    for proxy in proxies:
        await tx.proxy_set_counters(proxy.id, bound=counts[proxy.id], reserved=0, draining=0)
    row = ProxyGroupRow(tx.space.id, name, accounting, (current.revision + 1) if current else 1)
    await tx.proxy_store_group(row)
    return row, True


async def reserve(
    tx: StoreTx,
    identity_id: UUID,
    group: str,
    *,
    owner_lease_id: UUID,
    max_identities: int | None = None,
    proxy_id: UUID | None = None,
    policy_fingerprint: str | None = None,
    policy: dict[str, object] | None = None,
) -> ProxyAllocationRow:
    if max_identities is not None and (
        not isinstance(max_identities, int) or isinstance(max_identities, bool) or max_identities < 1
    ):
        raise ValidationFailed("invalid proxy capacity limit")
    await tx.lock_identities([identity_id])
    if await tx.get_identity(identity_id) is None:
        raise NotFound("identity", identity_id)
    config = await tx.proxy_group(group)
    if config is None or config.capacity_accounting != "allocations":
        raise ValidationFailed("deferred assignment requires allocations accounting")
    active = await tx.proxy_allocations(identity_id=identity_id)
    for row in active:
        if row.state == "reserved" and row.owner_lease_id == owner_lease_id:
            proxy = await tx.get_proxy(row.proxy_id)
            if proxy is None or proxy.pool != group or row.policy_fingerprint != policy_fingerprint:
                raise ProxyAssignmentConflict("policy_changed", row.id)
            return row
    chosen = (
        await tx.get_proxy(proxy_id)
        if proxy_id is not None
        else await tx.choose_proxy(group, max_identities, claim=False)
    )
    if chosen is None:
        for stale in active:
            candidate = await tx.get_proxy(stale.proxy_id)
            if (
                candidate is not None
                and candidate.pool == group
                and candidate.issuable
                and (
                    stale.state == "bound"
                    or (
                        stale.state in {"reserved", "draining"}
                        and not await tx.proxy_allocation_in_use(stale.id)
                    )
                )
            ):
                chosen = candidate
                break
    if chosen is None or chosen.pool != group or not chosen.issuable:
        raise LeaseUnavailable("no available proxy", reason=LeaseUnavailableReason.NO_PROXY)
    bindings = await tx.proxy_bindings(identity_id=identity_id)
    await tx.proxy_lock_rows(
        sorted({chosen.id, *(row.proxy_id for row in active), *(row.proxy_id for row in bindings)})
    )
    for stale in active:
        if stale.owner_lease_id != owner_lease_id:
            await close_use(tx, stale.id)
    active = await tx.proxy_allocations(identity_id=identity_id)
    chosen = await tx.get_proxy(chosen.id)
    if chosen is None or chosen.pool != group or not chosen.issuable:
        raise LeaseUnavailable("proxy became unavailable", reason=LeaseUnavailableReason.NO_PROXY)
    if max_identities is not None and (
        isinstance(max_identities, bool) or max_identities < 1 or max_identities > chosen.max_identities
    ):
        raise ValidationFailed("proxy policy cannot exceed physical capacity")
    for row in active:
        if row.proxy_id == chosen.id:
            if row.state == "bound":
                return row
            raise ProxyAssignmentConflict("assignment_changed", row.id)
    capacity = chosen.max_identities if max_identities is None else max_identities
    if chosen.bound_count + chosen.reserved_count + chosen.draining_count >= capacity:
        raise ProxyCapacityChanged()
    now = await tx.now()
    binding = bindings[0] if bindings else None
    row = ProxyAllocationRow(
        new_id(),
        tx.space.id,
        identity_id,
        chosen.id,
        "reserved",
        now,
        now,
        owner_lease_id=owner_lease_id,
        expected_binding_id=binding.allocation_id if binding else None,
        expected_proxy_id=binding.proxy_id if binding else None,
        policy_fingerprint=policy_fingerprint,
        policy=policy,
    )
    await _write(tx, row, None, reason="reserved", lease_id=owner_lease_id)
    return row


async def attach_use(
    tx: StoreTx, fence: LeaseFence, allocation_id: UUID, *, request: dict[str, Any] | None = None
) -> ProxyAllocationRow:
    await tx.check_fence(fence)
    row = await tx.proxy_allocation(allocation_id)
    if row is None or row.identity_id != fence.identity_id:
        raise NotFound("proxy_allocation", allocation_id)
    await tx.proxy_lock_rows([row.proxy_id])
    if row.state not in {"reserved", "bound"} or (
        row.state == "reserved" and row.owner_lease_id != fence.lease_id
    ):
        raise ProxyAssignmentConflict("assignment_changed", row.id)
    lease = await tx.current_lease(fence.identity_id)
    if lease is None or lease.proxy_allocation_id not in {None, allocation_id}:
        raise ProxyAssignmentConflict("assignment_changed", row.id)
    await tx.proxy_attach_lease(fence, row, request=request)
    return row


async def confirm(
    tx: StoreTx,
    fence: LeaseFence,
    allocation_id: UUID,
    *,
    policy_fingerprint: str | None = None,
) -> tuple[ProxyAllocationRow, bool]:
    await tx.check_fence(fence)
    row = await tx.proxy_allocation(allocation_id)
    if row is None or row.identity_id != fence.identity_id:
        raise NotFound("proxy_allocation", allocation_id)
    bindings = await tx.proxy_bindings(identity_id=fence.identity_id)
    active = await tx.proxy_allocations(identity_id=fence.identity_id)
    await tx.proxy_lock_rows(
        sorted(
            {row.proxy_id, *(binding.proxy_id for binding in bindings), *(other.proxy_id for other in active)}
        )
    )
    lease = await tx.current_lease(fence.identity_id)
    if lease is None or lease.proxy_allocation_id != row.id:
        raise ProxyAssignmentConflict("assignment_changed", row.id)
    binding = bindings[0] if bindings else None
    if row.state == "bound" and binding is not None and binding.allocation_id == row.id:
        return row, False
    if row.state != "reserved" or row.owner_lease_id != fence.lease_id:
        current_pair = (binding.allocation_id, binding.proxy_id) if binding else (None, None)
        if current_pair != (row.expected_binding_id, row.expected_proxy_id):
            raise ProxyAssignmentConflict("assignment_changed", row.id)
        raise ProxyAssignmentConflict("not_reserved", row.id)
    if row.policy_fingerprint != policy_fingerprint:
        raise ProxyAssignmentConflict("policy_changed", row.id)
    current = (binding.allocation_id, binding.proxy_id) if binding else (None, None)
    if current != (row.expected_binding_id, row.expected_proxy_id):
        raise ProxyAssignmentConflict("assignment_changed", row.id)
    proxy = await tx.get_proxy(row.proxy_id)
    if proxy is None or not proxy.issuable:
        raise ProxyAssignmentConflict("proxy_unavailable", row.id)
    if binding is not None:
        if binding.allocation_id is None:
            # Cross-group legacy replacement uses the old binding counter once.
            await tx.proxy_store_binding(fence.identity_id, None)
            await tx.adjust_proxy_load(binding.proxy_id, -1)
        else:
            await detach(
                tx, fence.identity_id, expected_binding_id=binding.allocation_id, cancel_reservations=False
            )
    changed = replace(row, state="bound", owner_lease_id=None, updated_at=await tx.now())
    await _write(tx, changed, row, reason="confirmed", lease_id=fence.lease_id)
    await tx.proxy_store_binding(fence.identity_id, changed)
    return changed, True


async def detach(
    tx: StoreTx,
    identity_id: UUID,
    *,
    expected_binding_id: UUID | None = None,
    cancel_reservations: bool = True,
) -> ProxyAllocationRow | None:
    await tx.lock_identities([identity_id])
    bindings = await tx.proxy_bindings(identity_id=identity_id)
    binding = bindings[0] if bindings else None
    if expected_binding_id is not None:
        if binding is None:
            return None
        if binding.allocation_id != expected_binding_id:
            raise ProxyAssignmentConflict("assignment_changed", expected_binding_id)
    rows = await tx.proxy_allocations(identity_id=identity_id)
    await tx.proxy_lock_rows(sorted({*(row.proxy_id for row in rows), *(row.proxy_id for row in bindings)}))
    if binding is not None and binding.allocation_id is None:
        await tx.proxy_store_binding(identity_id, None)
        await tx.adjust_proxy_load(binding.proxy_id, -1)
    selected = [
        row
        for row in rows
        if (binding is not None and row.id == binding.allocation_id)
        or (cancel_reservations and row.state == "reserved")
    ]
    result = None
    for row in selected:
        if binding is not None and binding.allocation_id == row.id:
            await tx.proxy_store_binding(identity_id, None)
        used = await tx.proxy_allocation_in_use(row.id)
        now = await tx.now()
        changed = replace(
            row,
            state="draining" if used else "released",
            updated_at=now,
            ended_at=None if used else now,
            owner_lease_id=row.owner_lease_id if used else None,
        )
        await _write(tx, changed, row, reason="detached")
        result = result or changed
    return result


async def close_use(tx: StoreTx, allocation_id: UUID, *, finishing_lease_id: UUID | None = None) -> bool:
    row = await tx.proxy_allocation(allocation_id)
    if row is None:
        return False
    await tx.lock_identities([row.identity_id])
    await tx.proxy_lock_rows([row.proxy_id])
    row = await tx.proxy_allocation(allocation_id)
    if row is None or row.state not in {"reserved", "draining"}:
        return False
    if await tx.proxy_allocation_in_use(row.id, excluding_lease_id=finishing_lease_id):
        return False
    now = await tx.now()
    lease = await tx.proxy_use_lease(row)
    owned = lease is not None
    expired = owned and lease is not None and lease.expires_at <= now
    await _write(
        tx,
        replace(row, state="released", ended_at=now, updated_at=now, owner_lease_id=None),
        row,
        reason="expired" if expired else "use_closed" if owned else "orphaned",
        lease_id=lease.lease_id if owned and lease else row.owner_lease_id,
        expired=expired,
        cleanup_delay=max(0.0, (now - lease.expires_at).total_seconds()) if expired and lease else None,
    )
    return True


async def revise_policy(tx: StoreTx, allocation_id: UUID, policy: dict[str, Any]) -> bool:
    """Caller держит весь набор identity/proxy пачки; счётчики и поколение пары неизменны."""
    row = await tx.proxy_allocation(allocation_id)
    if row is None or row.state not in {"bound", "reserved"}:
        raise ProxyAssignmentConflict("assignment_changed", allocation_id)
    await tx.lock_identities([row.identity_id])
    await tx.proxy_lock_rows([row.proxy_id])
    row = await tx.proxy_allocation(allocation_id)
    if row is None or row.state not in {"bound", "reserved"}:
        raise ProxyAssignmentConflict("assignment_changed", allocation_id)
    if row.policy is not None and (
        type(row.policy.get("version")) is not int or row.policy.get("version") not in {1, 2}
    ):
        raise ProxyAssignmentConflict("policy_changed", allocation_id)
    previous = {key: value for key, value in (row.policy or {}).items() if key != "reconciliation_id"}
    if (
        previous == policy
        and row.policy is not None
        and row.policy_fingerprint == fingerprint("proxy_assignment", row.policy)
    ):
        return False
    updated = {**policy, "reconciliation_id": str(new_id())}
    await tx.proxy_store_allocation(
        replace(
            row,
            policy=updated,
            policy_fingerprint=fingerprint("proxy_assignment", updated),
            updated_at=await tx.now(),
            reason="policy_reconciled",
        )
    )
    return True


async def close_lease_use(tx: StoreTx, lease: LeaseRow, *, shared: bool) -> None:
    """Лишь полный fence разрешает исключить завершающего владельца из live use."""
    rows = await tx.proxy_allocations(identity_id=lease.identity_id)
    if not rows:
        return
    finishing = None
    try:
        await tx.check_fence(
            LeaseFence(lease.identity_id, lease.lease_id, lease.token, lease.fencing, shared)
        )
        finishing = lease.lease_id
    except LeaseOwnershipLost:
        pass  # Истёкшие назначения можно закрыть, но живого чужого владельца исключать нельзя.
    await lock_identity_proxy_rows(tx, [lease.identity_id])
    for row in await tx.proxy_allocations(identity_id=lease.identity_id):
        await close_use(tx, row.id, finishing_lease_id=finishing)


async def close_reclaimed_use(tx: StoreTx, old: LeaseRow) -> None:
    """Закрыть use до перезаписи lease/history, сохранив резерв будущей выдачи.

    Reserve мог уже создать нового владельца в этой транзакции. Его ещё нет в
    ih_leases, поэтому чистить все неиспользуемые назначения здесь нельзя.
    """
    await tx.lock_identities([old.identity_id])
    if old.expires_at > await tx.now():
        return
    rows = await tx.proxy_allocations(identity_id=old.identity_id)
    owned = [row for row in rows if row.id == old.proxy_allocation_id or row.owner_lease_id == old.lease_id]
    if owned:
        await lock_identity_proxy_rows(tx, [old.identity_id])
        for row in owned:
            await close_use(tx, row.id)


async def unbind_proxy(tx: StoreTx, proxy_id: UUID) -> list[UUID]:
    """Снять назначения ресурса; резерв замены не снимает старую пару на другом proxy."""
    await lock_proxy_batch(tx, [proxy_id])
    bindings = await tx.proxy_bindings(proxy_id=proxy_id)
    for binding in bindings:
        await detach(tx, binding.identity_id)
    for allocation in await tx.proxy_allocations(proxy_id=proxy_id):
        if allocation.state != "reserved":
            continue
        used = await tx.proxy_allocation_in_use(allocation.id)
        now = await tx.now()
        await _write(
            tx,
            replace(
                allocation,
                state="draining" if used else "released",
                updated_at=now,
                ended_at=None if used else now,
                owner_lease_id=allocation.owner_lease_id if used else None,
            ),
            allocation,
            reason="proxy_unbound",
        )
    return [row.identity_id for row in bindings]


async def lock_identity_proxy_rows(
    tx: StoreTx, identity_ids: Iterable[UUID], *, additional_proxy_ids: Iterable[UUID] = ()
) -> None:
    """Все identity, затем все proxy пачки: поэлементный порядок недостаточен."""
    identities = sorted(set(identity_ids))
    await tx.lock_identities(identities)
    proxy_ids = set(additional_proxy_ids)
    for identity_id in identities:
        proxy_ids.update(row.proxy_id for row in await tx.proxy_bindings(identity_id=identity_id))
        proxy_ids.update(row.proxy_id for row in await tx.proxy_allocations(identity_id=identity_id))
    await tx.proxy_lock_rows(sorted(proxy_ids))


async def lock_rebind_batch(tx: StoreTx, targets: Mapping[UUID, UUID]) -> None:
    await lock_identity_proxy_rows(tx, targets, additional_proxy_ids=targets.values())


async def lock_proxy_batch(tx: StoreTx, proxy_ids: Iterable[UUID]) -> None:
    targets = sorted(set(proxy_ids))
    identities: set[UUID] = set()
    for proxy_id in targets:
        identities.update(row.identity_id for row in await tx.proxy_bindings(proxy_id=proxy_id))
        identities.update(row.identity_id for row in await tx.proxy_allocations(proxy_id=proxy_id))
    await lock_identity_proxy_rows(tx, identities, additional_proxy_ids=targets)
    for proxy_id in targets:
        bindings = await tx.proxy_bindings(proxy_id=proxy_id)
        allocations = await tx.proxy_allocations(proxy_id=proxy_id)
        current = {row.identity_id for row in bindings} | {row.identity_id for row in allocations}
        if not current <= identities:
            # Нельзя захватывать новую identity после proxy; повтор команды увидит полный набор.
            raise ConcurrentModification("proxy", proxy_id)


async def prepare_identity_delete(
    tx: StoreTx, identity_id: UUID, *, finishing_lease_id: UUID | None = None
) -> None:
    """Назначения завершаются до revoke/history-lock в той же транзакции удаления."""
    await lock_identity_proxy_rows(tx, [identity_id])
    await detach(tx, identity_id)
    for row in await tx.proxy_allocations(identity_id=identity_id):
        await close_use(tx, row.id, finishing_lease_id=finishing_lease_id)
    if await tx.proxy_allocations(identity_id=identity_id):
        raise ValidationFailed("identity allocation still in use")


async def prepare_proxy_delete(tx: StoreTx, proxy_id: UUID, *, unbind: bool) -> list[UUID]:
    await lock_proxy_batch(tx, [proxy_id])
    for row in await tx.proxy_allocations(proxy_id=proxy_id):
        if await tx.proxy_allocation_in_use(row.id):
            raise ValidationFailed("proxy deletion requires drain of active uses")
    bindings = await tx.proxy_bindings(proxy_id=proxy_id)
    if bindings and not unbind:
        raise ValidationFailed("proxy has bindings; use unbind=True")
    result = await unbind_proxy(tx, proxy_id)
    for row in await tx.proxy_allocations(proxy_id=proxy_id):
        await close_use(tx, row.id)
    if await tx.proxy_allocations(proxy_id=proxy_id):
        raise ValidationFailed("proxy allocation still in use")
    return result


async def reconcile_counters(
    tx: StoreTx, proxy_id: UUID, *, fix: bool = False
) -> list[tuple[str, object, object]]:
    """COUNT читается после proxy-lock; согласование не захватывает identity или lease."""
    await tx.proxy_lock_rows([proxy_id])
    proxy = await tx.get_proxy(proxy_id)
    if proxy is None:
        return []
    group = await tx.proxy_group(proxy.pool)
    managed = group is not None and group.capacity_accounting == "allocations"
    bindings = await tx.proxy_bindings(proxy_id=proxy_id)
    allocations = await tx.proxy_allocations(proxy_id=proxy_id)
    issues: list[tuple[str, object, object]] = []
    consistent = True
    if managed:
        expected_bindings = {row.id for row in allocations if row.state == "bound"}
        actual_bindings = {row.allocation_id for row in bindings}
        if expected_bindings != actual_bindings:
            issues.append(
                (
                    "proxy.allocations.bindings",
                    sorted(map(str, expected_bindings)),
                    sorted(map(str, actual_bindings)),
                )
            )
            consistent = False  # Нельзя освобождать место при повреждённом отношении.
        counts = Counter(row.state for row in allocations)
    else:
        counts = Counter({"bound": len(bindings)})
        if allocations:
            issues.append(("proxy.allocations.accounting", "allocations", "bindings"))
            consistent = False
    expected = {state: counts[state] for state in _COUNTER}
    actual = {"bound": proxy.bound_count, "reserved": proxy.reserved_count, "draining": proxy.draining_count}
    for state, value in expected.items():
        if actual[state] != value:
            issues.append((f"proxy.{state}_count", value, actual[state]))
    if fix and consistent and expected != actual:
        await tx.proxy_set_counters(proxy_id, **expected)
    return issues


async def rebind(
    tx: StoreTx,
    identity_id: UUID,
    proxy_id: UUID,
    *,
    expected_binding_id: UUID | None = None,
    source: str = "administrative_rebind",
) -> tuple[UUID | None, UUID]:
    """Явное доверенное назначение администратора; не подтверждение проверки аккаунта.

    Старый используемый proxy переходит в draining. Активный резерв той же пары нельзя
    присвоить без fence его владельца: администратор сначала завершает его использование.
    """
    await tx.lock_identities([identity_id])
    if await tx.get_identity(identity_id) is None:
        raise NotFound("identity", identity_id)
    bindings = await tx.proxy_bindings(identity_id=identity_id)
    active = await tx.proxy_allocations(identity_id=identity_id)
    await tx.proxy_lock_rows(
        sorted({proxy_id, *(row.proxy_id for row in bindings), *(row.proxy_id for row in active)})
    )
    target = await tx.get_proxy(proxy_id)
    if target is None:
        raise NotFound("proxy", proxy_id)
    if target.status != "alive":
        raise ValidationFailed("target proxy must be alive")
    if not target.enabled:
        raise ValidationFailed("target proxy is disabled")
    binding = bindings[0] if bindings else None
    if expected_binding_id is not None and (binding is None or binding.allocation_id != expected_binding_id):
        raise ProxyAssignmentConflict("assignment_changed", expected_binding_id)
    previous = binding.proxy_id if binding else None
    if previous == proxy_id:
        return previous, proxy_id
    group = await tx.proxy_group(target.pool)
    managed = group is not None and group.capacity_accounting == "allocations"
    if managed:
        for row in active:
            if row.proxy_id == proxy_id:
                raise ProxyAssignmentConflict("assignment_changed", row.id)
        if target.bound_count + target.reserved_count + target.draining_count >= target.max_identities:
            raise LeaseUnavailable(
                "target proxy has no available capacity", reason=LeaseUnavailableReason.NO_PROXY
            )
    # Все возможные proxy уже заблокированы, повторные блокировки не меняют порядок.
    await detach(tx, identity_id)
    if managed:
        now = await tx.now()
        allocation = ProxyAllocationRow(
            new_id(), tx.space.id, identity_id, proxy_id, "bound", now, now, reason=source
        )
        await _write(tx, allocation, None)
        await tx.proxy_store_binding(identity_id, allocation)
    elif await tx.rebind_proxy(identity_id, proxy_id) is None:
        raise LeaseUnavailable(
            "target proxy has no available capacity", reason=LeaseUnavailableReason.NO_PROXY
        )
    return previous, proxy_id
