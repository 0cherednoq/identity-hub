"""Hourly current-state snapshots with explicit historical coverage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from identity_hub.errors import ValidationFailed

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

__all__ = ["StateSnapshotFrame", "StateSnapshotRow", "StateSnapshots", "snapshot_hour"]


@dataclass(frozen=True, slots=True)
class StateSnapshotRow:
    space_id: UUID
    service: str
    region: str
    state: str
    taken_at: datetime
    count: int


@dataclass(frozen=True, slots=True)
class StateSnapshotFrame:
    taken_at: datetime
    captured_at: datetime
    source: str
    complete: bool
    rows: tuple[StateSnapshotRow, ...]


def snapshot_hour(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValidationFailed("snapshot time must include timezone")
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


class StateSnapshots:
    def __init__(self, hub: IdentityHub) -> None:
        self.hub = hub
        self._last_hour: datetime | None = None

    async def capture(self, *, at: datetime | None = None, source: str = "scheduled") -> int:
        taken_at = snapshot_hour(at or await self.hub._store.current_time())
        return await self.hub._store.capture_state_snapshot(taken_at, source=source)

    async def backfill_current(self, *, at: datetime | None = None) -> int:
        """Capture only the current hour; never synthesize earlier history."""
        return await self.capture(at=at, source="backfill")

    async def frames(self, space_slug: str, *, start: datetime, end: datetime) -> list[StateSnapshotFrame]:
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValidationFailed("snapshot range must be aware and non-empty")
        return await self.hub._store.read_state_snapshots(space_slug, start, end)

    async def purge(self, *, before: datetime | None = None) -> int:
        cutoff = before or (await self.hub._store.current_time() - self.hub.retention.state_snapshots)
        return await self.hub._store.purge_state_snapshots(cutoff)

    async def run_due(self) -> int:
        hour = snapshot_hour(await self.hub._store.current_time())
        if self._last_hour == hour:
            return 0
        captured = await self.capture(at=hour)
        purged = await self.purge()
        self._last_hour = hour
        return captured + purged
