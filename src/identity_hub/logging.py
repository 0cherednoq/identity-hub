"""Логи с контекстом аренды (ТЗ FR-12).

Внутри `pool.lease()` в contextvars лежит `LeaseContext`; процессор structlog и фильтр stdlib
`logging` добавляют его поля (`lease_id`, `identity_id`, `service`, `pool`, `holder`) в каждую запись.
Опциональный sink `LeaseLogSink` пишет важные записи в `ih_lease_logs` пачками на heartbeat и release.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from identity_hub.context import LeaseContext, current_lease

__all__ = [
    "LEASE_FIELDS",
    "LeaseContextFilter",
    "LeaseLogPolicy",
    "LeaseLogSink",
    "structlog_processor",
]

LEASE_FIELDS = ("lease_id", "identity_id", "service", "pool", "holder")
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def structlog_processor(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """`structlog.configure(processors=[..., structlog_processor, ...])` — поля аренды в каждое событие."""
    ctx = current_lease()
    if ctx is not None:
        for key, value in ctx.as_dict().items():
            event_dict.setdefault(key, value)
    return event_dict


class LeaseContextFilter(logging.Filter):
    """Фильтр stdlib logging: атрибуты записи `lease_id`, `identity_id`, … (None вне аренды)."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = current_lease()
        values = ctx.as_dict() if ctx is not None else dict.fromkeys(LEASE_FIELDS)
        for key, value in values.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


@dataclass(frozen=True)
class LeaseLogPolicy:
    """Sink в ih_lease_logs (FR-12.2)."""

    min_level: int = logging.WARNING
    max_per_lease: int = 200


@dataclass(frozen=True, slots=True)
class LeaseLogEntry:
    lease_id: UUID
    identity_id: UUID
    ts: datetime
    level: int
    message: str
    fields: dict[str, Any]


class LeaseLogSink(logging.Handler):
    """Handler, копящий записи аренды в памяти до heartbeat/release (FR-12.2).

    Подключается пользователем: `logging.getLogger().addHandler(hub.lease_log_sink)`.
    Ошибка записи sink'а не влияет на работу аренды.
    """

    def __init__(self, policy: LeaseLogPolicy | None = None) -> None:
        self.policy = policy or LeaseLogPolicy()
        super().__init__(level=self.policy.min_level)
        self._buffers: dict[UUID, list[LeaseLogEntry]] = defaultdict(list)
        self._counts: dict[UUID, int] = defaultdict(int)

    def emit(self, record: logging.LogRecord) -> None:
        ctx: LeaseContext | None = current_lease()
        if ctx is None:
            return
        try:
            if self._counts[ctx.lease_id] >= self.policy.max_per_lease:
                return
            self._counts[ctx.lease_id] += 1
            extra = {
                k: _jsonable(v)
                for k, v in vars(record).items()
                if k not in _RESERVED and k not in LEASE_FIELDS and not k.startswith("_")
            }
            self._buffers[ctx.lease_id].append(
                LeaseLogEntry(
                    lease_id=ctx.lease_id,
                    identity_id=ctx.identity_id,
                    ts=datetime.fromtimestamp(record.created, UTC),
                    level=record.levelno,
                    message=record.getMessage()[:4000],
                    fields=extra,
                )
            )
        except Exception:  # sink никогда не ломает код пользователя
            self.handleError(record)

    def drain(self, lease_id: UUID, *, final: bool = False) -> list[LeaseLogEntry]:
        entries = self._buffers.pop(lease_id, [])
        if final:
            self._counts.pop(lease_id, None)
        return entries


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)
