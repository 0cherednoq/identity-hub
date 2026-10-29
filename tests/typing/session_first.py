"""Статический контракт session-first API (SF0.1)."""

from __future__ import annotations

from typing import assert_type
from uuid import UUID

from identity_hub import SessionAdoptionResult, SessionVerificationResult, SpaceHandle


async def _session_first_contract(space: SpaceHandle, identity_id: UUID) -> None:
    assert_type(
        await space.auth.verify_session(identity_id, "cookie-1", actor="checker"),
        SessionVerificationResult,
    )
    assert_type(
        await space.auth.adopt_session(
            identity_id,
            "cookie-1",
            actor="checker",
            activate=True,
        ),
        SessionAdoptionResult,
    )
