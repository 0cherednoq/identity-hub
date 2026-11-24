"""Выдача proxy под lease: общий выбор для legacy и строгого учёта."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from identity_hub.domain.idempotency import LEASE_FAMILY, fingerprint
from identity_hub.enums import LeaseUnavailableReason
from identity_hub.errors import IdempotencyConflict, LeaseUnavailable, ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.pool import Pool
from identity_hub.proxy_allocator import (
    ProxyCapacityChanged,
    close_use,
    detach,
    lock_identity_proxy_rows,
    reserve,
)
from identity_hub.service import ProxyDownPolicy
from identity_hub.store.base import IdentityRow, ProxyAllocationRow, ProxyGroupRow, ProxyRow, StoreTx

_SUBJECT_FINGERPRINT = "allocation_policy_fingerprint"


def assignment_fingerprint(request: dict[str, Any]) -> str:
    """Отпечаток декларации не включает снимок самого ledger (нет рекурсивного hash)."""
    return fingerprint(
        "proxy_assignment", {key: value for key, value in request.items() if key != _SUBJECT_FINGERPRINT}
    )


def validate_subject(request: dict[str, Any], allocation: ProxyAllocationRow) -> bool:
    """Снимок use не получает право на политику, записанную после выдачи."""
    if allocation.policy is None:
        consistent = allocation.policy_fingerprint is None
    else:
        consistent = (
            type(allocation.policy.get("version")) is int
            and allocation.policy.get("version") in {1, 2}
            and allocation.policy_fingerprint == assignment_fingerprint(allocation.policy)
        )
    return (
        consistent
        and type(request.get("version")) is int
        and request.get("version") == 2
        and _SUBJECT_FINGERPRINT in request
        and request[_SUBJECT_FINGERPRINT] == allocation.policy_fingerprint
    )


class ProxyCandidateBlocked(Exception):
    """Нынешняя привязка запрещает выдачу аккаунта."""


@dataclass
class ProxyIssuance:
    proxy: ProxyRow | None = None
    allocation: ProxyAllocationRow | None = None
    request: dict[str, Any] | None = None
    drafts: list[EventDraft] = field(default_factory=list)


def managed(group: ProxyGroupRow | None) -> bool:
    return group is not None and group.capacity_accounting == "allocations"


def _group(group: ProxyGroupRow | None, name: str) -> dict[str, Any]:
    return {
        "name": name,
        "capacity_accounting": group.capacity_accounting if group else "bindings",
        "revision": group.revision if group else None,
    }


def request_base(pool: Pool[Any, Any], primary: ProxyGroupRow | None) -> dict[str, Any]:
    policy = pool.proxy_policy
    return {
        "version": 2,
        "service": pool.service.name,
        "service_revision": pool._service_revision,
        "lease_mode": str(pool.service.lease_mode),
        "policy": None
        if policy is None
        else {
            "group": policy.group,
            "sticky": policy.sticky,
            "max_identities": policy.max_identities,
            "on_proxy_down": str(policy.on_proxy_down),
            "binding": policy.binding.to_dict() if policy.binding else None,
        },
        "primary_group": _group(primary, policy.group) if policy else None,
    }


def request_snapshot(
    pool: Pool[Any, Any], primary: ProxyGroupRow | None, actual: ProxyGroupRow
) -> dict[str, Any]:
    return {**request_base(pool, primary), "actual_group": _group(actual, actual.name)}


async def validate_replay(
    tx: StoreTx, pool: Pool[Any, Any], primary: ProxyGroupRow | None, request: dict[str, Any]
) -> None:
    actual = request.get("actual_group")
    name = actual.get("name") if isinstance(actual, dict) else None
    if not isinstance(name, str):
        raise IdempotencyConflict(LEASE_FAMILY)
    current = await tx.proxy_group(name)
    subject = request.get(_SUBJECT_FINGERPRINT)
    base = {key: value for key, value in request.items() if key != _SUBJECT_FINGERPRINT}
    if (
        current is None
        or type(request.get("version")) is not int
        or request.get("version") != 2
        or _SUBJECT_FINGERPRINT not in request
        or (
            subject is not None
            and (
                not isinstance(subject, str)
                or len(subject) != 64
                or any(c not in "0123456789abcdef" for c in subject)
            )
        )
        or base != request_snapshot(pool, primary, current)
    ):
        raise IdempotencyConflict(LEASE_FAMILY)


async def issue(
    tx: StoreTx, pool: Pool[Any, Any], candidate: IdentityRow, lease_id: UUID, primary: ProxyGroupRow | None
) -> ProxyIssuance:
    policy = pool.proxy_policy
    if policy is None:
        return ProxyIssuance()
    current = await tx.get_binding(candidate.id) if policy.sticky else None
    current_group = await tx.proxy_group(current.pool) if current else None
    if (
        current is not None
        and not current.issuable
        and (not current.enabled or policy.on_proxy_down is ProxyDownPolicy.WAIT)
    ):
        raise ProxyCandidateBlocked
    reuse = current is not None and current.issuable
    # Opt-in строгой группы переносит прежнюю legacy-пару через резерв, не присваивая ей новый режим.
    if reuse and managed(primary) and not managed(current_group):
        reuse = False
    actual = current_group if reuse else primary
    if managed(actual):
        if pool.shared:
            raise ValidationFailed("allocations accounting requires exclusive leases")
        assert actual is not None
        request = request_snapshot(pool, primary, actual)
        allocation = await reserve(
            tx,
            candidate.id,
            actual.name,
            owner_lease_id=lease_id,
            proxy_id=current.id if reuse and current else None,
            max_identities=policy.max_identities,
            policy_fingerprint=assignment_fingerprint(request),
            policy=request,
        )
        use_request = {**request, _SUBJECT_FINGERPRINT: allocation.policy_fingerprint}
        return ProxyIssuance(await tx.get_proxy(allocation.proxy_id), allocation, use_request)
    chosen = current if reuse else await tx.choose_proxy(policy.group, policy.max_identities, claim=False)
    if chosen is None:
        raise LeaseUnavailable("no available proxy", reason=LeaseUnavailableReason.NO_PROXY)
    await lock_identity_proxy_rows(tx, [candidate.id], additional_proxy_ids=[chosen.id])
    for row in await tx.proxy_allocations(identity_id=candidate.id):
        await close_use(tx, row.id)
    chosen = await tx.get_proxy(chosen.id)
    if chosen is None or not chosen.issuable:
        raise LeaseUnavailable("proxy became unavailable", reason=LeaseUnavailableReason.NO_PROXY)
    if reuse:
        return ProxyIssuance(chosen)
    drafts: list[EventDraft] = []
    if policy.sticky:
        if current is not None and managed(current_group):
            await detach(tx, candidate.id, cancel_reservations=False)
        claimed = await tx.choose_proxy(policy.group, policy.max_identities, claim=True, proxy_id=chosen.id)
        if claimed is None:
            raise ProxyCapacityChanged()
        await tx.bind_proxy(
            candidate.id, chosen.id, current.id if current and not managed(current_group) else None
        )
        drafts.append(
            EventDraft(
                "proxy.binding.changed",
                actor=pool.holder,
                service=pool.service.name,
                identity_id=candidate.id,
                proxy_id=chosen.id,
                data={
                    "from_proxy_id": str(current.id) if current else None,
                    "to_proxy_id": str(chosen.id),
                    "reason": "rebind" if current else "initial",
                    "pool": chosen.pool,
                },
            )
        )
    return ProxyIssuance(chosen, drafts=drafts)
