"""F5.1: pytest-плагин, MemoryStore в тестах пользователя и фабрики (R1-01, R2-P02, R2-P19, R2-P20)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from identity_hub import IdentityHub, StaticSecretsProvider, ValidationFailed
from identity_hub.errors import LeaseUnavailable
from identity_hub.testing import MemoryStore, make_identity, make_service
from identity_hub.testing import TestingModel as Model  # имя Test* pytest принял бы за тесты

pytest_plugins = ("pytester",)
pytestmark = pytest.mark.unit

USER_TEST = """
import inspect

import pytest


{mark}
async def test_hub_fixture(identity_hub, identity_hub_space, identity_hub_make_identity):
    assert not inspect.iscoroutine(identity_hub)
    identity_id = await identity_hub_make_identity()
    lease = await identity_hub_space.pools.define("p", service="test_service").acquire()
    assert lease.identity_id == identity_id
    await lease.release()
"""


@pytest.mark.parametrize(
    ("mode", "mark"),
    [("strict", "@pytest.mark.asyncio"), ("auto", "")],
)
def test_plugin_fixture_works_in_both_pytest_asyncio_modes(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, mode: str, mark: str
) -> None:
    """R1-01: фикстура `identity_hub` работает и в strict-режиме pytest-asyncio (режим по умолчанию)."""
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")  # вывод дочернего pytest читается как UTF-8
    pytester.makeini(f"[pytest]\nasyncio_mode = {mode}\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makepyfile(USER_TEST.format(mark=mark))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")

    result.assert_outcomes(passed=1)


def test_plugin_without_pytest_asyncio_explains_what_to_install(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    pytester.makepyfile(
        """
def test_needs_hub(identity_hub):
    pass
"""
    )

    result = pytester.runpytest_subprocess("-p", "no:asyncio", "-p", "no:cacheprovider")

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*pytest-asyncio*"])


async def _hub() -> tuple[IdentityHub, object]:
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"),
        services=[make_service("svc")],
        store=MemoryStore(),
    )
    await hub.start()
    return hub, hub.space("s", "S", create=True)


async def test_among_select_on_memory_store_is_a_clear_error() -> None:
    """R2-P02: подзапрос SQL в `among` на MemoryStore — понятная ошибка, а не `TypeError`."""
    hub, space = await _hub()
    await make_identity(space, service="svc")
    pool = space.pools.define("p", service="svc")

    with pytest.raises(ValidationFailed, match="MemoryStore"):
        await pool.acquire(among=select(hub.read.identities.c.id))


async def test_among_accepts_a_predicate() -> None:
    _, space = await _hub()
    await make_identity(space, service="svc", external_ref="skip", labels={"farm": "1"})
    wanted = await make_identity(space, service="svc", external_ref="take", labels={"farm": "2"})
    pool = space.pools.define("p", service="svc")

    lease = await pool.acquire(among=lambda identity: identity.labels.get("farm") == "2")

    assert lease.identity_id == wanted
    await lease.release()
    with pytest.raises(LeaseUnavailable):
        await pool.acquire(among=lambda identity: False)


def test_memory_store_survives_contention_in_two_event_loops() -> None:
    """R2-P19: блокировка MemoryStore не привязана к первому event loop."""
    store = MemoryStore()

    async def contend() -> None:
        hub = IdentityHub(
            secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"),
            services=[make_service("svc")],
            store=store,
        )
        await hub.start()
        space = hub.space("s", "S", create=True)

        async def hold() -> None:
            async with space._tx(own=True):
                await asyncio.sleep(0.01)

        await asyncio.gather(hold(), hold(), hold())

    asyncio.run(contend())
    asyncio.run(contend())


async def test_make_identity_rejects_session_for_unauthenticated_account() -> None:
    """R2-P20: `authenticated=False` вместе с `session` — противоречие, а не тихий authenticated."""
    _, space = await _hub()

    with pytest.raises(ValidationFailed):
        await make_identity(space, service="svc", authenticated=False, session={"token": "x"})


async def test_testing_model_rejects_unknown_fields() -> None:
    """R2-P20: опечатка в поле не проходит молча."""
    with pytest.raises(ValueError):
        Model(pasword="x")  # type: ignore[call-arg]
