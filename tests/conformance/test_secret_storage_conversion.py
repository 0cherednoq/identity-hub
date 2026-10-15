"""S5.2: идемпотентный перевод credentials/session без доменных изменений."""

from __future__ import annotations

import pytest

from identity_hub import IdentityCreate, SecretRef, SpaceHandle
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


async def test_convert_round_trip_preserves_values_and_revisions(space: SpaceHandle) -> None:
    hub = space.hub
    identities = []
    for number in range(3):
        identities.append(
            await space.identities.create(
                IdentityCreate(
                    service="fake_tg",
                    external_ref=f"+convert-{number}",
                    credentials={"phone": f"+convert-{number}", "password": f"pw-{number}"},
                    session={"auth_key": f"session-{number}"},
                ),
                actor="test",
            )
        )
    identity_id = identities[0].id
    async with space._tx(own=True) as tx:
        credential_before = (await tx.get_credential(identity_id, "default", "default"))[1]  # type: ignore[index]
        session_before = await tx.session_history(identity_id, "default")

    plaintext = await hub.doctor.convert_secrets(to="plaintext", batch=2)
    assert plaintext.counts == {"credentials": 3, "sessions": 3}
    assert plaintext.spaces == {space.slug: 6}
    assert await hub.doctor.plaintext_secrets(space=space.slug) == {
        "credentials": 3,
        "sessions": 3,
    }
    assert (await hub.doctor.convert_secrets(to="plaintext", batch=2)).total == 0

    encrypted = await hub.doctor.convert_secrets(to="encrypted", space=space.slug, batch=2)
    assert encrypted.counts == {"credentials": 3, "sessions": 3}
    assert await hub.doctor.plaintext_secrets(space=space.slug) == {}
    assert (await hub.doctor.convert_secrets(to="encrypted", batch=2)).total == 0

    for number, identity in enumerate(identities):
        credentials = await space.secrets.reveal(
            identity.id, SecretRef.CREDENTIALS, actor="admin", reason="round trip"
        )
        session = await space.secrets.reveal(
            identity.id, SecretRef.session(), actor="admin", reason="round trip"
        )
        assert credentials["password"] == f"pw-{number}"
        assert session["auth_key"] == f"session-{number}"

    async with space._tx(own=True) as tx:
        credential_after = (await tx.get_credential(identity_id, "default", "default"))[1]  # type: ignore[index]
        session_after = await tx.session_history(identity_id, "default")
    assert (credential_after.revision, credential_after.version) == (
        credential_before.revision,
        credential_before.version,
    )
    assert session_after == session_before

    events = await space.admin.event_page(
        filters=EventFilters(type="admin.secrets.converted", include_journal=True)
    )
    assert [(event.data["to"], event.data["count"]) for event in events.items] == [
        ("encrypted", 6),
        ("plaintext", 6),
    ]


async def test_convert_can_be_limited_to_one_space(space: SpaceHandle) -> None:
    other = space.hub.space(f"{space.slug}-other", create=True)
    for target, suffix in ((space, "a"), (other, "b")):
        await target.identities.create(
            IdentityCreate(
                service="fake_tg",
                external_ref=f"+scope-{suffix}",
                credentials={"phone": f"+scope-{suffix}", "password": suffix},
            ),
            actor="test",
        )

    report = await space.hub.doctor.convert_secrets(to="plaintext", space=space.slug, batch=1)
    assert report.counts == {"credentials": 1, "sessions": 0}
    assert await space.hub.doctor.plaintext_secrets(space=space.slug) == {"credentials": 1}
    assert await space.hub.doctor.plaintext_secrets(space=other.slug) == {}
