"""Narrow audit commands used by administration transports."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.events import EventDraft

__all__ = ["AdminAuditMixin"]


class AdminAuditMixin(SpaceBase):
    async def _record_secret_conversion(self, to: str, *, count: int, actor: str) -> None:
        """Audit storage conversion without secret values or row identifiers."""
        async with self._tx() as tx:
            await self._emit(
                tx,
                EventDraft(
                    "admin.secrets.converted",
                    actor=actor,
                    data={"to": to, "count": count},
                ),
            )

    async def _record_admin_import(
        self,
        action: Literal["started", "cancel_requested"],
        run_id: UUID,
        *,
        actor: str,
        reason: str,
        service: str,
        count: int | None = None,
    ) -> None:
        """Audit import lifecycle without filenames, row values or staged payloads."""
        async with self._tx() as tx:
            await self._emit(
                tx,
                EventDraft(
                    "admin.import.changed",
                    actor=actor,
                    data={
                        "action": action,
                        "run_id": run_id,
                        "reason": reason,
                        "service": service,
                        **({"count": count} if count is not None else {}),
                    },
                ),
            )

    async def _record_admin_integration(
        self,
        action: str,
        resource_kind: Literal["notification_channel", "notification_rule", "webhook_endpoint", "delivery"],
        resource_id: str,
        *,
        actor: str,
        reason: str,
        outcome: str = "changed",
    ) -> None:
        """Audit integration commands without targets, payloads, tokens or signing keys."""
        async with self._tx() as tx:
            await self._emit(
                tx,
                EventDraft(
                    "admin.integration.changed",
                    actor=actor,
                    data={
                        "action": action,
                        "resource_kind": resource_kind,
                        "resource_id": resource_id,
                        "reason": reason,
                        "outcome": outcome,
                    },
                ),
            )

    async def _record_admin_bulk(
        self,
        operation: str,
        *,
        actor: str,
        reason: str,
        snapshot_id: UUID,
        succeeded: int,
        failed: int,
        attempt: int,
    ) -> None:
        """Audit one bulk execution attempt without retaining account payloads."""
        async with self._tx() as tx:
            await self._emit(
                tx,
                EventDraft(
                    "admin.bulk.executed",
                    actor=actor,
                    data={
                        "operation": operation,
                        "reason": reason,
                        "snapshot_id": snapshot_id,
                        "succeeded": succeeded,
                        "failed": failed,
                        "attempt": attempt,
                    },
                ),
            )

    async def _record_admin_export(
        self,
        kind: Literal["accounts", "events"],
        *,
        actor: str,
        reason: str,
        format: Literal["csv", "json"],
        count: int,
        filter_fingerprint: str,
    ) -> None:
        """Audit one completed, secret-free export without retaining its contents."""
        event_type = f"admin.{kind}.exported"
        async with self._tx() as tx:
            await self._emit(
                tx,
                EventDraft(
                    event_type,
                    actor=actor,
                    data={
                        "format": format,
                        "count": count,
                        "reason": reason,
                        "filter_fingerprint": filter_fingerprint,
                    },
                ),
            )
