"""P5.3: оркестрация входа и challenge (ТЗ FR-5.4–5.6, FR-4.3)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import ValidationFailed
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import ChallengeRecord, EventRecord, IdentityRecord
from identity_hub.protocols import LoginChallenge, LoginContext
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG, TgSession

pytestmark = pytest.mark.pg


class FakeAuth:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def login(self, ctx: LoginContext) -> BaseModel:
        ref = ctx.external_ref
        self.calls.append(ref)
        creds = await ctx.credentials()
        if ref == "boom":
            raise ConnectionError("platform down")
        if ref.startswith("sms"):
            if ctx.challenge_answer is None:
                raise LoginChallenge(
                    "sms_code", "Код из SMS", {"phone_code_hash": "h1"}, timedelta(minutes=5)
                )
            assert ctx.challenge_context == {"phone_code_hash": "h1"}
            if ctx.challenge_answer != "12345":
                raise LoginChallenge("sms_code", "Неверный код, ещё раз", {"phone_code_hash": "h1"})
        if ref == "needs_mail":
            mail = await ctx.linked("recovery_email")
            assert mail is not None and mail.external_ref == "mail@x"
            assert await ctx.linked("nope") is None
        assert creds is not None
        return TgSession(auth_key=f"key-{ref}")  # type: ignore[arg-type]

    async def refresh(self, credentials: BaseModel | None, session: BaseModel) -> BaseModel | None:
        return None


@pytest.fixture
async def auth(hub: IdentityHub) -> FakeAuth:
    fake = FakeAuth()
    hub.register_service(replace(FAKE_TG, name="tg_auth", authenticator=fake))
    await hub.start()
    return fake


async def _reg(space: SpaceHandle, ref: str, **kwargs: Any) -> UUID:
    result = await space.register(
        service="tg_auth", external_ref=ref, credentials={"phone": ref}, lifecycle="ready", **kwargs
    )
    return result.identity_id


async def _auth_state(sm: async_sessionmaker[AsyncSession], identity_id: UUID) -> str:
    async with sm() as s:
        return str(
            (
                await s.execute(select(IdentityRecord.auth_state).where(IdentityRecord.id == identity_id))
            ).scalar_one()
        )


async def _types(sm: async_sessionmaker[AsyncSession]) -> list[str]:
    async with sm() as s:
        return list((await s.execute(select(EventRecord.type).order_by(EventRecord.id))).scalars())


async def test_run_logins_success_and_failure(
    auth: FakeAuth, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ok = await _reg(space, "ok")
    bad = await _reg(space, "boom")
    authed = await _reg(space, "already", session={"auth_key": "x"})
    report = await space.run_logins(service="tg_auth", limit=10)
    assert report.succeeded == [ok]
    assert list(report.failed) == [bad] and "ConnectionError" in report.failed[bad]
    assert authed not in auth.calls
    assert await _auth_state(sessionmaker, ok) == "authenticated"
    assert await _auth_state(sessionmaker, bad) == "no_session"
    types = await _types(sessionmaker)
    assert "identity.login.succeeded" in types and "identity.login.failed" in types

    lease = await space.pool("p", service="tg_auth").acquire(among=[ok])  # сессия сохранена
    session = await lease.session()
    assert isinstance(session, TgSession) and session.auth_key.get_secret_value() == "key-ok"


async def test_challenge_flow(
    auth: FakeAuth, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await _reg(space, "sms-1")
    report = await space.run_logins(service="tg_auth")
    assert report.challenged == [identity_id]
    assert await _auth_state(sessionmaker, identity_id) == "challenge"
    async with sessionmaker() as s:
        challenge = (await s.execute(select(ChallengeRecord))).scalar_one()
    assert (challenge.kind, challenge.hint, challenge.resolved_at) == ("sms_code", "Код из SMS", None)
    assert b"h1" not in challenge.context_ciphertext

    assert await space.answer_challenge(challenge.id, "000") == "challenged"  # неверный код — новый вопрос
    async with sessionmaker() as s:
        challenges = list(
            (await s.execute(select(ChallengeRecord).order_by(ChallengeRecord.created_at))).scalars()
        )
    assert len(challenges) == 2 and challenges[0].resolved_at is not None
    assert challenges[0].answer_ciphertext is not None and b"000" not in challenges[0].answer_ciphertext
    with pytest.raises(ValidationFailed):
        await space.answer_challenge(challenges[0].id, "12345")

    assert await space.answer_challenge(challenges[1].id, "12345") == "succeeded"
    assert await _auth_state(sessionmaker, identity_id) == "authenticated"
    types = await _types(sessionmaker)
    assert types.count("identity.challenge.opened") == 2
    assert types.count("identity.challenge.answered") == 2
    assert "identity.login.succeeded" in types


async def test_challenge_expiry(
    auth: FakeAuth, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    identity_id = await _reg(space, "sms-2")
    await space.run_logins(service="tg_auth")
    assert await space.expire_challenges() == 0
    async with sessionmaker() as s, s.begin():
        await s.execute(update(ChallengeRecord).values(expires_at=text("now() - interval '1 second'")))
    with pytest.raises(ValidationFailed, match="истёк"):
        await space.answer_challenge(await _open_challenge_id(sessionmaker, identity_id), "12345")
    assert await space.expire_challenges() == 1
    assert await _auth_state(sessionmaker, identity_id) == "no_session"
    assert "identity.challenge.expired" in await _types(sessionmaker)


async def _open_challenge_id(sm: async_sessionmaker[AsyncSession], identity_id: UUID) -> UUID:
    async with sm() as session:
        return (
            await session.execute(
                select(ChallengeRecord.id).where(
                    ChallengeRecord.identity_id == identity_id,
                    ChallengeRecord.resolved_at.is_(None),
                )
            )
        ).scalar_one()


async def test_run_one_selected_login(
    auth: FakeAuth, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    selected = await _reg(space, "selected")
    untouched = await _reg(space, "untouched")
    assert await space.run_login(selected) == "succeeded"
    assert "selected" in auth.calls and "untouched" not in auth.calls
    assert await _auth_state(sessionmaker, selected) == "authenticated"
    assert await _auth_state(sessionmaker, untouched) == "no_session"


async def test_linked_identity_leased_during_login(
    auth: FakeAuth, hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    mail = await space.register(
        service="fake_exchange",
        external_ref="mail@x",
        credentials={"email": "mail@x", "password": "p"},
        lifecycle="ready",
    )
    identity_id = await _reg(space, "needs_mail", links={"recovery_email": mail.identity_id})
    report = await space.run_logins(service="tg_auth")
    assert report.succeeded == [identity_id], report.failed
    # аренда связанного аккаунта освобождена
    free = await space.pool("x", service="fake_exchange").acquire(
        among=[mail.identity_id], for_operation="probe"
    )
    assert free.identity_id == mail.identity_id
    assert FAKE_EXCHANGE.name == "fake_exchange"


async def test_service_without_authenticator(space: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed):
        await space.run_logins(service="fake_tg")
