"""Статический контракт session-first API (SF3.1)."""

from __future__ import annotations

from typing import assert_type
from uuid import UUID

from identity_hub import (
    SessionAdoptionOutcome,
    SessionAdoptionResult,
    SessionVerificationOutcome,
    SessionVerificationResult,
    SpaceHandle,
)


async def _session_first_contract(space: SpaceHandle, identity_id: UUID) -> None:
    verification = await space.auth.verify_session(identity_id, "cookie-1", actor="checker")
    assert_type(verification, SessionVerificationResult)
    assert_type(verification.outcome, SessionVerificationOutcome)
    adoption = await space.auth.adopt_session(
        identity_id,
        "cookie-1",
        actor="checker",
        activate=True,
    )
    assert_type(adoption, SessionAdoptionResult)
    assert_type(adoption.outcome, SessionAdoptionOutcome)
