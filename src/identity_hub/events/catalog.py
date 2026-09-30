"""Каталог событий v1 (ТЗ FR-13.2, FR-13.3), маски типов и очистка секретов (S-8)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, SecretBytes, SecretStr

from identity_hub.enums import Severity

__all__ = [
    "CATALOG",
    "Event",
    "EventDraft",
    "EventSpec",
    "SecretLeak",
    "matches",
    "matches_any",
    "scrub",
    "severity_at_least",
    "spec_of",
]


@dataclass(frozen=True, slots=True)
class EventSpec:
    type: str
    group: str
    external: bool = True  # False — только журнал: не уходит диспетчеру (lease.*, operation.*)
    severity: Severity = Severity.INFO


def _specs() -> dict[str, EventSpec]:
    groups: dict[str, list[tuple[str, Severity]]] = {
        "identity": [
            ("identity.registered", Severity.INFO),
            ("identity.imported", Severity.INFO),
            ("identity.updated", Severity.INFO),
            ("identity.lifecycle.changed", Severity.INFO),
            ("identity.deleted", Severity.WARNING),
        ],
        "regions": [
            ("identity.standing.changed", Severity.INFO),
            ("identity.auth.changed", Severity.INFO),
            ("identity.verification.changed", Severity.INFO),
            ("identity.tier.reached", Severity.INFO),
            ("identity.tier.lost", Severity.WARNING),
            ("identity.requirement.added", Severity.WARNING),
            ("identity.requirement.resolved", Severity.INFO),
            ("identity.availability.changed", Severity.INFO),
            ("identity.state.overridden", Severity.WARNING),
            ("identity.state.unpinned", Severity.WARNING),
            ("identity.state.drift", Severity.WARNING),
        ],
        "login": [
            ("identity.login.succeeded", Severity.INFO),
            ("identity.login.failed", Severity.WARNING),
            ("identity.challenge.opened", Severity.WARNING),
            ("identity.challenge.answered", Severity.INFO),
            ("identity.challenge.expired", Severity.WARNING),
        ],
        "secrets": [
            ("identity.credentials.updated", Severity.INFO),
            ("identity.session.updated", Severity.INFO),
            ("identity.secret.revealed", Severity.WARNING),
            ("identity.linked", Severity.INFO),
            ("identity.unlinked", Severity.INFO),
        ],
        "proxy": [
            ("proxy.status.changed", Severity.INFO),
            ("proxy.binding.changed", Severity.INFO),
            ("proxy.pool.exhausted", Severity.CRITICAL),
        ],
        "pool": [
            ("pool.capacity.low", Severity.WARNING),
            ("pool.capacity.empty", Severity.CRITICAL),
            ("identity.quota.exhausted", Severity.INFO),
            ("worker.leases_lost", Severity.WARNING),
        ],
        "delivery": [("webhook.endpoint.disabled", Severity.CRITICAL)],
        "admin": [
            ("admin.bulk.executed", Severity.INFO),
            ("admin.accounts.exported", Severity.INFO),
            ("admin.events.exported", Severity.INFO),
            ("admin.integration.changed", Severity.INFO),
            ("admin.import.changed", Severity.INFO),
        ],
    }
    journal = [
        "lease.acquired",
        "lease.released",
        "lease.failed",
        "lease.lost",
        "lease.expired",
        "lease.revoked",
        "operation.succeeded",
        "operation.failed",
        "session.saved",
    ]
    specs = {t: EventSpec(t, group, True, sev) for group, items in groups.items() for t, sev in items}
    specs |= {t: EventSpec(t, "journal", external=False) for t in journal}
    return specs


CATALOG: Mapping[str, EventSpec] = _specs()


def spec_of(event_type: str) -> EventSpec:
    try:
        return CATALOG[event_type]
    except KeyError:
        raise ValueError(f"тип события {event_type!r} не входит в каталог") from None


# ---------------------------------------------------------------- маски и severity

_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


def severity_at_least(severity: str, minimum: str) -> bool:
    return _SEVERITY_RANK[Severity(severity)] >= _SEVERITY_RANK[Severity(minimum)]


def matches(pattern: str, event_type: str) -> bool:
    """`pool.capacity.*` — все типы с префиксом; `*` — все; иначе точное совпадение (FR-14.1)."""
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return event_type.startswith(pattern[:-1])
    return pattern == event_type


def matches_any(patterns: Iterable[str], event_type: str) -> bool:
    return any(matches(p, event_type) for p in patterns)


# ---------------------------------------------------------------- очистка секретов

_SECRET_KEY = re.compile(r"(token|password|passwd|secret|auth_key|cookie|credential|evidence|totp)", re.I)
REDACTED = "[redacted]"


class SecretLeak(ValueError):
    """В данные события попал секрет (строгий режим — тесты и разработка)."""


def scrub(data: Mapping[str, Any], *, strict: bool = False) -> dict[str, Any]:
    """Привести `data` к JSON без секретов (S-8): SecretStr, bytes и «секретные» ключи вырезаются."""
    return {str(k): _scrub_value(str(k), v, strict) for k, v in data.items()}


def _scrub_value(key: str, value: Any, strict: bool) -> Any:
    if _SECRET_KEY.search(key) and value is not None:
        return _leak(f"ключ {key!r}", strict)
    if isinstance(value, SecretStr | SecretBytes | bytes | bytearray | memoryview):
        return _leak(f"значение {key!r} типа {type(value).__name__}", strict)
    if isinstance(value, BaseModel):
        return _leak(f"модель {type(value).__name__} в {key!r}", strict)
    if isinstance(value, Mapping):
        return {str(k): _scrub_value(str(k), v, strict) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        items = sorted(value, key=str) if isinstance(value, set | frozenset) else value
        return [_scrub_value(key, v, strict) for v in items]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _leak(what: str, strict: bool) -> str:
    if strict:
        raise SecretLeak(f"секрет в данных события: {what}")
    return REDACTED


# ---------------------------------------------------------------- события


@dataclass(frozen=True, slots=True)
class EventDraft:
    """Событие до записи в ih_events: пространство, sequence и время проставляет хранилище."""

    type: str
    actor: str
    data: Mapping[str, Any] = field(default_factory=dict)
    severity: Severity | None = None  # None — из каталога
    service: str | None = None
    identity_id: UUID | None = None
    proxy_id: UUID | None = None
    lease_id: UUID | None = None

    def __post_init__(self) -> None:
        spec_of(self.type)

    @property
    def spec(self) -> EventSpec:
        return CATALOG[self.type]

    @property
    def effective_severity(self) -> Severity:
        return self.severity or self.spec.severity


@dataclass(frozen=True, slots=True)
class Event:
    """Событие из журнала (FR-13.2) — то, что получают потребители."""

    id: int
    event_id: UUID
    type: str
    severity: Severity
    occurred_at: datetime
    space: str
    service: str | None
    identity_id: UUID | None
    proxy_id: UUID | None
    lease_id: UUID | None
    sequence: int | None
    actor: str
    data: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Тело вебхука и payload колбэков (без внутреннего `id`)."""
        return {
            "event_id": str(self.event_id),
            "type": self.type,
            "severity": str(self.severity),
            "occurred_at": self.occurred_at.isoformat(),
            "space": self.space,
            "service": self.service,
            "identity_id": str(self.identity_id) if self.identity_id else None,
            "proxy_id": str(self.proxy_id) if self.proxy_id else None,
            "lease_id": str(self.lease_id) if self.lease_id else None,
            "sequence": self.sequence,
            "actor": self.actor,
            "data": dict(self.data),
        }
