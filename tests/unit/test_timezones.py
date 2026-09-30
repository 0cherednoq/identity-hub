from __future__ import annotations

import pytest

from identity_hub.crypto import StaticSecretsProvider
from identity_hub.errors import ValidationFailed
from identity_hub.hub import IdentityHub
from identity_hub.testing import MemoryStore


def _hub(timezone: str) -> IdentityHub:
    return IdentityHub(
        secrets=StaticSecretsProvider({"k1": b"1" * 32}, "k1"),
        store=MemoryStore(),
        timezone=timezone,
    )


async def test_hub_timezone_is_validated_and_persisted_for_new_spaces() -> None:
    hub = _hub("Europe/Simferopol")
    assert (await hub.space("crimea").ref()).timezone == "Europe/Simferopol"


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValidationFailed, match="unknown IANA timezone"):
        _hub("Mars/Olympus_Mons")
