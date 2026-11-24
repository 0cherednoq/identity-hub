"""Общая финализация принятой команды: итоговые факты, поколение и решение назначения."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from identity_hub.domain.regions import Outcome, RegionDecision
from identity_hub.errors import (
    IdempotencyConflict,
    NotFound,
    OperationNotAllowed,
    ProxyAssignmentConflict,
    ValidationFailed,
)
from identity_hub.events import EventDraft
from identity_hub.metrics import record_metric
from identity_hub.proxy_allocator import detach, lock_identity_proxy_rows
from identity_hub.proxy_assignment import (
    AllOf,
    AnyOf,
    ProxyBindingPolicy,
    RegionFacts,
    RegionIn,
    StatePredicate,
)
from identity_hub.proxy_confirmation import confirm_assignment, predicate_facts
from identity_hub.proxy_issuance import validate_replay, validate_subject

if TYPE_CHECKING:
    from identity_hub._space import SpaceBase
    from identity_hub.lease import Lease
    from identity_hub.store.base import ProxyAllocationRow, StoreTx


def accepted(decision: RegionDecision | None) -> bool:
    """Новое принятое наблюдение включает подтверждение прежнего состояния, но не pin/stale."""
    return decision is not None and decision.outcome in {Outcome.APPLIED, Outcome.UNCHANGED}


def projection_proof(region: str, key: str, decision: RegionDecision | None) -> dict[tuple[str, str], str]:
    """Принятый факт этой команды, включая UNCHANGED; pin/stale не дают доказательства."""
    if not accepted(decision) or decision is None:
        return {}
    return {(region, key): decision.state.state}


def _positive_proof(rule: StatePredicate, facts: RegionFacts, proof: RegionFacts) -> bool:
    """Удовлетворённая ветка должна содержать новый принятый факт; остальные поля — предусловия."""
    if not rule.matches(facts):
        return False
    if isinstance(rule, RegionIn):
        key = (rule.region, rule.key)
        return key in proof and proof[key] == facts.get(key)
    if isinstance(rule, AllOf | AnyOf):
        return any(_positive_proof(child, facts, proof) for child in rule.rules)
    return False


@dataclass(frozen=True, slots=True)
class ProxyFinalization:
    """Безопасный исход сохраняется рядом с verdict; конфликт поколения не отменяет verdict."""

    outcome: str
    allocation_id: UUID | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "allocation_id": str(self.allocation_id) if self.allocation_id is not None else None,
        }


def with_assignment(
    result: dict[str, Any] | None, assignment: ProxyFinalization | None
) -> dict[str, Any] | None:
    return result if assignment is None else {**(result or {}), "proxy_assignment": assignment.to_dict()}


async def prepare(tx: StoreTx, identity_id: UUID) -> None:
    """До provider operation/history: весь набор proxy команды, включая резерв замены."""
    await lock_identity_proxy_rows(tx, [identity_id])


def _binding(row: ProxyAllocationRow, fallback: ProxyBindingPolicy | None) -> ProxyBindingPolicy | None:
    if row.policy is None:  # Явная доверенная admin/import-привязка наследует правила сервиса.
        return fallback
    if type(row.policy.get("version")) is not int or row.policy.get("version") not in {1, 2}:
        raise ProxyAssignmentConflict("policy_changed", row.id)
    policy = row.policy.get("policy")
    if policy is None and "policy" in row.policy:
        return None
    if not isinstance(policy, dict):
        raise ProxyAssignmentConflict("policy_changed", row.id)
    try:
        return ProxyBindingPolicy.from_dict(policy["binding"]) if policy.get("binding") is not None else None
    except (ValueError, TypeError, KeyError):
        raise ProxyAssignmentConflict("policy_changed", row.id) from None


async def finalize(
    space: SpaceBase,
    tx: StoreTx,
    identity_id: UUID,
    *,
    actor: str,
    reason: str,
    lease: Lease[Any, Any] | None = None,
    state_accepted: bool = True,
    allow_bind: bool = False,
    proof: RegionFacts | None = None,
) -> ProxyFinalization | None:
    """Вызывать один раз после всей проекции, до принятия claim и commit.

    prepare выполнен до записи проекции. Отказ fence/claim отменяет всю транзакцию;
    конфликт назначения возвращается значением и сохраняется вместе с verdict.
    Без нового принятого результата и его владельца автоматического bind нет.
    """
    if lease is not None:
        await lease._check_fencing(tx)
    rows = await tx.proxy_allocations(identity_id=identity_id)
    if not rows:
        return None
    if lease is not None and lease.proxy_allocation_id is None:
        # Lease без выданного назначения не несёт snapshot поколения. Его поздний
        # verdict не даёт права снимать новую административную пару.
        return ProxyFinalization("not_reserved")
    if not state_accepted:
        return ProxyFinalization("not_accepted", lease.proxy_allocation_id if lease else None)
    identity = await tx.get_identity(identity_id)
    assert identity is not None
    try:
        service = await space.hub.resolve_service(identity.service)
    except (NotFound, OperationNotAllowed, ValidationFailed):
        return ProxyFinalization("policy_changed", rows[0].id)
    fallback = service.proxy_policy.binding if service.proxy_policy else None
    bindings = await tx.proxy_bindings(identity_id=identity_id)
    binding = bindings[0] if bindings else None
    allocation_id = lease.proxy_allocation_id if lease else (binding.allocation_id if binding else None)
    try:
        use_rule = None
        use_reserved = False
        if lease is not None and allocation_id is not None:
            row = await tx.proxy_allocation(allocation_id)
            if row is None or row.identity_id != identity_id or row.state not in {"reserved", "bound"}:
                raise ProxyAssignmentConflict("assignment_changed", allocation_id)
            use_reserved = row.state == "reserved"
            pair = (binding.allocation_id, binding.proxy_id) if binding else (None, None)
            expected = (
                (row.id, row.proxy_id)
                if row.state == "bound"
                else (row.expected_binding_id, row.expected_proxy_id)
            )
            if pair != expected:
                raise ProxyAssignmentConflict("assignment_changed", allocation_id)
            # Проверка свежей композиции до любого отрицательного действия тоже обязательна.
            try:
                fresh = lease.pool._for_service(service)
                current = await tx.current_lease(identity_id)
                policy = fresh.proxy_policy
                if current is None or current.proxy_allocation_id != allocation_id:
                    raise ProxyAssignmentConflict("assignment_changed", allocation_id)
                if current.proxy_request is None or policy is None:
                    raise ProxyAssignmentConflict("policy_changed", allocation_id)
                await validate_replay(tx, fresh, await tx.proxy_group(policy.group), current.proxy_request)
                if not validate_subject(current.proxy_request, row):
                    raise ProxyAssignmentConflict("policy_changed", allocation_id)
                use_rule = policy.binding
            except (IdempotencyConflict, NotFound, OperationNotAllowed, ValidationFailed):
                raise ProxyAssignmentConflict("policy_changed", allocation_id) from None
        rules = [use_rule] if use_rule is not None else []
        for row in rows:
            if row.state == "draining":
                continue  # Политика уже снятого поколения не управляет новой парой.
            if row.state == "bound" and (binding is None or row.id != binding.allocation_id):
                raise ProxyAssignmentConflict("assignment_changed", row.id)
            if row.policy is not None and row.policy.get(
                "service_revision"
            ) != space.hub._runtime_service_revisions.get(identity.service):
                raise ProxyAssignmentConflict("policy_changed", row.id)
            rule = _binding(row, fallback)
            if rule is not None:
                rules.append(rule)
        facts = await predicate_facts(tx, identity, *(rule.unbind_when for rule in rules))
        if any(rule.unbind_when is not None and rule.unbind_when.matches(facts) for rule in rules):
            await detach(tx, identity_id, expected_binding_id=binding.allocation_id if binding else None)
            if binding is not None:
                proxy = await tx.get_proxy(binding.proxy_id)
                assert proxy is not None
                await space._emit(
                    tx,
                    EventDraft(
                        "proxy.binding.changed",
                        actor=actor,
                        service=identity.service,
                        identity_id=identity_id,
                        proxy_id=binding.proxy_id,
                        lease_id=lease.lease_id if lease else None,
                        data={
                            "from_proxy_id": str(binding.proxy_id),
                            "to_proxy_id": None,
                            "allocation_id": str(binding.allocation_id) if binding.allocation_id else None,
                            "reason": reason,
                            "pool": proxy.pool,
                        },
                    ),
                )
            return ProxyFinalization("unbound", allocation_id)
        if (
            lease is not None
            and allocation_id is not None
            and allow_bind
            and use_rule is not None
            and use_rule.bind_when is not None
        ):
            positive_facts = await predicate_facts(tx, identity, use_rule.bind_when)
            if (
                use_reserved
                and use_rule.bind_when.matches(positive_facts)
                and not _positive_proof(use_rule.bind_when, positive_facts, proof or {})
            ):
                return ProxyFinalization("not_accepted", allocation_id)
            await confirm_assignment(
                space, tx, lease.pool, lease.fence, allocation_id, actor=actor, reason=reason
            )
            return ProxyFinalization("bound", allocation_id)
        return ProxyFinalization("unchanged", allocation_id)
    except ProxyAssignmentConflict as exc:
        record_metric(
            space.hub.metrics,
            "counter",
            "identity_hub_failures_total",
            space=space.slug,
            service=identity.service,
            scope="proxy_finalization",
            reason=exc.reason,
        )
        return ProxyFinalization(exc.reason, exc.allocation_id)
