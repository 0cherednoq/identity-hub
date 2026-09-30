"""Secret-free read contracts for durable admin jobs and provider operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.api._base import legacy
from identity_hub.errors import ValidationFailed

__all__ = ["AdminStateMixin", "ImportRunInfo", "ProviderOperationInfo"]

_PROVIDER_STATES = frozenset({"pending", "running", "retryable", "succeeded", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class ImportRunInfo:
    id: UUID
    service: str
    actor: str
    source: str
    state: str
    on_existing: str
    verify_sessions: bool
    labels: dict[str, object]
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    cancel_requested_at: datetime | None
    total: int
    processed: int
    created: int
    updated: int
    skipped: int
    failed: int
    report: dict[str, object]


@dataclass(frozen=True, slots=True)
class ProviderOperationInfo:
    id: UUID
    identity_id: UUID
    kind: str
    state: str
    attempt: int
    claimed_by: str | None
    claim_expires_at: datetime | None
    available_at: datetime
    deadline_at: datetime | None
    result: dict[str, object]
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


def _limit(value: int) -> int:
    if not 1 <= value <= 500:
        raise ValidationFailed("limit must be between 1 and 500")
    return value


class AdminStateMixin(SpaceBase):
    async def _import_runs(self, *, limit: int = 100) -> tuple[ImportRunInfo, ...]:
        """Return newest durable import runs without staged input or secrets."""
        async with self._tx() as tx:
            rows = await tx.list_import_runs(limit=_limit(limit))
        return tuple(
            ImportRunInfo(
                id=row.id,
                service=row.service,
                actor=row.actor,
                source=row.source,
                state=row.state,
                on_existing=row.on_existing,
                verify_sessions=row.verify_sessions,
                labels=dict(row.labels),
                started_at=row.started_at,
                updated_at=row.updated_at,
                finished_at=row.finished_at,
                cancel_requested_at=row.cancel_requested_at,
                total=row.total,
                processed=row.processed,
                created=row.created,
                updated=row.updated,
                skipped=row.skipped,
                failed=row.failed,
                report=dict(row.report),
            )
            for row in rows
        )

    async def _import_run(self, run_id: UUID) -> ImportRunInfo | None:
        """Return one durable import run, scoped to this space."""
        async with self._tx() as tx:
            row = await tx.get_import_run(run_id)
        if row is None:
            return None
        return ImportRunInfo(
            id=row.id,
            service=row.service,
            actor=row.actor,
            source=row.source,
            state=row.state,
            on_existing=row.on_existing,
            verify_sessions=row.verify_sessions,
            labels=dict(row.labels),
            started_at=row.started_at,
            updated_at=row.updated_at,
            finished_at=row.finished_at,
            cancel_requested_at=row.cancel_requested_at,
            total=row.total,
            processed=row.processed,
            created=row.created,
            updated=row.updated,
            skipped=row.skipped,
            failed=row.failed,
            report=dict(row.report),
        )

    import_run = legacy(_import_run, "import_run")

    async def _provider_operations(
        self,
        *,
        identity_id: UUID | None = None,
        states: frozenset[str] | None = None,
        limit: int = 100,
    ) -> tuple[ProviderOperationInfo, ...]:
        """Return durable claim/call/apply state without adapter payload secrets."""
        unknown = states - _PROVIDER_STATES if states is not None else frozenset()
        if unknown:
            raise ValidationFailed(f"unknown provider operation states: {', '.join(sorted(unknown))}")
        async with self._tx() as tx:
            rows = await tx.list_provider_operations(
                identity_id=identity_id,
                states=states,
                limit=_limit(limit),
            )
        return tuple(
            ProviderOperationInfo(
                id=row.id,
                identity_id=row.identity_id,
                kind=row.kind,
                state=row.state,
                attempt=row.attempt,
                claimed_by=row.claimed_by,
                claim_expires_at=row.claim_expires_at,
                available_at=row.available_at,
                deadline_at=row.deadline_at,
                result=dict(row.result),
                error_code=row.error_code,
                created_at=row.created_at,
                updated_at=row.updated_at,
                finished_at=row.finished_at,
            )
            for row in rows
        )

    import_runs = legacy(_import_runs, "import_runs")
    provider_operations = legacy(_provider_operations, "provider_operations")
