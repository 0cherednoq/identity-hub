"""Short-lived server-side bulk selections and retry progress."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from identity_hub.read.search import AccountSelectionEntry
from litestar.exceptions import HTTPException


@dataclass(slots=True)
class BulkSnapshot:
    id: UUID
    space_slug: str
    actor: str
    operation: str
    parameters: dict[str, Any]
    filter_fingerprint: str
    entries: tuple[AccountSelectionEntry, ...]
    truncated: bool
    created_at: datetime
    expires_at: datetime
    succeeded: set[UUID] = field(default_factory=set)
    failures: dict[UUID, tuple[str, str]] = field(default_factory=dict)
    attempts: int = 0
    execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class BulkCoordinator:
    """Own bounded selections until durable admin tables arrive in A4.1."""

    def __init__(self, *, ttl: timedelta = timedelta(minutes=15), max_entries: int = 1000) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._items: dict[UUID, BulkSnapshot] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        space_slug: str,
        actor: str,
        operation: str,
        parameters: dict[str, Any],
        filter_fingerprint: str,
        entries: tuple[AccountSelectionEntry, ...],
        truncated: bool,
    ) -> BulkSnapshot:
        now = datetime.now(UTC)
        item = BulkSnapshot(
            id=uuid4(),
            space_slug=space_slug,
            actor=actor,
            operation=operation,
            parameters=parameters,
            filter_fingerprint=filter_fingerprint,
            entries=entries,
            truncated=truncated,
            created_at=now,
            expires_at=now + self.ttl,
        )
        async with self._lock:
            self._prune(now)
            self._items[item.id] = item
        return item

    async def get(self, snapshot_id: UUID, *, space_slug: str, actor: str) -> BulkSnapshot:
        now = datetime.now(UTC)
        async with self._lock:
            self._prune(now)
            item = self._items.get(snapshot_id)
            if item is None or item.space_slug != space_slug or item.actor != actor:
                raise HTTPException("bulk selection not found", status_code=404)
            return item

    async def update(
        self,
        item: BulkSnapshot,
        *,
        succeeded: set[UUID],
        failures: dict[UUID, tuple[str, str]],
    ) -> None:
        async with self._lock:
            item.succeeded.update(succeeded)
            item.failures = failures
            item.attempts += 1

    def _prune(self, now: datetime) -> None:
        expired = [key for key, item in self._items.items() if item.expires_at <= now]
        for key in expired:
            self._items.pop(key, None)
