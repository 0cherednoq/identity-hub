"""Логи с контекстом аренды (ТЗ FR-12).

Внутри `pool.lease()` в contextvars лежит `LeaseContext`; процессор structlog и фильтр stdlib
`logging` добавляют его поля (`lease_id`, `identity_id`, `service`, `pool`, `holder`) в каждую запись.
Опциональный sink `LeaseLogSink` пишет важные записи в `ih_lease_logs` пачками на heartbeat и release.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from identity_hub.context import LeaseContext, current_lease
from identity_hub.domain.redaction import redact_inline_secrets
from identity_hub.events.catalog import scrub

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
    max_field_chars: int = 1000  # длина одного строкового поля `extra`
    max_entry_bytes: int = 8192  # размер JSON всех полей записи
    # буфер аренды без heartbeat и release (pool.acquire без release) сбрасывается через это время
    buffer_ttl: timedelta = timedelta(hours=1)


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

    def __init__(
        self, policy: LeaseLogPolicy | None = None, *, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self.policy = policy or LeaseLogPolicy()
        super().__init__(level=self.policy.min_level)
        self._buffers: dict[UUID, list[LeaseLogEntry]] = defaultdict(list)
        self._counts: dict[UUID, int] = defaultdict(int)
        self._touched: dict[UUID, float] = {}
        self._monotonic = monotonic
        self._purged_at = monotonic()

    def emit(self, record: logging.LogRecord) -> None:
        ctx: LeaseContext | None = current_lease()
        if ctx is None:
            return
        try:
            now = self._monotonic()
            self._purge(now)
            self._touched[ctx.lease_id] = now
            if self._counts[ctx.lease_id] >= self.policy.max_per_lease:
                return
            self._counts[ctx.lease_id] += 1
            extra = {
                k: v
                for k, v in vars(record).items()
                if k not in _RESERVED and k not in LEASE_FIELDS and not k.startswith("_")
            }
            self._buffers[ctx.lease_id].append(
                LeaseLogEntry(
                    lease_id=ctx.lease_id,
                    identity_id=ctx.identity_id,
                    ts=datetime.fromtimestamp(record.created, UTC),
                    level=record.levelno,
                    message=redact_inline_secrets(record.getMessage()[:4000]),
                    fields=self._fields(extra),
                )
            )
        except Exception:  # sink никогда не ломает код пользователя
            self.handleError(record)

    def _fields(self, extra: dict[str, Any]) -> dict[str, Any]:
        """Поля `extra` для ih_lease_logs: без секретов (как данные событий) и ограниченного размера."""
        limit = self.policy.max_field_chars
        fields: dict[str, Any] = {}
        size = 2
        for key, value in scrub(extra).items():
            clipped = _clip(value, limit)
            cost = len(json.dumps({key: clipped}, ensure_ascii=False, default=str).encode()) + 1
            if size + cost > self.policy.max_entry_bytes - len(_TRUNCATED):
                fields["_truncated"] = True
                break
            if clipped != value:
                fields["_truncated"] = True
            fields[key] = clipped
            size += cost
        return fields

    def _purge(self, now: float) -> None:
        """Сбросить буферы аренд, которые давно не писали и не были освобождены (R2-S11)."""
        ttl = self.policy.buffer_ttl.total_seconds()
        if now - self._purged_at < min(ttl, 60.0):
            return
        self._purged_at = now
        for lease_id in [key for key, touched in self._touched.items() if now - touched >= ttl]:
            self.discard(lease_id)

    def discard(self, lease_id: UUID) -> None:
        """Забыть аренду: буфер и счётчик (аренда потеряна или истекла без `release`)."""
        self._buffers.pop(lease_id, None)
        self._counts.pop(lease_id, None)
        self._touched.pop(lease_id, None)

    def drain(self, lease_id: UUID, *, final: bool = False) -> list[LeaseLogEntry]:
        entries = self._buffers.pop(lease_id, [])
        if final:
            self._counts.pop(lease_id, None)
            self._touched.pop(lease_id, None)
        return entries


_TRUNCATED = b',"_truncated":true'


def _clip(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict | list):
        text = json.dumps(value, ensure_ascii=False, default=str)
        return value if len(text) <= limit else text[:limit]
    return value
