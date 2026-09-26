"""Помощники pg-тестов: сырые вставки до появления публичного API."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from identity_hub._ids import new_id
from identity_hub.hub import SpaceHandle
from identity_hub.postgres.models import IdentityRecord


async def make_identity(space: SpaceHandle, service: str = "fake_tg", **columns: Any) -> UUID:
    ref = await space.ref()
    identity_id = columns.pop("id", None) or new_id()
    values = {
        "id": identity_id,
        "space_id": ref.id,
        "service": service,
        "external_ref": str(identity_id),
        "lifecycle": "ready",
        "auth_state": "authenticated",
        **columns,
    }
    async with space._tx(own=True) as tx:
        await tx.execute(IdentityRecord.__table__.insert().values(**values))  # type: ignore[attr-defined]
    return identity_id
