"""S12.2: eazy SessionStore is backed by the slot under the caller's lease."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel

from identity_hub import (
    IdentityCreate,
    IdentityHub,
    OperationRule,
    Service,
    SpaceHandle,
    ValidationFailed,
)
from identity_hub.integrations.eazy import EazySession, HubSessionStore

pytestmark = pytest.mark.conformance


@dataclass(frozen=True)
class FakeSessionKey:
    value: str


@dataclass(frozen=True)
class FakeSessionRevision:
    value: int


@dataclass(frozen=True)
class FakeStoredSession:
    value: Any
    revision: FakeSessionRevision
    cookies: Any | None = None


class FakeSessionRevisionError(RuntimeError):
    pass


class FakeCookieState:
    def __init__(self, value: list[dict[str, object]]) -> None:
        self.value = value

    @classmethod
    def from_primitive(cls, value: object) -> FakeCookieState:
        assert isinstance(value, list)
        return cls(value)

    def to_primitive(self) -> list[dict[str, object]]:
        return self.value


@pytest.fixture(autouse=True)
def fake_eazy(monkeypatch: pytest.MonkeyPatch) -> None:
    package = types.ModuleType("eazy_sdk")
    auth = types.ModuleType("eazy_sdk.auth")
    session = types.ModuleType("eazy_sdk.auth.session")
    cookies = types.ModuleType("eazy_sdk.cookies")
    session.SessionKey = FakeSessionKey  # type: ignore[attr-defined]
    session.SessionRevision = FakeSessionRevision  # type: ignore[attr-defined]
    session.SessionRevisionError = FakeSessionRevisionError  # type: ignore[attr-defined]
    session.StoredSession = FakeStoredSession  # type: ignore[attr-defined]
    cookies.CookieState = FakeCookieState  # type: ignore[attr-defined]
    package.auth = auth  # type: ignore[attr-defined]
    package.cookies = cookies  # type: ignore[attr-defined]
    auth.session = session  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "eazy_sdk", package)
    monkeypatch.setitem(sys.modules, "eazy_sdk.auth", auth)
    monkeypatch.setitem(sys.modules, "eazy_sdk.auth.session", session)
    monkeypatch.setitem(sys.modules, "eazy_sdk.cookies", cookies)


class SdkSession(BaseModel):
    token: str


async def test_hub_session_store_round_trip_revision_and_invalidation(
    hub_under_test: IdentityHub,
    space: SpaceHandle,
) -> None:
    schema = EazySession[SdkSession]
    service = Service(
        "eazy_contract",
        "eazy contract",
        session=schema,
        operations={"store": OperationRule(needs_auth=False)},
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()
    identity = await space.identities.create(
        IdentityCreate(service=service.name, external_ref="eazy-1", lifecycle="ready"),
        actor="test",
    )
    lease = await space.pools.define("eazy-contract", service=service).acquire(for_operation="store")
    store = HubSessionStore[SdkSession](lease)
    key = store.key("default")
    cookies = FakeCookieState.from_primitive(
        [
            {
                "name": "sid",
                "value": "secret",
                "domain": "example.com",
                "path": "/",
                "host_only": True,
                "expires_at": None,
                "secure": True,
                "http_only": True,
                "same_site": "Lax",
            }
        ]
    )

    assert identity.id == lease.identity_id
    assert await lease.session_revision() == 0
    assert await store.load(key) is None
    await store.save(key, SdkSession(token="one"), FakeSessionRevision(1), cookies)

    persisted = await lease.session(schema=schema)
    assert persisted is not None and persisted.value.token == "one"
    assert persisted.cookies is not None and persisted.cookies.cookies.root[0].name == "sid"
    assert await lease.session_revision() == 1
    loaded = await store.load(key)
    assert loaded.value == SdkSession(token="one")
    assert loaded.revision == FakeSessionRevision(1)
    assert loaded.cookies.to_primitive() == cookies.to_primitive()

    with pytest.raises(FakeSessionRevisionError):
        await store.save(key, SdkSession(token="stale"), FakeSessionRevision(1))
    await store.save(key, SdkSession(token="two"), FakeSessionRevision(2))
    with pytest.raises(FakeSessionRevisionError):
        await store.invalidate(key, FakeSessionRevision(1))
    await store.invalidate(key, FakeSessionRevision(2))
    assert await store.load(key) is None
    assert await lease.session_revision() == 2
    lease.check()  # store did not release or replace the caller's lease

    other_key = FakeSessionKey("session:00000000-0000-0000-0000-000000000000:default")
    with pytest.raises(ValidationFailed, match="не принадлежит"):
        await store.load(other_key)
    await lease.release()
