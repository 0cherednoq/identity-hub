from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from identity_hub.clock import FakeClock
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.domain.verification import VerificationSpec
from identity_hub.hub import IdentityHub
from identity_hub.protocols import LoginChallenge, LoginContext
from identity_hub.state import Override
from identity_hub.testing.memory import MemoryStore
from litestar.testing import TestClient
from pydantic import BaseModel, SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import DEMO_SERVICE, create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment


def _settings() -> AdminSettings:
    tokens = {
        f"{role}-token": {
            "actor": f"test:{role}",
            "role": role,
            "allowed_spaces": ["demo-large"],
        }
        for role in ("viewer", "operator", "admin")
    }
    return AdminSettings(
        environment=Environment.TEST,
        demo_mode=True,
        run_dispatcher=False,
        bearer_tokens=SecretStr(json.dumps(tokens)),
    )


def _headers(role: str, key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {role}-token", "Idempotency-Key": key}


def _confirmation(*codes: str) -> dict[str, Any]:
    return {"confirmed": True, "consequence_codes": list(codes)}


COMMAND_ID = UUID("00000000-0000-0000-0000-000000003201")
INTERACTION_ID = UUID("00000000-0000-0000-0000-000000003301")
RESOURCE_TARGET_ID = UUID("00000000-0000-0000-0000-000000003401")
RESOURCE_LINKED_ID = UUID("00000000-0000-0000-0000-000000003402")
RESOURCE_DEPENDENT_ID = UUID("00000000-0000-0000-0000-000000003403")


class _Session(BaseModel):
    sid: SecretStr


class _ChallengeAuthenticator:
    async def login(self, context: LoginContext) -> BaseModel:
        if context.challenge_answer is None:
            raise LoginChallenge(
                "otp", "Код из приложения", {"nonce": "encrypted-context"}, timedelta(minutes=5)
            )
        if context.challenge_answer != "654321":
            raise LoginChallenge("otp", "Неверный код", {"nonce": "encrypted-context"})
        return _Session(sid=SecretStr("session-secret"))

    async def refresh(self, credentials: BaseModel | None, session: BaseModel) -> BaseModel | None:
        del credentials, session
        return None


INTERACTION_SERVICE = replace(
    DEMO_SERVICE,
    name="interactive_account",
    session=_Session,
    authenticator=_ChallengeAuthenticator(),
    verifications=(VerificationSpec(name="kyc", reject_codes=frozenset({"blurry", "expired"})),),
)


def _interaction_hub() -> IdentityHub:
    clock = FakeClock(datetime(2026, 1, 15, 12, 0, tzinfo=UTC))
    return IdentityHub(
        secrets=StaticSecretsProvider({"test": b"0123456789abcdef0123456789abcdef"}, "test"),
        services=[INTERACTION_SERVICE],
        store=MemoryStore(clock),
        clock=clock,
        strict_events=True,
    )


async def _seed_interaction_target(hub: IdentityHub) -> None:
    space = hub.space("demo-large", "Interactive commands")
    await space.register(
        service=INTERACTION_SERVICE,
        external_ref="interactive-target",
        id=INTERACTION_ID,
        profile={"display_name": "Interactive target", "country": "UA"},
        lifecycle="ready",
        actor="test:seed",
    )
    lease = await space.pool("seed", service=INTERACTION_SERVICE).acquire(
        among=[INTERACTION_ID], for_operation="probe"
    )
    verification = lease.verification("kyc")
    await verification.start()
    await verification.submit()
    await lease.release()


async def _seed_command_target(hub: Any) -> None:
    space = hub.space("demo-large", "Large dataset")
    await space.register(
        service=DEMO_SERVICE,
        external_ref="command-target",
        id=COMMAND_ID,
        profile={"display_name": "Command target", "country": "UA"},
        lifecycle="ready",
        actor="test:seed",
    )
    await space.override(
        COMMAND_ID,
        Override("standing", "", "banned", reason="seed blocked account", actor="test:seed"),
    )
    await space.add_requirement(
        COMMAND_ID,
        "identity_review",
        actor="test:seed",
    )


def test_state_commands_follow_machine_revision_roles_and_audit() -> None:
    hub = create_demo_hub()
    store = cast(MemoryStore, hub.store)
    with TestClient(create_admin_app(_settings(), hub=hub)) as client:
        item = client.get(
            "/api/v1/spaces/demo-large/accounts?lifecycle=ready&limit=1",
            headers=_headers("viewer", "unused-view-key"),
        ).json()["data"]["items"][0]
        identity_id = item["id"]
        detail_url = f"/api/v1/spaces/demo-large/accounts/{identity_id}"
        revision = client.get(detail_url, headers=_headers("viewer", "unused-detail-key")).json()["data"][
            "revision"
        ]
        state = client.get(f"{detail_url}/state", headers=_headers("operator", "unused-state-key"))
        action_ids = {action["id"] for action in state.json()["data"]["actions"]}

        denied = client.post(
            f"{detail_url}/commands/lifecycle",
            headers=_headers("viewer", "viewer-retire-0001"),
            json={
                "reason": "support retirement",
                "expected_revision": revision,
                "target": "retired",
                "confirmation": _confirmation("removes_from_lease_pool"),
            },
        )
        invalid = client.post(
            f"{detail_url}/commands/lifecycle",
            headers=_headers("operator", "invalid-transition-0001"),
            json={"reason": "invalid test", "expected_revision": revision, "target": "provisioning"},
        )
        missing_confirmation = client.post(
            f"{detail_url}/commands/lifecycle",
            headers=_headers("operator", "missing-confirmation-0001"),
            json={"reason": "support retirement", "expected_revision": revision, "target": "retired"},
        )
        retired = client.post(
            f"{detail_url}/commands/lifecycle",
            headers=_headers("operator", "retire-command-0001"),
            json={
                "reason": "support retirement",
                "expected_revision": revision,
                "target": "retired",
                "confirmation": _confirmation("removes_from_lease_pool"),
            },
        )
        stale = client.post(
            f"{detail_url}/commands/lifecycle",
            headers=_headers("operator", "stale-command-0001"),
            json={"reason": "stale retry", "expected_revision": revision, "target": "ready"},
        )

    assert "lifecycle:retired" in action_ids
    assert "lifecycle:provisioning" not in action_ids
    assert denied.status_code == 403
    assert invalid.status_code == 409
    assert missing_confirmation.status_code == 400
    assert retired.status_code == 201
    assert retired.json()["data"]["state"] == "retired"
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "revision_mismatch"
    event = next(
        event
        for event, _, _ in reversed(store._state.events)
        if event.type == "identity.lifecycle.changed" and str(event.identity_id) == identity_id
    )
    assert event.actor == "test:operator"
    assert event.data["reason"] == "support retirement"
    assert "secret" not in json.dumps(event.data).lower()


def test_override_unpin_restore_and_requirement_resolution() -> None:
    hub = create_demo_hub()
    asyncio.run(_seed_command_target(hub))
    with TestClient(create_admin_app(_settings(), hub=hub)) as client:
        identity_id = str(COMMAND_ID)
        detail_url = f"/api/v1/spaces/demo-large/accounts/{identity_id}"
        revision = client.get(detail_url, headers=_headers("viewer", "unused-banned-detail")).json()["data"][
            "revision"
        ]
        restored = client.post(
            f"{detail_url}/commands/standing-restore",
            headers=_headers("operator", "restore-command-0001"),
            json={
                "reason": "appeal accepted",
                "expected_revision": revision,
                "confirmation": _confirmation("clears_sticky_state"),
            },
        )
        revision = client.get(detail_url, headers=_headers("viewer", "unused-after-restore")).json()["data"][
            "revision"
        ]
        overridden = client.post(
            f"{detail_url}/commands/state-override",
            headers=_headers("admin", "override-command-0001"),
            json={
                "reason": "platform status disputed",
                "expected_revision": revision,
                "region": "standing",
                "key": "",
                "target": "limited",
                "pin_seconds": 3600,
                "confirmation": _confirmation("can_mask_platform_observations"),
            },
        )
        revision = client.get(detail_url, headers=_headers("viewer", "unused-after-override")).json()["data"][
            "revision"
        ]
        unpinned = client.post(
            f"{detail_url}/commands/state-unpin",
            headers=_headers("admin", "unpin-command-0001"),
            json={
                "reason": "platform correction deployed",
                "expected_revision": revision,
                "region": "standing",
                "key": "",
                "confirmation": _confirmation("platform_observations_can_apply"),
            },
        )

        requirement_url = detail_url
        queue_before = client.get(
            "/api/v1/spaces/demo-large/human-queue?kind=requirement&limit=20",
            headers=_headers("viewer", "unused-queue-before"),
        ).json()["data"]
        requirement_revision = client.get(
            requirement_url, headers=_headers("viewer", "unused-requirement-detail")
        ).json()["data"]["revision"]
        resolved = client.post(
            f"{requirement_url}/commands/requirement-resolve",
            headers=_headers("operator", "resolve-requirement-0001"),
            json={
                "reason": "documents reviewed",
                "expected_revision": requirement_revision,
                "code": "identity_review",
            },
        )
        queue_after = client.get(
            "/api/v1/spaces/demo-large/human-queue?kind=requirement&limit=20",
            headers=_headers("viewer", "unused-queue-after"),
        ).json()["data"]

    assert restored.status_code == 201
    assert restored.json()["data"]["state"] == "good"
    assert overridden.status_code == 201
    assert overridden.json()["data"]["state"] == "limited"
    assert unpinned.status_code == 201
    assert unpinned.json()["data"]["state"] == "limited"
    target_before = next(item for item in queue_before["items"] if item["identity_id"] == identity_id)
    assert target_before["action_available"] is True
    assert resolved.status_code == 201
    assert resolved.json()["data"]["changed"] is True
    assert all(item["identity_id"] != identity_id for item in queue_after["items"])


def test_login_challenge_and_manual_verification_are_guarded_and_secret_free() -> None:
    hub = _interaction_hub()
    asyncio.run(_seed_interaction_target(hub))
    store = cast(MemoryStore, hub.store)
    app = create_admin_app(_settings().model_copy(update={"demo_mode": False}), hub=hub)
    with TestClient(app) as client:
        detail_url = f"/api/v1/spaces/demo-large/accounts/{INTERACTION_ID}"
        revision = client.get(detail_url, headers=_headers("operator", "read-before-login")).json()["data"][
            "revision"
        ]
        section = client.get(
            f"{detail_url}/verification", headers=_headers("operator", "read-actions-before-login")
        ).json()["data"]
        action_ids = {item["id"] for item in section["actions"]}
        assert {"run-login", "verification-approve:kyc", "verification-reject:kyc"} <= action_ids

        login = client.post(
            f"{detail_url}/commands/login",
            headers=_headers("operator", "interactive-login-0001"),
            json={"reason": "operator requested login", "expected_revision": revision},
        )
        assert login.status_code == 201 and login.json()["data"]["outcome"] == "challenged"

        challenged = client.get(
            f"{detail_url}/verification", headers=_headers("operator", "read-open-challenge")
        ).json()["data"]
        challenge = challenged["data"]["challenge"]
        assert challenge["expired"] is False
        assert f"answer-challenge:{challenge['id']}" in {item["id"] for item in challenged["actions"]}
        queue = client.get(
            "/api/v1/spaces/demo-large/human-queue?kind=challenge",
            headers=_headers("operator", "read-challenge-queue"),
        ).json()["data"]
        assert queue["items"][0]["action_available"] is True

        revision = client.get(detail_url, headers=_headers("operator", "read-before-answer")).json()["data"][
            "revision"
        ]
        answer_payload = {
            "reason": "customer supplied otp",
            "expected_revision": revision,
            "challenge_id": challenge["id"],
            "answer": "654321",
        }
        answered = client.post(
            f"{detail_url}/commands/challenge-answer",
            headers=_headers("operator", "interactive-answer-0001"),
            json=answer_payload,
        )
        replay = client.post(
            f"{detail_url}/commands/challenge-answer",
            headers=_headers("operator", "interactive-answer-0001"),
            json=answer_payload,
        )
        assert answered.status_code == replay.status_code == 201
        assert answered.json()["data"]["outcome"] == "succeeded"

        revision = client.get(detail_url, headers=_headers("operator", "read-before-review")).json()["data"][
            "revision"
        ]
        approved = client.post(
            f"{detail_url}/commands/verification-review",
            headers=_headers("operator", "interactive-approve-0001"),
            json={
                "reason": "manual documents accepted",
                "expected_revision": revision,
                "factor": "kyc",
                "action": "approve",
                "confirmation": _confirmation("factor_becomes_verified"),
            },
        )
        assert approved.status_code == 201
        assert approved.json()["data"]["state"] == "verified"

    serialized_events = json.dumps([event.data for event, _, _ in store._state.events], default=str)
    assert "654321" not in serialized_events
    assert "session-secret" not in serialized_events
    assert "654321" not in repr(app.state.idempotency._entries)
    review_event = next(
        event
        for event, _, _ in reversed(store._state.events)
        if event.type == "identity.verification.changed" and event.data.get("to") == "verified"
    )
    assert review_event.actor == "test:operator"
    assert review_event.data["reason"] == "manual documents accepted"


async def _seed_resource_commands(hub: IdentityHub) -> tuple[UUID, UUID]:
    space = hub.space("demo-large", "Large dataset")
    for identity_id, external_ref in (
        (RESOURCE_TARGET_ID, "resource-target"),
        (RESOURCE_LINKED_ID, "resource-linked"),
        (RESOURCE_DEPENDENT_ID, "resource-dependent"),
    ):
        await space.register(
            service=DEMO_SERVICE,
            external_ref=external_ref,
            id=identity_id,
            profile={"display_name": external_ref, "country": "UA"},
            lifecycle="ready",
            actor="test:seed",
        )
    await space.link(RESOURCE_TARGET_ID, "recovery", RESOURCE_LINKED_ID, actor="test:seed")
    await space.link(RESOURCE_DEPENDENT_ID, "parent", RESOURCE_TARGET_ID, actor="test:seed")
    await space.override(
        RESOURCE_TARGET_ID,
        Override("auth", "", "authenticated", reason="seed leaseable target", actor="test:seed"),
    )
    proxy_id, _ = await space.add_proxy("http://10.10.0.1:8080", pool="manual", max_identities=1)
    await space.override_proxy(proxy_id, "alive", reason="seed available proxy", actor="test:seed")
    other = hub.space("other-resource-space", "Other")
    foreign_proxy_id, _ = await other.add_proxy("http://10.10.0.2:8080", pool="manual")
    await other.override_proxy(foreign_proxy_id, "alive", reason="seed foreign proxy", actor="test:seed")
    async with space._tx() as tx:
        identity = await space._identity(tx, RESOURCE_TARGET_ID)
        await tx.acquire_lease(
            identity,
            pool="resource-live",
            holder="test:worker",
            ttl_seconds=300,
            idempotency_key=None,
            shared=False,
            proxy_id=None,
        )
    return proxy_id, foreign_proxy_id


def test_lease_proxy_and_link_commands_are_guarded_scoped_and_impact_aware() -> None:
    hub = create_demo_hub()
    proxy_id, foreign_proxy_id = asyncio.run(_seed_resource_commands(hub))
    store = cast(MemoryStore, hub.store)
    detail_url = f"/api/v1/spaces/demo-large/accounts/{RESOURCE_TARGET_ID}"
    with TestClient(create_admin_app(_settings(), hub=hub)) as client:
        lease_section = client.get(
            f"{detail_url}/leases", headers=_headers("operator", "read-resource-lease")
        ).json()["data"]
        assert {item["kind"] for item in lease_section["actions"]} == {"lease_revoke"}
        fencing_before = store._state.identities[RESOURCE_TARGET_ID].fencing_seq
        revoked = client.post(
            f"{detail_url}/commands/lease-revoke",
            headers=_headers("operator", "resource-revoke-0001"),
            json={
                "reason": "worker must stop immediately",
                "expected_revision": lease_section["revision"],
                "confirmation": _confirmation("increments_fencing", "worker_loses_lease"),
            },
        )
        assert revoked.status_code == 201 and revoked.json()["data"]["changed"] is True
        assert store._state.identities[RESOURCE_TARGET_ID].fencing_seq == fencing_before + 1

        network = client.get(
            f"{detail_url}/network", headers=_headers("operator", "read-resource-network")
        ).json()["data"]
        action_kinds = {item["kind"] for item in network["actions"]}
        assert {"link", "unlink", "proxy_rebind"} <= action_kinds
        rejected_unlink = client.post(
            f"{detail_url}/commands/unlink",
            headers=_headers("operator", "resource-unlink-no-confirm"),
            json={
                "reason": "remove recovery dependency",
                "expected_revision": network["revision"],
                "role": "recovery",
            },
        )
        assert rejected_unlink.status_code == 422
        removed = client.post(
            f"{detail_url}/commands/unlink",
            headers=_headers("operator", "resource-unlink-confirmed"),
            json={
                "reason": "remove recovery dependency",
                "expected_revision": network["revision"],
                "role": "recovery",
                "confirmation": _confirmation("affects_dependents"),
            },
        )
        assert removed.status_code == 201
        assert str(RESOURCE_DEPENDENT_ID) in removed.json()["data"]["affected_identity_ids"]

        revision = client.get(detail_url, headers=_headers("operator", "read-resource-before-rebind")).json()[
            "data"
        ]["revision"]
        foreign = client.post(
            f"{detail_url}/commands/proxy-rebind",
            headers=_headers("operator", "resource-foreign-rebind"),
            json={
                "reason": "must stay scoped",
                "expected_revision": revision,
                "proxy_id": str(foreign_proxy_id),
                "confirmation": _confirmation("changes_network_identity"),
            },
        )
        assert foreign.status_code == 404
        rebound = client.post(
            f"{detail_url}/commands/proxy-rebind",
            headers=_headers("operator", "resource-local-rebind"),
            json={
                "reason": "route through healthy endpoint",
                "expected_revision": revision,
                "proxy_id": str(proxy_id),
                "confirmation": _confirmation("changes_network_identity", "consumes_proxy_capacity"),
            },
        )
        assert rebound.status_code == 201 and rebound.json()["data"]["changed"] is True

        proxy_url = f"/api/v1/spaces/demo-large/proxies/{proxy_id}"
        proxy_detail = client.get(proxy_url, headers=_headers("admin", "read-resource-proxy")).json()["data"]
        assert {item["kind"] for item in proxy_detail["actions"]} == {"recheck", "override"}
        overridden = client.post(
            f"{proxy_url}/commands/override",
            headers=_headers("admin", "resource-proxy-override"),
            json={
                "reason": "manual incident quarantine",
                "status": "quarantined",
                "confirmation": _confirmation("overrides_proxy_health"),
            },
        )
        rechecked = client.post(
            f"{proxy_url}/commands/recheck",
            headers=_headers("operator", "resource-proxy-recheck"),
            json={"reason": "incident verification"},
        )
        assert overridden.status_code == 201
        assert rechecked.status_code == 201 and rechecked.json()["data"]["outcome"] == "alive"
