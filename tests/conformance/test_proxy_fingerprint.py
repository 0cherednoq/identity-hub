"""IMP-04 / R2-S16 (I1.4a): отпечаток прокси — HMAC кольца ключей, ротация и пересчёт (D-207)."""

from __future__ import annotations

import hashlib
from uuid import UUID

import pytest
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG

from identity_hub import AlreadyExists, IdentityHub, SpaceHandle, StaticSecretsProvider

pytestmark = [pytest.mark.conformance]

URL = "socks5://alice:pass@10.0.0.1:1080"
FP_OLD, FP_NEW = bytes(range(40, 72)), bytes(range(80, 112))


def _legacy(username: str | None = "alice") -> str:
    """Отпечаток до I1.4a: SHA-256 без ключа — логин подбирается перебором (R2-S16)."""
    return hashlib.sha256(f"socks5|10.0.0.1|1080|{username or ''}".encode()).hexdigest()


async def _stored(space: SpaceHandle, proxy_id: UUID) -> tuple[str, str | None]:
    async with space._tx(own=True) as tx:
        row = await tx.get_proxy(proxy_id)
    assert row is not None
    return row.fingerprint, row.fingerprint_key_id


async def _hub(base: IdentityHub, **fingerprint: object) -> IdentityHub:
    hub = IdentityHub(
        store=base._store,
        secrets=StaticSecretsProvider(TEST_KEYS, "k1", **fingerprint),  # type: ignore[arg-type]
        services=[FAKE_TG, FAKE_EXCHANGE],
        clock=base.clock,
    )
    await hub.start()
    return hub


async def test_fingerprint_is_keyed_hmac(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    proxy = await space.proxies.create(URL, group="res")

    fingerprint, key_id = await _stored(space, proxy.id)

    assert key_id == "k1" and fingerprint != _legacy()
    assert fingerprint == hub_under_test._fingerprints.tag("proxy:v1|socks5|10.0.0.1|1080|alice")
    again, created = await space.proxies.ensure(URL, group="res")
    assert (again.id, created) == (proxy.id, False)
    other, created = await space.proxies.ensure("socks5://bob:pass@10.0.0.1:1080", group="res")
    assert created and other.id != proxy.id


async def test_rotation_keeps_dedup_and_rekey_moves_to_active(hub_under_test: IdentityHub) -> None:
    old = await _hub(hub_under_test, fingerprint_keys={"f1": FP_OLD})
    space = old.space("rot", create=True)
    proxy = await space.proxies.create(URL, group="res")
    bob, _ = await space.proxies.ensure("socks5://bob:p@10.0.0.1:1080", group="res")
    assert (await _stored(space, proxy.id))[1] == "f1"

    new = await _hub(hub_under_test, fingerprint_keys={"f2": FP_NEW, "f1": FP_OLD}, fingerprint_active="f2")
    rotated = new.space("rot")
    same, created = await rotated.proxies.ensure(URL, group="res")
    assert (same.id, created) == (proxy.id, False)  # совпадение по историческому ключу
    with pytest.raises(AlreadyExists):
        await rotated.proxies.replace_credentials(bob.id, username="alice", password="x", actor="ops")

    report = await new.doctor.rekey_fingerprints()
    assert report.updated == 2 and report.complete
    assert (await _stored(rotated, proxy.id)) == (
        new._fingerprints.tag("proxy:v1|socks5|10.0.0.1|1080|alice"),
        "f2",
    )
    assert (await new.doctor.rekey_fingerprints()).updated == 0


async def test_legacy_sha256_is_found_then_rekeyed(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    proxy = await space.proxies.create(URL, group="res")
    async with space._tx(own=True) as tx:  # строка, записанная до миграции 0018
        assert await tx.set_proxy_fingerprint(proxy.id, _legacy(), None)

    same, created = await space.proxies.ensure(URL, group="res")
    assert (same.id, created) == (proxy.id, False)

    report = await hub_under_test.doctor.rekey_fingerprints()
    assert report.updated >= 1 and report.complete
    fingerprint, key_id = await _stored(space, proxy.id)
    assert key_id == "k1" and fingerprint != _legacy()


async def test_fingerprint_not_in_events_or_repr(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    proxy = await space.proxies.create(URL, group="res")
    await space.proxies.replace_credentials(proxy.id, username="carol", password="p", actor="ops")
    fingerprint, _ = await _stored(space, proxy.id)

    page = await space.admin.event_page(limit=50)
    assert all(fingerprint not in repr(item) for item in page.items)
    async with space._tx(own=True) as tx:
        row = await tx.get_proxy(proxy.id)
    assert fingerprint not in repr(await space.proxies.get(proxy.id))
    assert row is not None and "carol" not in repr(row)
