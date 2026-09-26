"""P5.1: прокси и их здоровье (ТЗ FR-8.1, 8.4, §3.7, FR-9.4)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import ValidationFailed
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import EventRecord, ProxyRecord
from identity_hub.proxies import ProxyHealthPolicy, fingerprint, parse_proxy_url

pytestmark = pytest.mark.pg


class Checker:
    def __init__(self, *results: bool) -> None:
        self.results = list(results)
        self.urls: list[str] = []

    async def __call__(self, url: str) -> bool:
        self.urls.append(url)
        return self.results.pop(0) if self.results else False


async def _make_due(sm: async_sessionmaker[AsyncSession]) -> None:
    async with sm() as s, s.begin():
        await s.execute(update(ProxyRecord).values(next_check_at=text("now() - interval '1 second'")))


async def _proxy(sm: async_sessionmaker[AsyncSession]) -> Any:
    async with sm() as s:
        return (await s.execute(select(ProxyRecord))).scalar_one()


def test_parse_and_fingerprint() -> None:
    parsed = parse_proxy_url("socks5://us%40er:p%3Ass@Proxy.Example.com:1080")
    assert (parsed.scheme, parsed.host, parsed.port, parsed.username, parsed.password) == (
        "socks5",
        "proxy.example.com",
        1080,
        "us@er",
        "p:ss",
    )
    assert fingerprint("socks5", "PROXY.example.com", 1080, "u") == fingerprint(
        "socks5", "proxy.example.com", 1080, "u"
    )
    assert fingerprint("socks5", "h", 1, "a") != fingerprint("socks5", "h", 1, "b")
    for bad in ("ftp://h:1", "socks5://h", "http://:80"):
        with pytest.raises(ValidationFailed):
            parse_proxy_url(bad)


async def test_add_dedup_and_secret(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    proxy_id, created = await space.add_proxy("socks5://user:secret@1.2.3.4:1080", pool="residential-ua")
    same_id, again = await space.add_proxy("socks5://user:other-password@1.2.3.4:1080", pool="residential-ua")
    assert created and not again and same_id == proxy_id
    many = await space.add_proxies(["http://5.6.7.8:3128", "http://5.6.7.8:3128"], pool="dc")
    assert many[0][1] and not many[1][1]
    row = await _proxy_row(sessionmaker, proxy_id)
    assert (row.host, row.port, row.scheme, row.status) == ("1.2.3.4", 1080, "socks5", "alive")
    assert b"secret" not in row.ciphertext
    assert await space._proxy_url(proxy_id) == "socks5://user:secret@1.2.3.4:1080"
    assert await space._proxy_url(many[0][0]) == "http://5.6.7.8:3128"


async def _proxy_row(sm: async_sessionmaker[AsyncSession], proxy_id: Any) -> Any:
    async with sm() as s:
        return (await s.execute(select(ProxyRecord).where(ProxyRecord.id == proxy_id))).scalar_one()


async def test_health_cycle(
    hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    hub.proxy_health = ProxyHealthPolicy(error_threshold=2, base_backoff=timedelta(seconds=10), dead_after=2)
    proxy_id, _ = await space.add_proxy("http://u:p@9.9.9.9:8080", pool="dc")

    assert await space.report_proxy_failure(proxy_id) == "alive"
    assert await space.report_proxy_failure(proxy_id, raw_status="CONNECT_TIMEOUT") == "quarantined"
    row = await _proxy(sessionmaker)
    assert row.next_check_at is not None and row.fail_streak == 0

    assert await space.recheck_proxies(Checker(True)) == {}  # срок ещё не наступил
    await _make_due(sessionmaker)
    checker = Checker(True)
    assert await space.recheck_proxies(checker) == {
        proxy_id: "quarantined"
    }  # первая успешная — ждём подтверждения
    assert checker.urls == ["http://u:p@9.9.9.9:8080"]
    assert (await _proxy(sessionmaker)).pending_observation == "alive"

    await _make_due(sessionmaker)
    assert await space.recheck_proxies(Checker(False)) == {
        proxy_id: "quarantined"
    }  # неудача сбрасывает ожидание
    row = await _proxy(sessionmaker)
    assert (row.pending_observation, row.fail_streak) == (None, 1)
    await _make_due(sessionmaker)
    assert await space.recheck_proxies(Checker(False)) == {proxy_id: "dead"}
    await _make_due(sessionmaker)
    assert await space.recheck_proxies(Checker(True)) == {}  # dead не перепроверяется по расписанию

    assert await space.recheck_proxy(proxy_id, Checker(True, True), actor="admin") == "alive"
    row = await _proxy(sessionmaker)
    assert (row.status, row.fail_streak, row.error_score, row.next_check_at) == ("alive", 0, 0.0, None)

    async with sessionmaker() as s:
        changes = [
            (e.data["from"], e.data["to"], e.severity)
            for e in (await s.execute(select(EventRecord).order_by(EventRecord.id))).scalars()
            if e.type == "proxy.status.changed"
        ]
    assert changes == [
        ("alive", "quarantined", "warning"),
        ("quarantined", "dead", "critical"),
        ("dead", "alive", "info"),
    ]


async def test_override_and_success_decay(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    proxy_id, _ = await space.add_proxy("http://1.1.1.1:80", pool="dc")
    await space.report_proxy_failure(proxy_id)
    await space.report_proxy_success(proxy_id)
    assert (await _proxy(sessionmaker)).error_score == 0.5
    assert await space.override_proxy(proxy_id, "dead", reason="провайдер закрыт", actor="admin") == "dead"
    assert await space.override_proxy(proxy_id, "alive", reason="продлили", actor="admin") == "alive"
    with pytest.raises(ValidationFailed):
        await space.override_proxy(proxy_id, "dead", reason="", actor="admin")
