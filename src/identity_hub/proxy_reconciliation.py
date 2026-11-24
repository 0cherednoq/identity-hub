"""Явная смена правил существующих назначений без нового положительного verdict."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from identity_hub.errors import ProxyAssignmentConflict, ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.proxy_allocator import revise_policy
from identity_hub.proxy_finalization import finalize
from identity_hub.proxy_issuance import request_base, request_snapshot

if TYPE_CHECKING:
    from identity_hub._space import SpaceBase
    from identity_hub.pool import Pool
    from identity_hub.store.base import StoreTx


async def reconcile(
    space: SpaceBase,
    tx: StoreTx,
    identity_id: UUID,
    pool: Pool[Any, Any],
    *,
    actor: str,
    reason: str,
    expected_binding_id: UUID | None = None,
) -> None:
    """Все блокировки пачки взяты до первого вызова. Один savepoint на identity."""
    identity = await space._identity(tx, identity_id)
    if identity.service != pool.service.name:
        raise ValidationFailed("proxy policy reconciliation requires the selected service")
    bindings = await tx.proxy_bindings(identity_id=identity_id)
    binding = bindings[0] if bindings else None
    rows = [row for row in await tx.proxy_allocations(identity_id=identity_id) if row.state != "draining"]
    if expected_binding_id is not None and (binding is None or binding.allocation_id != expected_binding_id):
        raise ProxyAssignmentConflict("assignment_changed", expected_binding_id)
    if binding is not None and binding.allocation_id is None:
        raise ValidationFailed("proxy policy reconciliation requires allocations accounting")
    if not rows:
        return
    subject = binding.allocation_id if binding else rows[0].id
    assert subject is not None
    current_service = await space.hub.resolve_service(identity.service)
    fresh = pool._for_service(current_service)
    primary = await tx.proxy_group(fresh.proxy_policy.group) if fresh.proxy_policy else None
    if request_base(fresh, primary) != request_base(pool, primary):
        raise ProxyAssignmentConflict("policy_changed", subject)
    if (
        fresh.proxy_policy is not None
        and fresh.proxy_policy.binding is not None
        and (primary is None or primary.capacity_accounting != "allocations")
    ):
        raise ValidationFailed("proxy binding policy requires allocations accounting")
    changed = []
    for row in rows:
        if row.state == "bound" and (binding is None or binding.allocation_id != row.id):
            raise ProxyAssignmentConflict("assignment_changed", row.id)
        proxy = await tx.get_proxy(row.proxy_id)
        assert proxy is not None
        group = await tx.proxy_group(proxy.pool)
        if group is None or group.capacity_accounting != "allocations":
            raise ValidationFailed("proxy policy reconciliation requires allocations accounting")
        if await revise_policy(tx, row.id, request_snapshot(fresh, primary, group)):
            changed.append(row.id)
    outcome = await finalize(space, tx, identity_id, actor=actor, reason=reason)
    if outcome is not None and outcome.outcome in {"assignment_changed", "policy_changed"}:
        raise ProxyAssignmentConflict(outcome.outcome, outcome.allocation_id or subject)
    if changed or (outcome is not None and outcome.outcome == "unbound"):
        await space._emit(
            tx,
            EventDraft(
                "proxy.binding.policy_reconciled",
                actor=actor,
                service=identity.service,
                identity_id=identity_id,
                proxy_id=binding.proxy_id if binding else rows[0].proxy_id,
                data={
                    "allocation_ids": [str(row.id) for row in rows],
                    "binding_id": str(binding.allocation_id) if binding else None,
                    "service_revision": fresh._service_revision,
                    "reason": reason,
                    "outcome": outcome.outcome if outcome is not None else "unchanged",
                },
            ),
        )
