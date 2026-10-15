"""S9: отпечатки значений и точечная валидность credentials/session."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from pydantic import SecretStr
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_TG, TgSession

from identity_hub import (
    AcceptPolicy,
    CredentialInput,
    IdentityCreate,
    IdentityHub,
    ImportRecord,
    LoginFailure,
    LoginOutcome,
    SpaceHandle,
    StaticSecretsProvider,
)

pytestmark = pytest.mark.conformance

FP_OLD, FP_NEW = bytes(range(32, 64)), bytes(range(64, 96))


async def _fingerprints(space: SpaceHandle, identity_id: object) -> tuple[str | None, str | None]:
    async with space._tx(own=True) as tx:
        credential = await tx.get_credential(identity_id, "default", "default")
        sessions = await tx.session_history(identity_id, "default")
    assert credential is not None and sessions
    return credential[1].value_fingerprint, sessions[-1].value_fingerprint


async def test_secret_values_get_canonical_fingerprints_and_cards_hide_them(
    space: SpaceHandle,
) -> None:
    first = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="fp-1",
            credentials={"password": "same", "phone": "+100"},
            session={"dc_id": 2, "auth_key": "session"},
        ),
        actor="test",
    )
    second = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="fp-2",
            credentials={"phone": "+100", "password": "same"},
            session={"auth_key": "session"},
        ),
        actor="test",
    )
    third = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="fp-3",
            credentials={"phone": "+100", "password": "different"},
            session={"auth_key": "other"},
        ),
        actor="test",
    )

    first_fp = await _fingerprints(space, first.id)
    second_fp = await _fingerprints(space, second.id)
    third_fp = await _fingerprints(space, third.id)
    assert first_fp[0] == second_fp[0] != third_fp[0]
    assert first_fp[1] == second_fp[1] != third_fp[1]

    card = await space.secrets.describe(first.id)
    assert card.credential.last_verified_at is None
    assert card.credential.last_rejected_at is None
    assert card.credential.reject_code is None
    assert card.sessions[0].last_verified_at is None
    assert card.sessions[0].last_rejected_at is None
    assert card.sessions[0].reject_code is None
    rendered = repr(card)
    assert "same" not in rendered and first_fp[0] not in rendered and first_fp[1] not in rendered


async def test_overwrite_recomputes_fingerprint_and_record_card_has_validity_fields(
    space: SpaceHandle,
) -> None:
    identity = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="fp-replace",
            credentials={"phone": "+200", "password": "old"},
        ),
        actor="test",
    )
    before, _session = await _fingerprints_with_optional_session(space, identity.id)

    await space.secrets.replace_credentials(
        identity.id,
        {"password": "new", "phone": "+200"},
        actor="test",
    )
    async with space._tx(own=True) as tx:
        stored = await tx.get_credential(identity.id, "default", "default")
    assert stored is not None and stored[1].value_fingerprint != before

    records = (await space.secrets.describe(identity.id)).credential_records
    assert len(records) == 1
    assert records[0].last_verified_at is None
    assert records[0].last_rejected_at is None
    assert records[0].reject_code is None
    assert not hasattr(records[0], "value_fingerprint")


async def test_fingerprint_key_rotation_retags_credentials_and_sessions(
    hub_under_test: IdentityHub,
) -> None:
    old = IdentityHub(
        store=hub_under_test._store,
        secrets=StaticSecretsProvider(TEST_KEYS, "k1", fingerprint_keys={"f1": FP_OLD}),
        services=[FAKE_TG],
        clock=hub_under_test.clock,
    )
    await old.start()
    space = old.space("value-fingerprint-rotation", create=True)
    identity = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="rotate-values",
            credentials={"phone": "+250", "password": "secret"},
            session={"auth_key": "session"},
        ),
        actor="test",
    )
    before = await _fingerprints(space, identity.id)

    new = IdentityHub(
        store=hub_under_test._store,
        secrets=StaticSecretsProvider(
            TEST_KEYS,
            "k1",
            fingerprint_keys={"f1": FP_OLD, "f2": FP_NEW},
            fingerprint_active="f2",
        ),
        services=[FAKE_TG],
        clock=hub_under_test.clock,
    )
    await new.start()
    report = await new.doctor.rekey_fingerprints(batch_size=1)

    after = await _fingerprints(new.space("value-fingerprint-rotation"), identity.id)
    assert after[0] != before[0] and after[1] != before[1]
    assert report.value_updated == {"credentials": 1, "sessions": 1}
    assert report.value_unreadable == () and report.complete


async def _fingerprints_with_optional_session(
    space: SpaceHandle, identity_id: object
) -> tuple[str | None, str | None]:
    async with space._tx(own=True) as tx:
        credential = await tx.get_credential(identity_id, "default", "default")
        sessions = await tx.session_history(identity_id, "default")
    assert credential is not None
    return credential[1].value_fingerprint, sessions[-1].value_fingerprint if sessions else None


class CandidateAuthenticator:
    async def login(self, ctx: Any) -> TgSession:
        assert await ctx.credential("default", name="bad") is not None
        await ctx.reject_credential("default", name="bad", code="wrong_password")
        assert await ctx.credential("default", name="good") is not None
        await ctx.verify_credential("default", name="good")
        return TgSession(auth_key=SecretStr("accepted"))


async def test_authenticator_marks_losing_and_winning_pairs_atomically(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    service = replace(FAKE_TG, name="validity_marks", authenticator=CandidateAuthenticator())
    hub_under_test.register_service(service)
    await hub_under_test.start()
    identity = await space.identities.create(
        IdentityCreate(
            service=service.name,
            external_ref="candidate-pairs",
            lifecycle="ready",
            credential_records=[
                CredentialInput("default", {"phone": "+300", "password": "bad"}, name="bad"),
                CredentialInput("default", {"phone": "+300", "password": "good"}, name="good"),
            ],
        ),
        actor="test",
    )

    assert await space.auth.login(identity.id) is LoginOutcome.SUCCEEDED

    records = {row.name: row for row in (await space.secrets.describe(identity.id)).credential_records}
    assert records["bad"].last_rejected_at is not None
    assert records["bad"].reject_code == "wrong_password"
    assert records["bad"].last_verified_at is None
    assert records["good"].last_verified_at is not None
    assert records["good"].last_rejected_at is None
    assert records["good"].reject_code is None


class ShortcutAuthenticator:
    def __init__(self) -> None:
        self.calls = 0

    async def login(self, ctx: Any) -> TgSession:
        self.calls += 1
        if self.calls == 1:
            raise LoginFailure(
                "credentials_rejected",
                retryable=False,
                credential=("default", "default"),
            )
        await ctx.verify_credential("default", name="fresh")
        return TgSession(auth_key=SecretStr("fresh-session"))


async def test_shortcut_reject_disables_by_policy_and_fresh_pair_reopens_login_many(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator = ShortcutAuthenticator()
    service = replace(
        FAKE_TG,
        name="validity_shortcut",
        authenticator=authenticator,
        disable_rejected_credentials=True,
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()
    identity = await space.identities.create(
        IdentityCreate(
            service=service.name,
            external_ref="shortcut",
            lifecycle="ready",
            credentials={"phone": "+400", "password": "bad"},
        ),
        actor="test",
    )

    assert await space.auth.login(identity.id) is LoginOutcome.FAILED
    rejected = (await space.secrets.describe(identity.id)).credential_records[0]
    assert rejected.lifecycle == "disabled"
    assert rejected.reject_code == "credentials_rejected"

    report = await space.auth.login_many(service=service, limit=10)
    assert report.succeeded == [] and authenticator.calls == 1

    await space.secrets.put_credential(
        identity.id,
        "default",
        {"phone": "+400", "password": "fresh"},
        name="fresh",
        actor="test",
    )
    report = await space.auth.login_many(service=service, limit=10)
    assert report.succeeded == [identity.id]
    assert authenticator.calls == 2
    fresh = {row.name: row for row in (await space.secrets.describe(identity.id)).credential_records}["fresh"]
    assert fresh.last_verified_at is not None


async def test_acceptance_retries_account_while_an_unrejected_pair_remains(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    authenticator = ShortcutAuthenticator()
    service = replace(
        FAKE_TG,
        name="validity_acceptance",
        authenticator=authenticator,
        default_accept=AcceptPolicy(max_attempts=2),
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()
    report = await space.imports.run(
        service=service,
        records=[
            ImportRecord(
                "accept-candidates",
                credentials={"phone": "+500", "password": "bad"},
            )
        ],
        actor="test",
    )
    merged = await space.imports.run(
        service=service,
        records=[
            ImportRecord(
                "accept-candidates",
                credential_records=[
                    CredentialInput(
                        "default",
                        {"phone": "+500", "password": "fresh"},
                        name="fresh",
                    )
                ],
            )
        ],
        on_existing="merge",
        actor="test",
    )
    assert merged.updated == report.created

    assert await hub_under_test.workers.acceptance.run_once() == 1
    assert authenticator.calls == 1
    assert await hub_under_test.workers.acceptance.run_once() == 1
    assert authenticator.calls == 2
    identity = await space.identities.get(report.created[0])
    assert identity.auth == "authenticated"
