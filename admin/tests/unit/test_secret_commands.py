from __future__ import annotations

import asyncio
import json
from typing import cast
from uuid import UUID

from identity_hub.clock import FakeClock
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub
from identity_hub.service import Service
from identity_hub.testing.memory import MemoryStore
from litestar.testing import TestClient
from pydantic import BaseModel, SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.settings import AdminSettings, Environment


class _Profile(BaseModel):
    display_name: str
    private_note: str


class _Credentials(BaseModel):
    username: str
    password: SecretStr


class _Session(BaseModel):
    token: SecretStr


SECRET_SERVICE = Service(
    name="secret_account",
    title="Secret account",
    profile=_Profile,
    credentials=_Credentials,
    session=_Session,
    filterable_profile=frozenset({"display_name"}),
    profile_encrypted=True,
)
SECRET_ID = UUID("00000000-0000-0000-0000-000000003501")


def _settings(*, reveal_rate_limit: int = 10) -> AdminSettings:
    tokens = {
        f"{role}-secret-token": {
            "actor": f"secret:{role}",
            "role": role,
            "allowed_spaces": ["secret-space"],
        }
        for role in ("viewer", "operator", "admin")
    }
    return AdminSettings(
        environment=Environment.TEST,
        run_dispatcher=False,
        bearer_tokens=SecretStr(json.dumps(tokens)),
        reveal_rate_limit=reveal_rate_limit,
    )


def _headers(role: str, key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {role}-secret-token",
        "Idempotency-Key": key,
    }


def _hub() -> IdentityHub:
    return IdentityHub(
        secrets=StaticSecretsProvider({"test": b"0123456789abcdef0123456789abcdef"}, "test"),
        services=[SECRET_SERVICE],
        store=MemoryStore(FakeClock()),
        strict_events=True,
    )


async def _seed(hub: IdentityHub) -> None:
    await hub.space("secret-space", "Secrets").register(
        service=SECRET_SERVICE,
        external_ref="secret-target",
        id=SECRET_ID,
        profile={"display_name": "Visible", "private_note": "profile-secret"},
        credentials={"username": "alice", "password": "old-password"},
        session={"token": "old-session"},
        lifecycle="ready",
        actor="test:seed",
    )


def _confirmation(code: str) -> dict[str, object]:
    return {"confirmed": True, "consequence_codes": [code]}


def test_reveal_and_update_require_admin_reauth_never_cache_and_audit_without_values() -> None:
    hub = _hub()
    asyncio.run(_seed(hub))
    store = cast(MemoryStore, hub.store)
    app = create_admin_app(_settings(), hub=hub)
    base = f"/api/v1/spaces/secret-space/accounts/{SECRET_ID}"
    with TestClient(app) as client:
        detail = client.get(base, headers=_headers("admin", "read-secret-detail")).json()["data"]
        access = client.get(f"{base}/access", headers=_headers("admin", "read-secret-access"))
        assert {item["field"] for item in access.json()["data"]["secrets"]} == {
            "credentials",
            "session:default",
        }
        payload = {
            "reason": "support investigation",
            "expected_revision": detail["revision"],
            "field": "credentials",
            "reauthentication": "admin-secret-token",
            "confirmation": _confirmation("reveals_secret"),
        }
        denied = client.post(
            f"{base}/secrets/reveal",
            headers=_headers("operator", "operator-reveal-denied"),
            json=payload,
        )
        wrong = client.post(
            f"{base}/secrets/reveal",
            headers=_headers("admin", "wrong-reauth-secret"),
            json={**payload, "reauthentication": "wrong-token"},
        )
        revealed = client.post(
            f"{base}/secrets/reveal",
            headers=_headers("admin", "reveal-credentials-0001"),
            json=payload,
        )
        assert denied.status_code == 403
        assert wrong.status_code == 401
        assert revealed.status_code == 201
        assert revealed.json()["data"]["value"] == {
            "username": "alice",
            "password": "old-password",
        }
        assert "no-store" in revealed.headers["cache-control"]
        assert "old-password" not in repr(app.state.idempotency._entries)

        revision = client.get(base, headers=_headers("admin", "read-after-reveal")).json()["data"]["revision"]
        updated = client.post(
            f"{base}/secrets/update",
            headers=_headers("admin", "update-credentials-0001"),
            json={
                "reason": "credential rotation",
                "expected_revision": revision,
                "kind": "credentials",
                "value": {"username": "alice", "password": "new-password"},
                "reauthentication": "admin-secret-token",
                "confirmation": _confirmation("replaces_secret"),
            },
        )
        assert updated.status_code == 201
        assert "no-store" in updated.headers["cache-control"]
        assert "new-password" not in repr(app.state.idempotency._entries)

        revision = client.get(base, headers=_headers("admin", "read-after-update")).json()["data"]["revision"]
        profile = client.post(
            f"{base}/secrets/reveal",
            headers=_headers("admin", "reveal-profile-0001"),
            json={
                "reason": "profile investigation",
                "expected_revision": revision,
                "field": "profile",
                "reauthentication": "admin-secret-token",
                "confirmation": _confirmation("reveals_secret"),
            },
        )
        assert profile.json()["data"]["value"] == {"private_note": "profile-secret"}

    serialized_events = json.dumps([event.data for event, _, _ in store._state.events], default=str)
    assert "old-password" not in serialized_events
    assert "new-password" not in serialized_events
    assert "profile-secret" not in serialized_events
    reveals = [event for event, _, _ in store._state.events if event.type == "identity.secret.revealed"]
    assert [event.data["field"] for event in reveals] == ["credentials", "profile"]


def test_reveal_rate_limit_is_enforced_before_decryption() -> None:
    hub = _hub()
    asyncio.run(_seed(hub))
    base = f"/api/v1/spaces/secret-space/accounts/{SECRET_ID}"
    with TestClient(create_admin_app(_settings(reveal_rate_limit=1), hub=hub)) as client:
        revision = client.get(base, headers=_headers("admin", "rate-read")).json()["data"]["revision"]
        first = client.post(
            f"{base}/secrets/reveal",
            headers=_headers("admin", "rate-reveal-0001"),
            json={
                "reason": "first reveal",
                "expected_revision": revision,
                "field": "credentials",
                "reauthentication": "admin-secret-token",
                "confirmation": _confirmation("reveals_secret"),
            },
        )
        second = client.post(
            f"{base}/secrets/reveal",
            headers=_headers("admin", "rate-reveal-0002"),
            json={
                "reason": "second reveal",
                "expected_revision": revision + 1,
                "field": "credentials",
                "reauthentication": "admin-secret-token",
                "confirmation": _confirmation("reveals_secret"),
            },
        )
    assert first.status_code == 201
    assert second.status_code == 429
