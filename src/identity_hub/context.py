"""Контекст аренды в contextvars (ТЗ FR-12.1): логи внутри `lease()` получают его автоматически."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

__all__ = ["LeaseContext", "current_lease"]


@dataclass(frozen=True, slots=True)
class LeaseContext:
    lease_id: UUID
    identity_id: UUID
    service: str
    pool: str
    holder: str

    def as_dict(self) -> dict[str, str]:
        return {
            "lease_id": str(self.lease_id),
            "identity_id": str(self.identity_id),
            "service": self.service,
            "pool": self.pool,
            "holder": self.holder,
        }


_current: ContextVar[LeaseContext | None] = ContextVar("identity_hub_lease", default=None)


def current_lease() -> LeaseContext | None:
    """Контекст аренды текущей задачи или None вне `lease()`."""
    return _current.get()
