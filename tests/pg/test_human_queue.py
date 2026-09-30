"""A2.5: PostgreSQL projection for the secret-free human-attention queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from identity_hub._ids import new_id
from identity_hub.crypto import associated_data
from identity_hub.hub import IdentityHub, SpaceHandle

pytestmark = pytest.mark.pg


async def test_postgres_human_queue_is_scoped_and_never_reads_challenge_context(
    hub: IdentityHub,
    space: SpaceHandle,
) -> None:
    registered = await space.register(
        service="fake_exchange",
        external_ref="attention@x",
        credentials={"email": "attention@x", "password": "secret-password"},
        session={"cookies": {"sid": "secret-cookie"}},
        lifecycle="ready",
    )
    challenge_id = new_id()
    secret = "never-return-postgres-secret"
    context = hub.envelope.seal(
        f'{{"answer":"{secret}"}}'.encode(),
        associated_data("ih_challenges", registered.identity_id, str(challenge_id)),
    )
    async with space._tx() as transaction:
        assert await transaction.open_challenge(
            challenge_id,
            registered.identity_id,
            "otp",
            "***91",
            context,
            datetime.now(UTC) - timedelta(minutes=1),
        )

    foreign = hub.space("foreign", "Foreign")
    foreign_identity = await foreign.register(
        service="fake_exchange",
        external_ref="foreign@x",
        credentials={"email": "foreign@x", "password": "foreign-password"},
        lifecycle="ready",
    )
    await foreign.add_requirement(foreign_identity.identity_id, "must-not-leak")

    queue = await space.human_queue()

    assert len(queue.items) == 1
    item = queue.items[0]
    assert item.kind == "challenge" and item.expired
    assert item.details == {"kind": "otp", "hint": "***91"}
    assert item.identity_id == registered.identity_id
    assert secret not in repr(queue)
    assert foreign_identity.identity_id not in {candidate.identity_id for candidate in queue.items}
