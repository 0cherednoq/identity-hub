"""Импорт аккаунтов (FR-2.4–2.7). Запись импорта; сам `import_many` — задача P5.7."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

__all__ = ["ImportRecord"]


@dataclass(slots=True)
class ImportRecord:
    """Одна запись импорта. Данные валидируются схемами сервиса до обращения к БД.

    `links` — роль → external_ref связанного аккаунта (в том числе из той же пачки).
    `sessions` — слот → данные сессии; `proxy` — URL прокси (дедупликация по fingerprint).
    `verifications` — фактор → начальное состояние (например, `"verified"`).
    """

    external_ref: str
    id: UUID | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    profile: Mapping[str, Any] = field(default_factory=dict)
    labels: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    tags: frozenset[str] = frozenset()
    note: str | None = None
    credentials: Mapping[str, Any] | None = None
    sessions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    links: Mapping[str, str] = field(default_factory=dict)
    proxy: str | None = None
    verifications: Mapping[str, str] = field(default_factory=dict)
