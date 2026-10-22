"""Жизненный цикл записи credentials (IMP-09, D-215): `active ↔ disabled`, `revoked` конечное."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from identity_hub.errors import TransitionNotAllowed

__all__ = [
    "ACTIVE",
    "DISABLED",
    "LIFECYCLES",
    "REVOKED",
    "SESSION_MODES",
    "CredentialInput",
    "check_transition",
    "is_expired",
    "validity_code",
]


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


_VALIDITY_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")


def validity_code(code: str) -> str:
    """Код отказа ручной пометки (C8.1): формат как у `LoginFailure.code`."""
    from identity_hub.errors import ValidationFailed

    value = str(code)
    if _VALIDITY_CODE.fullmatch(value) is None:
        raise ValidationFailed("code должен соответствовать [a-z][a-z0-9_.-]{0,63}")
    return value


SESSION_MODES = frozenset({"in_place", "replace"})


def session_mode(mode: str) -> str:
    """Режим записи сессии (IMP-10, D-219): `in_place` или `replace`; иначе `ValidationFailed`."""
    from identity_hub.errors import ValidationFailed

    if mode not in SESSION_MODES:
        raise ValidationFailed(f"режим записи сессии: in_place или replace, получено {mode!r}")
    return mode


def session_event_data(slot: str, mode: str, written: Any) -> dict[str, object]:
    """Поля события о записи сессии без её данных: слот, режим, id записей и ревизия."""
    replaced = getattr(written, "replaced_id", None)
    return {
        "slot": slot,
        "mode": mode,
        "session_id": str(written.id),
        "replaced_id": str(replaced) if replaced is not None else None,
        "session_revision": written.revision,
    }
