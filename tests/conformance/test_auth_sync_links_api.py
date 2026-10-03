"""IMP-23 (I8.7): `space.auth`, `space.sync`, `space.links`, `space.data`, `space.verifications`."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr
from tests.fixtures.services import FAKE_TG, TgSession

from identity_hub import (
    ConcurrentModification,
    IdentityCreate,
    IdentityHub,
    ImportRecord,
    Lifecycle,
    LoginChallenge,
    LoginOutcome,
    ProbeResult,
    SpaceHandle,
    Standing,
)

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]

SMS_CODE = "12345"


class SmsAuthenticator:
    async def login(self, ctx: Any) -> BaseModel:
        if ctx.external_ref.startswith("broken"):
            raise ConnectionError("platform down")
        if ctx.challenge_answer is None:
            raise LoginChallenge("sms_code", "Код из SMS", {"hash": "h1"}, timedelta(minutes=5))
        if ctx.challenge_answer != SMS_CODE:
            raise LoginChallenge("sms_code", "Неверный код", {"hash": "h1"})
        return TgSession(auth_key=SecretStr(f"key-{ctx.external_ref}"))

    async def refresh(self, credentials: BaseModel | None, session: BaseModel) -> BaseModel | None:
        return None


class StatusProbes:
    def __init__(self) -> None:
        self.codes: dict[UUID, str] = {}

    async def auth(self, ctx: Any) -> ProbeResult | None:
        return ProbeResult("authenticated")

    async def standing(self, ctx: Any) -> ProbeResult | None:
        return ProbeResult(None, code=self.codes.get(ctx.identity_id, "ok"))

    async def verification(self, ctx: Any, factor: str) -> ProbeResult | None:
        return None


PROBES = StatusProbes()
TG_LOGIN = replace(
    FAKE_TG,
    name="tg_login",
    authenticator=SmsAuthenticator(),
    probes=PROBES,
    standing_codes={**FAKE_TG.standing_codes, "ok": Standing.GOOD},
)


class Warmup(BaseModel):
    day: int = 0


@pytest.fixture
async def login_space(hub_under_test: IdentityHub, space: SpaceHandle) -> SpaceHandle:
    if TG_LOGIN.name not in {svc.name for svc in hub_under_test.services}:
        hub_under_test.register_service(TG_LOGIN)
        await hub_under_test.start()
    return space


def test_login_outcome_equals_legacy_strings() -> None:
    assert LoginOutcome.SUCCEEDED == "succeeded"
    assert LoginOutcome.CHALLENGED == "challenged"
    assert LoginOutcome.FAILED == "failed"
    assert {str(item) for item in LoginOutcome} == {"succeeded", "challenged", "failed"}


async def test_onboarding_on_new_names(login_space: SpaceHandle) -> None:
    """Сценарий онбординга из руководства: create → login → challenge → activate → refresh."""
    space = login_space
    identity = await space.identities.create(
        IdentityCreate(service="tg_login", external_ref="+100", credentials={"phone": "+100"}), actor="test"
    )
    assert identity.lifecycle is Lifecycle.PROVISIONING

    assert await space.auth.login(identity.id) is LoginOutcome.CHALLENGED
    challenge = (await space.identities.card(identity.id)).open_challenge
    assert challenge is not None and challenge["kind"] == "sms_code"
    wrong = await space.auth.answer_challenge(UUID(challenge["id"]), "000", actor="operator")
    assert wrong is LoginOutcome.CHALLENGED
    challenge = (await space.identities.card(identity.id)).open_challenge
    assert challenge is not None
    outcome = await space.auth.answer_challenge(
        UUID(challenge["id"]), SMS_CODE, actor="operator", expected_identity_id=identity.id
    )
    assert outcome is LoginOutcome.SUCCEEDED

    await space.identities.activate(identity.id, actor="onboarding")
    outcomes = (await space.sync.refresh(identity.id)).outcomes
    assert set(outcomes) == {"auth", "standing"} and "not_probed" not in outcomes.values()
    ready = await space.identities.get(identity.id)
    assert ready.lifecycle is Lifecycle.READY and ready.auth == "authenticated"
    assert ready.standing is Standing.GOOD


async def test_login_many_reports_each_outcome(login_space: SpaceHandle) -> None:
    space = login_space
    for ref in ("+110", "broken-111"):
        await space.identities.create(IdentityCreate(service="tg_login", external_ref=ref), actor="test")

    report = await space.auth.login_many(service="tg_login", limit=10)

    challenged = await space.identities.get_by_ref("tg_login", "+110")
    broken = await space.identities.get_by_ref("tg_login", "broken-111")
    assert report.challenged == [challenged.id]
    assert list(report.failed) == [broken.id]


async def test_refresh_many_applies_platform_ban(login_space: SpaceHandle) -> None:
    space = login_space
    identity = await space.identities.create(
        IdentityCreate(
            service="tg_login",
            external_ref="+120",
            sessions={"default": {"auth_key": "k"}},
            lifecycle=Lifecycle.READY,
        ),
        actor="test",
    )
    PROBES.codes[identity.id] = "banned"

    report = await space.sync.refresh_many(("standing",), service="tg_login", not_synced_for=timedelta(0))

    assert report.probed[identity.id] == {"standing": "applied"}
    assert (await space.identities.get(identity.id)).standing is Standing.BANNED


async def test_links_add_of_impact_remove(space: SpaceHandle) -> None:
    mail = await space.identities.create(IdentityCreate(service="fake_tg", external_ref="+130"), actor="test")
    main = await space.identities.create(IdentityCreate(service="fake_tg", external_ref="+131"), actor="test")

    await space.links.add(main.id, "recovery", mail.id, actor="ops")
    links = await space.links.of(main.id)
    impact = await space.links.impact(mail.id)

    assert links.linked("recovery") is not None
    assert [item.identity_id for item in impact] == [main.id]
    assert await space.links.remove(main.id, "recovery", actor="ops") is True
    assert await space.links.remove(main.id, "recovery", actor="ops") is False
    assert (await space.links.of(main.id)).outgoing == []


async def test_data_load_save_with_version(space: SpaceHandle) -> None:
    identity = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+140"), actor="test"
    )

    assert await space.data.load(identity.id, "farm.warmup", Warmup) is None
    version = await space.data.save(identity.id, "farm.warmup", Warmup(day=1), expected_version=None)
    record = await space.data.load(identity.id, "farm.warmup", Warmup)
    assert record is not None and record.value.day == 1 and record.version == version
    with pytest.raises(ConcurrentModification):
        await space.data.save(identity.id, "farm.warmup", Warmup(day=2), expected_version=version + 5)


async def test_verifications_status_review_requirements(space: SpaceHandle) -> None:
    report = await space.imports.run(
        service="fake_exchange",
        records=[
            ImportRecord(
                external_ref="ex-150",
                verifications={"email": "verified", "phone": "verified", "kyc_id": "under_review"},
            )
        ],
        actor="test",
    )
    identity_id = report.created[0]

    assert (await space.verifications.status(identity_id, "kyc_id")).state == "under_review"
    status = await space.verifications.review(
        identity_id, "kyc_id", "approve", actor="admin", reason="документы в порядке"
    )
    assert status.state == "verified"
    assert "kyc_id" in (await space.identities.get(identity_id)).verified_factors

    requirements = space.verifications.requirements
    assert await requirements.add(identity_id, "selfie", actor="platform") is True
    assert await requirements.add(identity_id, "selfie", actor="platform") is False
    assert await requirements.resolve(identity_id, "selfie", actor="admin", reason="загружено") is True
    assert await requirements.resolve(identity_id, "selfie", actor="platform") is False
