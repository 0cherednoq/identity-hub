"""SF0.1: исполняемый контракт будущего публичного session-first API."""

from __future__ import annotations

import identity_hub

from identity_hub.api.auth import Auth


def test_session_first_public_names_and_methods() -> None:
    names = {
        "SessionAdoptionOutcome",
        "SessionAdoptionResult",
        "SessionVerificationOutcome",
        "SessionVerificationResult",
    }

    assert names <= set(identity_hub.__all__)
    assert hasattr(Auth, "verify_session")
    assert hasattr(Auth, "adopt_session")
