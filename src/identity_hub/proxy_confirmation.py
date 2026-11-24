"""Подтверждение под владельцем: политика и принятые факты, затем переход allocator."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.errors import (
    IdempotencyConflict,
    NotFound,
    OperationNotAllowed,
    ProxyAssignmentConflict,
    ValidationFailed,
)
from identity_hub.events import EventDraft
from identity_hub.pool import Pool
from identity_hub.proxy_allocator import confirm
from identity_hub.proxy_assignment import StatePredicate, predicate_regions
from identity_hub.proxy_issuance import assignment_fingerprint, validate_replay, validate_subject
from identity_hub.state import COLUMN_REGIONS, _initial
from identity_hub.store.base import IdentityRow, LeaseFence, ProxyAllocationRow, StoreTx


async def predicate_facts(
    tx: StoreTx, identity: IdentityRow, *rules: StatePredicate | None
) -> dict[tuple[str, str], str]:
    """Те же canonical defaults, что у машин состояний; без применения новых timer/observations."""
    facts: dict[tuple[str, str], str] = {}
    for rule in rules:
        if rule is None:
            continue
        for leaf in predicate_regions(rule):
            pair = (leaf.region, leaf.key)
            if pair in facts:
                continue
            if leaf.region in COLUMN_REGIONS:
                facts[pair] = getattr(identity, COLUMN_REGIONS[leaf.region][0])
            else:
                state = await tx.load_region(identity.id, leaf.region, leaf.key)
                facts[pair] = (state or _initial(leaf.region, leaf.key, identity)).state
    return facts


async def confirm_assignment(
    space: SpaceBase,
    tx: StoreTx,
    pool: Pool[Any, Any],
    fence: LeaseFence,
    allocation_id: UUID,
    *,
    actor: str,
    reason: str,
) -> ProxyAllocationRow:
    """Вызывающий владеет commit; precondition failure не меняет назначение."""
    await tx.check_fence(fence)
    current = await tx.current_lease(fence.identity_id)
    if fence.shared or current is None or current.proxy_allocation_id != allocation_id:
        raise ProxyAssignmentConflict("assignment_changed", allocation_id)
    identity = await tx.get_identity(fence.identity_id)
    assert identity is not None
    try:
        service = await space.hub.resolve_service(identity.service)
        fresh = pool._for_service(service)
    except (NotFound, OperationNotAllowed, ValidationFailed):
        raise ProxyAssignmentConflict("policy_changed", allocation_id) from None
    policy = fresh.proxy_policy
    if current.proxy_request is None or policy is None:
        raise ProxyAssignmentConflict("policy_changed", allocation_id)
    primary = await tx.proxy_group(policy.group)
    try:
        await validate_replay(tx, fresh, primary, current.proxy_request)
    except IdempotencyConflict:
        raise ProxyAssignmentConflict("policy_changed", allocation_id) from None
    allocation = await tx.proxy_allocation(allocation_id)
    if allocation is None or not validate_subject(current.proxy_request, allocation):
        raise ProxyAssignmentConflict("policy_changed", allocation_id)
    if not policy.sticky:
        raise ProxyAssignmentConflict("not_reserved", allocation_id)
    if policy.binding is not None:
        binding = policy.binding
        facts = await predicate_facts(tx, identity, binding.bind_when, binding.unbind_when)
        if (binding.unbind_when is not None and binding.unbind_when.matches(facts)) or (
            binding.bind_when is not None and not binding.bind_when.matches(facts)
        ):
            raise ProxyAssignmentConflict("condition_not_met", allocation_id)
    previous = await tx.proxy_bindings(identity_id=fence.identity_id)
    row, changed = await confirm(
        tx, fence, allocation_id, policy_fingerprint=assignment_fingerprint(current.proxy_request)
    )
    if changed:
        proxy = await tx.get_proxy(row.proxy_id)
        assert proxy is not None
        await space._emit(
            tx,
            EventDraft(
                "proxy.binding.changed",
                actor=actor,
                service=identity.service,
                identity_id=identity.id,
                proxy_id=row.proxy_id,
                lease_id=fence.lease_id,
                data={
                    "from_proxy_id": str(previous[0].proxy_id) if previous else None,
                    "to_proxy_id": str(row.proxy_id),
                    "allocation_id": str(row.id),
                    "reason": reason,
                    "pool": proxy.pool,
                },
            ),
        )
    return row
