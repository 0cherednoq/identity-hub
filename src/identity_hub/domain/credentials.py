"""Жизненный цикл записи credentials (IMP-09, D-215): `active ↔ disabled`, `revoked` конечное."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from identity_hub.errors import TransitionNotAllowed

__all__ = ["ACTIVE", "DISABLED", "LIFECYCLES", "REVOKED", "CredentialInput", "check_transition", "is_expired"]


@dataclass(frozen=True)
class CredentialInput:
    """Запись credentials при создании и импорте (IMP-09, D-217); значение в repr не попадает."""

    kind: str
    value: BaseModel | Mapping[str, Any] = field(repr=False)
    name: str = "default"
    public_value: Mapping[str, Any] | None = None
    expires_at: datetime | None = None


ACTIVE, DISABLED, REVOKED = "active", "disabled", "revoked"
LIFECYCLES = frozenset({ACTIVE, DISABLED, REVOKED})
_ALLOWED = {
    (ACTIVE, DISABLED),
    (DISABLED, ACTIVE),
    (ACTIVE, REVOKED),
    (DISABLED, REVOKED),
}


def check_transition(current: str, target: str) -> bool:
    """`True` — переход нужен; то же состояние — `False`; запрещённый — `TransitionNotAllowed`."""
    if current == target:
        return False
    if (current, target) not in _ALLOWED:
        raise TransitionNotAllowed("credentials", current, target, "revoked — конечное состояние")
    return True


def is_expired(lifecycle: str, expires_at: datetime | None, now: datetime) -> bool:
    """Срок считается по времени, не переходом (D-215)."""
    return lifecycle == ACTIVE and expires_at is not None and expires_at <= now
