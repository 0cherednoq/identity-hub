"""F3.4: вход не застревает в `logging_in` и не теряет результат (R2-F01, R2-F20, R2-F21, R2-F22)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel

from identity_hub import (
    IdentityHub,
    LoginChallenge,
    LoginOutcome,
    Override,
    Service,
    SpaceHandle,
    StaticSecretsProvider,
    ValidationFailed,
)
from identity_hub.clock import FakeClock
from identity_hub.errors import LeaseUnavailable
from identity_hub.read.events import EventFilters
from identity_hub.testing import MemoryStore, make_identity, make_service

pytestmark = pytest.mark.unit


class Sess(BaseModel):
    auth_key: str


class _Auth:
    """Аутентификатор из функции: `login(ctx)` — корутина."""

    def __init__(self, login: Any) -> None:
        self._login = login

    async def login(self, ctx: Any) -> Any:
        return await self._login(ctx)

    async def refresh(self, ctx: Any, session: Any) -> Any:
        return None


async def _hub(login: Any, **service: Any) -> tuple[IdentityHub, FakeClock, SpaceHandle, UUID, Service]:
    svc = make_service("svc", session=Sess, authenticator=_Auth(login), **service)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    hub = IdentityHub(
        store=MemoryStore(),
        secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"),
        services=[svc],
        clock=clock,
    )
    await hub.start()
    space = hub.space("s", "S")
    identity_id = await make_identity(space, service=svc, authenticated=False)
    return hub, clock, space, identity_id, svc


async def _auth(space: SpaceHandle, identity_id: UUID) -> str:
    return str((await space.identities.get(identity_id)).auth)


async def _live_lease(space: SpaceHandle, identity_id: UUID) -> Any:
    async with space._tx(own=True) as tx:
        return await tx.current_lease(identity_id)


async def _events(space: SpaceHandle, type_: str) -> list[Any]:
    page = await space.admin.event_page(filters=EventFilters(type=type_, include_journal=True))
    return list(page.items)


# ---------------------------------------------------------------- R2-F01


async def test_session_of_wrong_shape_fails_and_restores_auth() -> None:
    """Сессия не по схеме: исход FAILED, состояние возвращено, повторный вход возможен."""

    async def login(ctx: Any) -> Any:
        return {"wrong": 1}

    _, _clock, space, identity_id, svc = await _hub(login)

    assert await space.auth.login(identity_id) is LoginOutcome.FAILED
    assert await _auth(space, identity_id) == "no_session"
    assert await _live_lease(space, identity_id) is None
    assert await space.auth.login(identity_id) is LoginOutcome.FAILED  # не ValidationFailed из logging_in
    report = await space.auth.login_many(service=svc)
    assert list(report.failed) == [identity_id]  # login_many аккаунт по-прежнему видит
    failed = await _events(space, "identity.login.failed")
    assert failed and all(item.data["error"] == "ValidationFailed" for item in failed)


async def test_login_longer_than_lease_deadline_keeps_the_session() -> None:
    """Вход на 45 с при дедлайне аренды 40 с: heartbeat продлевает аренду, сессия сохранена."""
    holder: dict[str, FakeClock] = {}

    async def login(ctx: Any) -> Any:
        await holder["clock"].sleep(45)
        return Sess(auth_key="k")

    _, clock, space, identity_id, svc = await _hub(login)
    holder["clock"] = clock

    assert await space.auth.login(identity_id) is LoginOutcome.SUCCEEDED
    assert await _auth(space, identity_id) == "authenticated"
    async with space.pools.define("p", service=svc).lease() as lease:
        assert (await lease.session()).auth_key == "k"  # type: ignore[union-attr]


async def test_cancelled_login_restores_auth_and_frees_the_lease() -> None:
    """Вызывающий обернул вход в `wait_for`: состояние возвращено, аренда освобождена."""

    async def login(ctx: Any) -> Any:
        await asyncio.sleep(3600)

    _, _clock, space, identity_id, _svc = await _hub(login)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(space.auth.login(identity_id), 0.3)

    assert await _auth(space, identity_id) == "no_session"
    assert await _live_lease(space, identity_id) is None
    (failed,) = await _events(space, "identity.login.failed")
    assert failed.data["error"] == "CancelledError"


async def test_cancelled_login_from_session_invalid_returns_to_session_invalid() -> None:
    async def login(ctx: Any) -> Any:
        await asyncio.sleep(3600)

    _, _clock, space, identity_id, _svc = await _hub(login)
    await space.identities.override(
        identity_id, Override("auth", "default", "session_invalid", reason="тест", actor="op")
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(space.auth.login(identity_id), 0.3)

    assert await _auth(space, identity_id) == "session_invalid"


async def test_override_closes_open_challenge_and_login_starts_over() -> None:
    """Override из `challenge` закрывает вопрос: следующий вход открывает новый, старый не отвечается."""

    async def login(ctx: Any) -> Any:
        if ctx.challenge_answer is None:
            raise LoginChallenge("sms", "code", {"h": 1})
        return Sess(auth_key="k")

    _, _clock, space, identity_id, _svc = await _hub(login)
    assert await space.auth.login(identity_id) is LoginOutcome.CHALLENGED
    old = UUID(str((await space.identities.card(identity_id)).open_challenge["id"]))  # type: ignore[index]

    await space.identities.override(
        identity_id, Override("auth", "default", "no_session", reason="restart", actor="op")
    )
    assert (await space.identities.card(identity_id)).open_challenge is None

    assert await space.auth.login(identity_id) is LoginOutcome.CHALLENGED
    new = UUID(str((await space.identities.card(identity_id)).open_challenge["id"]))  # type: ignore[index]
    assert new != old
    with pytest.raises(ValidationFailed):
        await space.auth.answer_challenge(old, "1")
    assert await space.auth.answer_challenge(new, "1") is LoginOutcome.SUCCEEDED
    assert await _auth(space, identity_id) == "authenticated"


async def test_login_timeout_bounds_the_authenticator() -> None:
    async def login(ctx: Any) -> Any:
        await asyncio.sleep(3600)

    _, _clock, space, identity_id, _svc = await _hub(login, login_timeout=timedelta(seconds=0.2))

    assert await asyncio.wait_for(space.auth.login(identity_id), 5) is LoginOutcome.FAILED
    assert await _auth(space, identity_id) == "no_session"
    (failed,) = await _events(space, "identity.login.failed")
    assert failed.data["error"] == "LoginTimeout"
    assert make_service("x").login_timeout == timedelta(seconds=120)


async def test_abandoned_logging_in_is_reset_by_maintenance() -> None:
    """Процесс упал во время входа: обслуживание возвращает `logging_in` без аренды в `no_session`."""
    from dataclasses import replace

    from identity_hub.enums import AuthState

    async def login(ctx: Any) -> Any:
        return Sess(auth_key="k")

    hub, clock, space, identity_id, svc = await _hub(login, login_timeout=timedelta(seconds=120))
    pool = space.pools.define("crashed", service=svc)
    query = replace(
        pool.candidate_query(for_operation="work"),
        needs_auth=False,
        auth_states=frozenset({AuthState.NO_SESSION}),
        among=[identity_id],
    )
    crashed = await pool.acquire(query=query)
    await space._set_auth(crashed, AuthState.LOGGING_IN)  # дальше процесс «упал»: release не будет

    clock.advance(61)  # аренда истекла, но срок входа ещё не вышел
    await hub.maintenance.run_all()
    assert await _auth(space, identity_id) == "logging_in"

    clock.advance(180)
    await hub.maintenance.run_all()
    assert await _auth(space, identity_id) == "no_session"
    (failed,) = await _events(space, "identity.login.failed")
    assert (failed.actor, failed.data["error"]) == ("timer", "LoginAbandoned")
    assert await space.auth.login(identity_id) is LoginOutcome.SUCCEEDED


# ---------------------------------------------------------------- R2-F20


async def test_closed_challenge_does_not_keep_the_answer() -> None:
    """Ответ на challenge (SMS-код, пароль 2FA) затирается, когда challenge закрыт."""

    async def login(ctx: Any) -> Any:
        if ctx.challenge_answer is None:
            raise LoginChallenge("sms", "code", {"h": 1})
        return Sess(auth_key="k")

    _, _clock, space, identity_id, _svc = await _hub(login)
    await space.auth.login(identity_id)
    challenge_id = UUID(str((await space.identities.card(identity_id)).open_challenge["id"]))  # type: ignore[index]

    assert await space.auth.answer_challenge(challenge_id, "123456") is LoginOutcome.SUCCEEDED

    async with space._tx(own=True) as tx:
        row = await tx.get_challenge(challenge_id)
    assert row is not None and row.resolved_at is not None and row.answered_at is not None
    assert row.answer_nonce is None and row.answer_ciphertext is None


# ---------------------------------------------------------------- R2-F21, R2-F22


async def test_second_login_of_busy_account_fails_at_once() -> None:
    """Аккаунт уже входит: повторный `login` сразу даёт `LeaseUnavailable`, а не ждёт TTL."""
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def login(ctx: Any) -> Any:
        entered.set()
        await proceed.wait()
        return Sess(auth_key="k")

    _, clock, space, identity_id, _svc = await _hub(login)
    first = asyncio.create_task(space.auth.login(identity_id))
    await entered.wait()
    before = clock.monotonic()

    with pytest.raises(LeaseUnavailable):
        await space.auth.login(identity_id)

    assert clock.monotonic() - before < 1  # ожидания свободной аренды не было
    proceed.set()
    assert await first is LoginOutcome.SUCCEEDED


async def test_linked_lease_wait_is_bounded_by_the_main_lease() -> None:
    """`ctx.linked()` ждёт занятый связанный аккаунт не дольше остатка основной аренды."""
    waited: dict[str, float] = {}
    holder: dict[str, Any] = {}

    async def login(ctx: Any) -> Any:
        started = holder["clock"].monotonic()
        try:
            await ctx.linked("recovery")
        finally:
            waited["seconds"] = holder["clock"].monotonic() - started
        return Sess(auth_key="k")

    mail = make_service("mail")
    svc = make_service("svc", session=Sess, authenticator=_Auth(login))
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    holder["clock"] = clock
    hub = IdentityHub(
        store=MemoryStore(),
        secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"),
        services=[svc, mail],
        clock=clock,
    )
    await hub.start()
    space = hub.space("s", "S")
    mail_id = await make_identity(space, service=mail)
    identity_id = await make_identity(space, service=svc, authenticated=False, links={"recovery": mail_id})
    busy = await space.pools.define("mailers", service=mail).acquire()  # почта занята другим воркером

    assert await space.auth.login(identity_id) is LoginOutcome.FAILED

    assert waited["seconds"] <= 40  # ttl − safety_margin основной аренды, а не TTL связанной (60 с)
    assert await _auth(space, identity_id) == "no_session"
    await busy.release()
    assert await space.auth.login(identity_id) is LoginOutcome.SUCCEEDED
    assert await _live_lease(space, mail_id) is None  # связанная аренда освобождена
