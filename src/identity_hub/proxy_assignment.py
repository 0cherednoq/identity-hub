"""Декларативные правила назначения прокси, без I/O и понятия валидности аккаунта."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from identity_hub.domain.region_specs import REGIONS
from identity_hub.domain.verification import VERIFICATION

if TYPE_CHECKING:
    from identity_hub.service import ProxyPolicy
    from identity_hub.store.base import ProxyAllocationRow

__all__ = ["AllOf", "AnyOf", "ProxyAssignment", "ProxyBindingPolicy", "RegionIn"]


@dataclass(frozen=True, slots=True)
class ProxyAssignment:
    """Результат назначения без сетевых секретов и lease token."""

    allocation_id: UUID
    identity_id: UUID
    proxy_id: UUID
    state: str
    updated_at: datetime

    @classmethod
    def from_row(cls, row: ProxyAllocationRow) -> ProxyAssignment:
        return cls(row.id, row.identity_id, row.proxy_id, row.state, row.updated_at)


_MAX_DEPTH = 8
_MAX_NODES = 64
type RegionFacts = Mapping[tuple[str, str], str]


@dataclass(frozen=True, slots=True, init=False)
class RegionIn:
    """Состояние объявленного региона входит в набор; отсутствующий факт даёт False."""

    region: str
    key: str
    states: frozenset[str]

    def __init__(self, region: str, *, states: Iterable[str], key: str | None = None) -> None:
        spec = VERIFICATION if region == "verification" else REGIONS.get(region)
        if spec is None:
            raise ValueError("proxy predicate: неизвестный регион")
        if key is not None and not isinstance(key, str):
            raise ValueError("proxy predicate: ключ региона должен быть строкой")
        if isinstance(states, str | bytes):
            raise ValueError("proxy predicate: states должен быть набором состояний")
        values = frozenset(states)
        if not values or not values <= spec.states:
            raise ValueError("proxy predicate: пустой набор или неизвестное состояние")
        normalized_key = (key or "default") if region == "auth" else (key or "")
        if not isinstance(normalized_key, str) or len(normalized_key) > 64:
            raise ValueError("proxy predicate: некорректный ключ региона")
        if region == "verification" and not normalized_key:
            raise ValueError("proxy predicate: требуется ключ фактора")
        if region not in {"auth", "verification"} and normalized_key:
            raise ValueError("proxy predicate: регион не имеет ключа")
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "key", normalized_key)
        object.__setattr__(self, "states", values)

    def matches(self, facts: RegionFacts) -> bool:
        return facts.get((self.region, self.key)) in self.states

    def to_dict(self) -> dict[str, Any]:
        return {"type": "region", "region": self.region, "key": self.key, "states": sorted(self.states)}


@dataclass(frozen=True, slots=True, init=False)
class AllOf:
    """Все условия выполнены. Пустая композиция и произвольные callbacks запрещены."""

    rules: tuple[StatePredicate, ...]

    def __init__(self, *rules: StatePredicate) -> None:
        _validate_rules(rules)
        object.__setattr__(self, "rules", rules)

    def matches(self, facts: RegionFacts) -> bool:
        return all(rule.matches(facts) for rule in self.rules)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "all", "rules": [rule.to_dict() for rule in self.rules]}


@dataclass(frozen=True, slots=True, init=False)
class AnyOf:
    """Хотя бы одно условие выполнено; неизвестные факты не считаются успехом."""

    rules: tuple[StatePredicate, ...]

    def __init__(self, *rules: StatePredicate) -> None:
        _validate_rules(rules)
        object.__setattr__(self, "rules", rules)

    def matches(self, facts: RegionFacts) -> bool:
        return any(rule.matches(facts) for rule in self.rules)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "any", "rules": [rule.to_dict() for rule in self.rules]}


type StatePredicate = RegionIn | AllOf | AnyOf


def _validate_rules(rules: tuple[StatePredicate, ...]) -> None:
    if not rules:
        raise ValueError("proxy predicate: требуется хотя бы одно условие")
    pending: list[tuple[object, int]] = [(rule, 2) for rule in rules]
    count = 1
    while pending:
        rule, depth = pending.pop()
        count += 1
        if type(rule) not in (RegionIn, AllOf, AnyOf) or depth > _MAX_DEPTH or count > _MAX_NODES:
            raise ValueError("proxy predicate: неизвестное или слишком сложное условие")
        if isinstance(rule, AllOf | AnyOf):
            pending.extend((child, depth + 1) for child in rule.rules)


def predicate_from_dict(value: Mapping[str, Any]) -> StatePredicate:
    """Строгое чтение JSON-предиката; неизвестные поля и типы не исполняются."""
    pending: list[tuple[object, int]] = [(value, 1)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if not isinstance(node, Mapping) or depth > _MAX_DEPTH or count > _MAX_NODES:
            raise ValueError("proxy predicate: некорректное или слишком сложное описание")
        if node.get("type") in ("all", "any"):
            rules = node.get("rules")
            if not isinstance(rules, list) or not 1 <= len(rules) <= _MAX_NODES:
                raise ValueError("proxy predicate: некорректная композиция")
            pending.extend((child, depth + 1) for child in rules)
    return _predicate_from_dict(value)


def _predicate_from_dict(value: Mapping[str, Any], *, _depth: int = 1) -> StatePredicate:
    if not isinstance(value, Mapping) or _depth > _MAX_DEPTH:
        raise ValueError("proxy predicate: некорректное или слишком глубокое описание")
    kind = value.get("type")
    if kind == "region":
        if set(value) - {"type", "region", "key", "states"}:
            raise ValueError("proxy predicate: неизвестные поля")
        region, states, key = value.get("region"), value.get("states"), value.get("key")
        if (
            not isinstance(region, str)
            or not isinstance(states, list)
            or not all(isinstance(state, str) for state in states)
            or (key is not None and not isinstance(key, str))
        ):
            raise ValueError("proxy predicate: некорректные поля региона")
        return RegionIn(region, states=states, key=key)
    if kind in ("all", "any"):
        rules = value.get("rules")
        if set(value) != {"type", "rules"} or not isinstance(rules, list) or len(rules) > _MAX_NODES:
            raise ValueError("proxy predicate: некорректная композиция")
        children = tuple(_predicate_from_dict(rule, _depth=_depth + 1) for rule in rules)
        return AllOf(*children) if kind == "all" else AnyOf(*children)
    raise ValueError("proxy predicate: неизвестный тип условия")


def predicate_regions(rule: StatePredicate) -> Iterable[RegionIn]:
    if isinstance(rule, RegionIn):
        yield rule
    else:
        for child in rule.rules:
            yield from predicate_regions(child)


@dataclass(frozen=True, slots=True)
class ProxyBindingPolicy:
    """Подтверждение и снятие привязки являются независимыми решениями."""

    mode: Literal["immediate", "deferred"] = "deferred"
    bind_when: StatePredicate | None = None
    unbind_when: StatePredicate | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("immediate", "deferred"):
            raise ValueError("proxy binding: неизвестный режим")
        for rule in (self.bind_when, self.unbind_when):
            if rule is not None and type(rule) not in (RegionIn, AllOf, AnyOf):
                raise ValueError("proxy binding: требуется декларативное условие")
        if self.mode == "immediate" and self.bind_when is not None:
            raise ValueError("proxy binding: bind_when требует deferred")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "bind_when": self.bind_when.to_dict() if self.bind_when is not None else None,
            "unbind_when": self.unbind_when.to_dict() if self.unbind_when is not None else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProxyBindingPolicy:
        if not isinstance(value, Mapping) or set(value) != {"mode", "bind_when", "unbind_when"}:
            raise ValueError("proxy binding: некорректное описание")
        mode = value["mode"]
        if mode not in ("immediate", "deferred"):
            raise ValueError("proxy binding: неизвестный режим")
        return cls(
            mode=mode,
            bind_when=predicate_from_dict(value["bind_when"]) if value["bind_when"] is not None else None,
            unbind_when=predicate_from_dict(value["unbind_when"])
            if value["unbind_when"] is not None
            else None,
        )


def compose_proxy_policy(base: ProxyPolicy | None, override: ProxyPolicy | None) -> ProxyPolicy | None:
    """Пул может сузить подтверждение, но не отключить правила сервиса."""
    if override is None:
        return base
    if base is None or base.binding is None:
        return override
    if not override.sticky:
        raise ValueError("proxy policy: пул не может отключить привязку сервиса")
    if override.binding is None:
        return replace(override, binding=base.binding)
    parent, child = base.binding, override.binding
    if parent.mode == "deferred" and child.mode != "deferred":
        raise ValueError("proxy policy: пул не может ослабить deferred-политику сервиса")
    bind_rules = tuple(rule for rule in (parent.bind_when, child.bind_when) if rule is not None)
    unbind_rules = tuple(rule for rule in (parent.unbind_when, child.unbind_when) if rule is not None)
    return replace(
        override,
        binding=ProxyBindingPolicy(
            mode=child.mode,
            bind_when=AllOf(*bind_rules) if len(bind_rules) > 1 else (bind_rules[0] if bind_rules else None),
            unbind_when=AnyOf(*unbind_rules)
            if len(unbind_rules) > 1
            else (unbind_rules[0] if unbind_rules else None),
        ),
    )
