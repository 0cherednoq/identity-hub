"""Жизненный цикл аккаунта из руководства docs/guide.

Запуск: ``uv run python examples/account_lifecycle.py``.
Сеть и PostgreSQL не нужны: платформа имитируется классом ``FakePlatform``.

Сценарий: онбординг с SMS-кодом → работа под арендой → flood-wait и бан по ответу
платформы → разбан человеком → бан вручную → сверка статуса пробой → верификация.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, SecretStr

from identity_hub import (
    Blame,
    Fault,
    IdentityCreate,
    IdentityHub,
    Lifecycle,
    LoginChallenge,
    OperationRule,
    Override,
    ProbeResult,
    ProxyPolicy,
    Quota,
    Service,
    Standing,
    StaticSecretsProvider,
    VerificationMode,
    VerificationSpec,
)
from identity_hub.testing import MemoryStore


class Credentials(BaseModel):
    phone: str


class Session(BaseModel):
    auth_key: SecretStr


class FloodWait(Exception):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait {seconds}s")
        self.seconds = seconds


class Banned(Exception):
    pass


@dataclass
class FakePlatform:
    """Имитация API платформы: коды, статусы аккаунтов, ошибки."""

    sms_code: str = "12345"
    statuses: dict[str, str] = field(default_factory=dict)
    next_error: Exception | None = None

    async def send(self, session: Session, text: str) -> None:
        if self.next_error is not None:
            error, self.next_error = self.next_error, None
            raise error


PLATFORM = FakePlatform()


class Authenticator:
    async def login(self, ctx: Any) -> BaseModel:
        if ctx.challenge_answer is None:
            raise LoginChallenge("sms_code", "Код из SMS", {"hash": "h1"}, timedelta(minutes=5))
        if ctx.challenge_answer != PLATFORM.sms_code:
            raise LoginChallenge("sms_code", "Неверный код", {"hash": "h1"})
        return Session(auth_key=SecretStr(f"key-{ctx.external_ref}"))


class Probes:
    async def auth(self, ctx: Any) -> ProbeResult | None:
        return ProbeResult("authenticated")

    async def standing(self, ctx: Any) -> ProbeResult | None:
        code = PLATFORM.statuses.get(str(ctx.identity_id), "OK")
        return ProbeResult(None, code=code)

    async def verification(self, ctx: Any, factor: str) -> ProbeResult | None:
        return None


class ErrorClassifier:
    def classify(self, exc: BaseException) -> Fault | None:
        if isinstance(exc, FloodWait):
            return Fault(Blame.IDENTITY, retry_after=timedelta(seconds=exc.seconds), code="FLOOD_WAIT")
        if isinstance(exc, Banned):
            return Fault(Blame.IDENTITY_BANNED, code="USER_BANNED")
        return None


SERVICE = Service(
    name="messenger",
    title="Messenger",
    credentials=Credentials,
    session=Session,
    operations={"send": OperationRule(quotas=(Quota(50, timedelta(hours=1)),))},
    verifications=(VerificationSpec("kyc", mode=VerificationMode.MANUAL, max_attempts=3),),
    authenticator=Authenticator(),
    probes=Probes(),
    error_classifier=ErrorClassifier(),
    standing_codes={"OK": Standing.GOOD, "USER_BANNED": Standing.BANNED},
    proxy_policy=ProxyPolicy(group="residential", sticky=True),
)


@dataclass
class WalkthroughResult:
    steps: list[tuple[str, str]] = field(default_factory=list)

    def record(self, step: str, value: Any) -> None:
        self.steps.append((step, str(value)))


async def _standing(space: Any, identity_id: UUID) -> str:
    return str((await space.identities.card(identity_id)).identity.standing)


async def run_walkthrough() -> WalkthroughResult:
    result = WalkthroughResult()
    hub = IdentityHub(
        store=MemoryStore(),
        secrets=StaticSecretsProvider({"example": bytes(range(32))}, active="example"),
        services=[SERVICE],
    )
    await hub.start()
    space = hub.space("guide", "Guide", create=True)
    await space.proxies.ensure("socks5://user:pass@10.0.0.1:1080", group="residential")

    # 1. Онбординг: регистрация → вход с SMS-кодом → ready
    reg = await space.identities.create(
        IdentityCreate(service=SERVICE, external_ref="+10000000001", credentials={"phone": "+10000000001"}),
        actor="example",
    )
    result.record("registered", (await space.identities.card(reg.id)).identity.lifecycle)
    result.record("login", await space.auth.login(reg.id))
    challenge = (await space.identities.card(reg.id)).open_challenge
    assert challenge is not None
    result.record(
        "answer", await space.auth.answer_challenge(UUID(challenge["id"]), PLATFORM.sms_code, actor="example")
    )
    await space.identities.set_lifecycle(reg.id, Lifecycle.READY, actor="onboarding")
    PLATFORM.statuses[str(reg.id)] = "OK"

    # 2. Работа под арендой с прокси
    pool = space.pools.define("senders", service=SERVICE)
    async with pool.lease(for_operation="send") as lease:
        result.record("proxy", lease.proxy is not None)
        session = await lease.session()
        assert isinstance(session, Session)
        async with lease.operation("send", key="send:1"):
            await PLATFORM.send(session, "hello")

    # 3. Flood-wait → cooling
    PLATFORM.next_error = FloodWait(60)
    try:
        async with pool.lease(for_operation="send") as lease, lease.operation("send"):
            await PLATFORM.send(Session(auth_key=SecretStr("x")), "hi")
    except FloodWait:
        pass
    result.record("after_flood", (await space.identities.card(reg.id)).identity.availability)
    await space.identities.override(
        reg.id, Override("availability", "", "available", reason="demo", actor="guide")
    )

    # 4. Бан по ответу платформы → разбан человеком
    PLATFORM.next_error = Banned()
    try:
        async with pool.lease(for_operation="send") as lease, lease.operation("send"):
            await PLATFORM.send(Session(auth_key=SecretStr("x")), "hi")
    except Banned:
        pass
    result.record("after_ban", await _standing(space, reg.id))
    await space.identities.restore_standing(reg.id, actor="admin", reason="ложное срабатывание")
    result.record("after_restore", await _standing(space, reg.id))

    # 5. Бан вручную и сверка пробой: sticky banned не снимается наблюдением
    await space.identities.override(
        reg.id, Override("standing", "", Standing.BANNED, reason="жалоба", actor="admin")
    )
    outcome = await space.sync.refresh(reg.id, regions=("standing",))
    result.record("resync_banned", outcome.outcomes["standing"])
    await space.identities.restore_standing(reg.id, actor="admin", reason="проверено")

    # 6. Бан на платформе обнаружен фоновой сверкой
    PLATFORM.statuses[str(reg.id)] = "USER_BANNED"
    report = await space.sync.refresh_many(regions=("standing",), not_synced_for=timedelta(0))
    result.record("reconcile", report.probed[reg.id]["standing"])
    result.record("after_reconcile", await _standing(space, reg.id))
    await space.identities.restore_standing(reg.id, actor="admin", reason="апелляция одобрена")

    # 7. Верификация: start/submit под арендой, решение человека вне аренды
    async with pool.lease(for_operation="probe", among=[reg.id]) as lease:
        kyc = lease.verification("kyc")
        await kyc.start()
        result.record("kyc_submitted", (await kyc.submit()).state)
    status = await space.verifications.review(reg.id, "kyc", "approve", actor="admin", reason="ok")
    result.record("kyc_reviewed", status.state)

    # 8. Вывод из работы
    await space.identities.retire(reg.id, actor="ops", reason="демонстрация завершена")
    result.record("retired", (await space.identities.card(reg.id)).identity.lifecycle)
    return result


def main() -> None:
    result = asyncio.run(run_walkthrough())
    for step, value in result.steps:
        print(f"{step}={value}")


if __name__ == "__main__":
    main()
