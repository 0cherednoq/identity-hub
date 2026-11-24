"""Прокси закрепляется после проверки, invalid/unknown/challenge решает приложение.

Запуск: uv run python examples/proxy_assignment.py
Сеть и PostgreSQL для main не нужны: FakeSite имитирует ответы платформы.
confirm_in_transaction показывает отдельную границу SQL-транзакции приложения;
сетевой checker должен завершиться до её открытия.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from identity_hub import (
    AllOf,
    AnyOf,
    AuthState,
    ChangeSource,
    IdentityCreate,
    IdentityHub,
    Lease,
    LoginChallenge,
    LoginContext,
    Observation,
    OperationRule,
    Pool,
    ProbeContext,
    ProbeResult,
    ProxyAssignment,
    ProxyBindingPolicy,
    ProxyPolicy,
    RegionIn,
    Service,
    SpaceHandle,
    StaticSecretsProvider,
)
from identity_hub.testing import MemoryStore


class Credentials(BaseModel):
    username: str
    password: SecretStr


class Session(BaseModel):
    key: SecretStr


class Verdict(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"
    CHALLENGED = "challenged"


class FakeSite:
    """Замените методы своим клиентом; URL используется только для сетевого подключения."""

    def __init__(self) -> None:
        self.auth_state: AuthState | None = AuthState.AUTHENTICATED
        self.challenge = False
        self.proxy_used = False

    async def login(self, ctx: LoginContext[Credentials, Session]) -> Session:
        assert ctx.proxy_url is not None
        self.proxy_used = True
        if self.challenge and ctx.challenge_answer is None:
            raise LoginChallenge("sms", "Код из SMS", context={"ticket": "demo"})
        return Session(key="demo-session")

    async def auth(self, ctx: ProbeContext[Credentials, Session]) -> ProbeResult:
        assert ctx.proxy_url is not None
        self.proxy_used = True
        return ProbeResult(self.auth_state)


def site_service(client: FakeSite) -> Service[Credentials, Session]:
    return Service(
        name="proxy_example_site",
        title="Example site",
        credentials=Credentials,
        session=Session,
        authenticator=client,
        probes=client,
        operations={"check": OperationRule()},
        proxy_policy=ProxyPolicy(
            group="site-egress",
            binding=ProxyBindingPolicy(
                bind_when=AllOf(
                    RegionIn("auth", states={"authenticated"}), RegionIn("lifecycle", states={"ready"})
                ),
                # Это политика данного приложения. У библиотеки нет default «аккаунт мёртв».
                unbind_when=AnyOf(
                    RegionIn("auth", states={"session_invalid"}), RegionIn("standing", states={"banned"})
                ),
            ),
        ),
    )


async def configure_proxy_group(space: SpaceHandle) -> UUID:
    proxy = await space.proxies.create("http://127.0.0.1:9085", group="site-egress", max_identities=3)
    await space.proxies.configure_group(
        "site-egress",
        capacity_accounting="allocations",
        actor="operator",
        reason="enable deferred assignment",
    )
    return proxy.id


async def account(space: SpaceHandle, ref: str, *, with_session: bool = True) -> UUID:
    return (
        await space.identities.create(
            IdentityCreate(
                service="proxy_example_site",
                external_ref=ref,
                lifecycle="ready",
                credentials={"username": ref, "password": "demo-password"},
                session={"key": "candidate-session"} if with_session else None,
            ),
            actor="ingest",
        )
    ).id


async def manual_check(
    space: SpaceHandle,
    pool: Pool[Credentials, Session],
    identity: UUID,
    checker: Callable[[Lease[Credentials, Session]], Awaitable[Verdict]],
) -> ProxyAssignment | None:
    async with pool.lease(among=[identity]) as lease:
        verdict = await checker(lease)  # Network I/O: DB-транзакция ещё не открыта.
        if verdict is Verdict.VALID:
            return await lease.confirm_proxy(reason="application checker accepted account")
        if verdict is Verdict.INVALID:
            # Приложение хранит свой verdict и явно снимает только проверенное поколение.
            # Общий identities.observe — факт об аккаунте, без fence текущей аренды.
            await space.proxies.unbind(
                identity,
                actor="checker",
                reason="application checker rejected account",
                expected_binding_id=lease.proxy_allocation_id,
            )
        # UNKNOWN/CHALLENGED не меняют состояние; выход возвращает лишь временный слот.
        return None


async def confirm_in_transaction(
    space: SpaceHandle,
    lease: Lease[Credentials, Session],
    session: AsyncSession,
    write_result: Callable[[AsyncSession, UUID], Awaitable[None]],
) -> ProxyAssignment:
    """Сеть уже проверена. Вызывающий владеет session.begin()/commit/rollback."""
    await write_result(session, lease.identity_id)  # Только таблицы приложения.
    decision = await space.using(session).identities.observe(
        lease.identity_id,
        Observation("auth", "default", "authenticated"),
        actor="checker",
        source=ChangeSource.PROBE,
    )
    if str(decision.outcome) not in {"applied", "unchanged"}:
        raise RuntimeError("observation was not accepted")
    return await lease.using(session).confirm_proxy(reason="accepted check and application result")


async def demonstrate(hub: IdentityHub, client: FakeSite) -> dict[str, Any]:
    space = hub.space("proxy-example", create=True)
    proxy = await configure_proxy_group(space)
    valid = await account(space, "login", with_session=False)
    login = await space.auth.login(valid)
    assert str(login) == "succeeded"
    assert (await space.admin.proxy_detail(proxy)).proxy.bound_count == 1

    manual = await account(space, "manual")
    pool = space.pools.define("checkers", service="proxy_example_site", holder="manual-checker")

    async def check(lease: Lease[Credentials, Session]) -> Verdict:
        assert lease.proxy is not None  # Передайте lease.proxy.reveal() вашему сетевому клиенту.
        return Verdict.VALID

    assignment = await manual_check(space, pool, manual, check)
    assert assignment is not None and assignment.state == "bound"

    unknown = await account(space, "unknown")
    client.auth_state = None
    uncertain = await space.auth.verify_session(unknown, actor="checker")
    assert str(uncertain.outcome) == "failed"
    assert (await space.admin.proxy_detail(proxy)).proxy.bound_count == 2

    challenged = await account(space, "challenge", with_session=False)
    client.challenge = True
    challenge = await space.auth.login(challenged)
    assert str(challenge) == "challenged"
    assert (await space.admin.proxy_detail(proxy)).proxy.bound_count == 2

    client.auth_state = AuthState.SESSION_INVALID
    invalid = await space.auth.verify_session(valid, actor="checker")
    assert str(invalid.outcome) == "invalid"
    detail = (await space.admin.proxy_detail(proxy)).proxy
    assert (detail.bound_count, detail.reserved_count, detail.draining_count) == (1, 0, 0)
    return {
        "login": str(login),
        "manual": assignment.state,
        "unknown": str(uncertain.outcome),
        "challenge": str(challenge),
        "later_check": str(invalid.outcome),
        "bound": detail.bound_count,
        "temporary_slots": detail.reserved_count + detail.draining_count,
        "provider_used_proxy": client.proxy_used,
    }


async def main() -> None:
    client = FakeSite()
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"demo": b"0123456789abcdef0123456789abcdef"}, "demo"),
        services=[site_service(client)],
        store=MemoryStore(),
        secret_storage="encrypted",
    )
    await hub.start()
    print(await demonstrate(hub, client))


if __name__ == "__main__":
    asyncio.run(main())
