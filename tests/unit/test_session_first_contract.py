"""SF3.1: исполняемый контракт публичного session-first API."""

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
    assert set(identity_hub.SessionVerificationOutcome) == {
        "valid",
        "invalid",
        "challenged",
        "failed",
    }
    assert set(identity_hub.SessionAdoptionOutcome) == {
        "adopted",
        "invalid",
        "challenged",
        "failed",
    }
