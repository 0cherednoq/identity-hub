"""F1.4: свободный текст не уносит секреты в историю аренды и события (R2-S04, R2-S06, R2-S17)."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle
from identity_hub.pool import Pool
from identity_hub.protocols import Blame, Fault
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance

SECRET_URL = "https://api.platform.example/v1/send?api_key=SK-LIVE-12345"
PROXY_URL = "socks5://user:PROXYPASS@1.2.3.4:1080"
LEAKS = ("SK-LIVE-12345", "PROXYPASS", "SESSION-KEY")


class _Classifier:
    def classify(self, exc: BaseException) -> Fault | None:
        return Fault(Blame.SESSION_INVALID, code=f"AUTH {exc}", raw_status=str(exc))


CLASSIFIED = replace(FAKE_TG, name="leaky", error_classifier=_Classifier())


async def _pool(hub: IdentityHub, space: SpaceHandle, service: str = "fake_tg") -> Pool:
    if service == "leaky":
        hub.register_service(CLASSIFIED)
        await hub.start()
    await register(space, service=service, external_ref="+1", lifecycle="ready", session={"auth_key": "k"})
    return space.pools.define("p", service=service, holder="w")


async def _events(space: SpaceHandle, type_: str) -> list[dict[str, Any]]:
    page = await space.admin.event_page(filters=EventFilters(type=type_, include_journal=True))
    return [dict(item.data) for item in page.items]


def _assert_clean(value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, default=str)
    for leak in LEAKS:
        assert leak not in text


async def test_lease_body_exception_text_is_not_stored(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-S04: в `last_error` и `lease.failed` остаётся только имя типа исключения."""
    pool = await _pool(hub_under_test, space)
    with pytest.raises(RuntimeError):
        async with pool.lease() as lease:
            identity_id = lease.identity_id
            raise RuntimeError(f"401 Unauthorized for url '{SECRET_URL}' via proxy {PROXY_URL}")

    (history,) = await space.admin.lease_history(identity_id)
    assert (history.end_reason, history.last_error) == ("failed", "RuntimeError")
    (failed,) = await _events(space, "lease.failed")
    assert failed["error"] == "RuntimeError"


async def test_operation_exception_text_is_not_stored(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    pool = await _pool(hub_under_test, space)
    async with pool.lease() as lease:
        identity_id = lease.identity_id
        with pytest.raises(ValueError):
            async with lease.operation("read_channel"):
                raise ValueError(f"bad response for {SECRET_URL}")

    (history,) = await space.admin.lease_history(identity_id)
    assert history.last_error == "ValueError"
    _assert_clean(await _events(space, "lease.released"))
    _assert_clean(await _events(space, "operation.failed"))


async def test_explicit_release_error_is_sanitized(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    pool = await _pool(hub_under_test, space)
    lease = await pool.acquire()
    await lease.release("failed", error=f"упало на {PROXY_URL}: {'x' * 5000}")
    (history,) = await space.admin.lease_history(lease.identity_id)
    assert history.last_error is not None and history.last_error.startswith("упало на socks5://")
    assert "PROXYPASS" not in history.last_error and len(history.last_error) <= 256


async def test_classifier_text_is_sanitized_in_events_and_card(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-S06: `raw_status` и `code` из классификатора проходят санитайзер и лимит длины."""
    pool = await _pool(hub_under_test, space, service="leaky")
    async with pool.lease() as lease:
        identity_id = lease.identity_id
        with pytest.raises(RuntimeError):
            async with lease.operation("read_channel"):
                raise RuntimeError(f"401 for url {SECRET_URL} session=abcdef-SESSION-KEY " + "z" * 500)

    changed = await _events(space, "identity.auth.changed")
    assert changed, "наблюдение session_invalid должно дать событие"
    _assert_clean(changed)
    _assert_clean(await _events(space, "operation.failed"))
    card = await space.identities.card(identity_id)
    auth = [region for region in card.regions if region.region == "auth"]
    _assert_clean([region.raw_status for region in auth])
    assert all(region.raw_status is None or len(region.raw_status) <= 128 for region in auth)
    assert all(len(event["code"]) <= 64 for event in changed if event.get("code"))


async def test_refs_stay_in_events_by_default(space: SpaceHandle) -> None:
    await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+79990001122", labels={"owner": "ivan"}), actor="test"
    )
    (registered,) = await _events(space, "identity.registered")
    assert registered["external_ref"] == "+79990001122"
    assert registered["labels"] == {"owner": "ivan"}


async def test_redact_refs_in_events_hides_personal_data(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-S17 (D-152): по настройке хаба `external_ref` и `labels` во внешние события не идут."""
    hub_under_test.redact_refs_in_events = True
    identity = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+79990001122", labels={"owner": "ivan"}), actor="test"
    )
    await space.identities.delete(identity.id, actor="admin")

    events = [*await _events(space, "identity.registered"), *await _events(space, "identity.deleted")]
    assert len(events) == 2
    text = json.dumps(events, ensure_ascii=False, default=str)
    assert "+79990001122" not in text and "ivan" not in text
    assert all(event["external_ref"] == "[redacted]" for event in events)
