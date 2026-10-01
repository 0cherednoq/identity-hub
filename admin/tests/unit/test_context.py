from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from identity_hub.errors import NotFound
from litestar import Request, post
from litestar.datastructures import State
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from litestar.testing import TestClient
from pydantic import SecretStr
from tests.factories.api import register

from identity_hub_admin.api.routes import route_meta
from identity_hub_admin.app import create_admin_app
from identity_hub_admin.auth import AdminPrincipal
from identity_hub_admin.context import ensure_identity_in_space, resolve_space_context
from identity_hub_admin.demo import DEMO_SERVICE, create_demo_hub
from identity_hub_admin.idempotency import (
    IdempotencyConflict,
    IdempotencyCoordinator,
    IdempotencyResultConsumed,
)
from identity_hub_admin.settings import AdminSettings, AuthMode, Environment


@post(
    "/api/v1/spaces/{space_slug:str}/probe-command",
    operation_id="test_space_probe_command",
    opt=route_meta("operator", "admin", space_scoped=True),
    status_code=200,
)
async def scoped_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
) -> dict[str, str]:
    resolved = await resolve_space_context(request, state, space_slug)
    return {
        "actor": resolved.context.actor,
        "request_id": resolved.context.request_id,
        "space_slug": resolved.context.space_slug,
        "idempotency_key": resolved.context.idempotency_key,
    }


def scoped_settings() -> AdminSettings:
    tokens = {
        "operator-token": {
            "actor": "oidc:operator@example.test",
            "role": "operator",
            "allowed_spaces": ["demo-empty", "missing-but-allowed"],
        }
    }
    return AdminSettings(
        environment=Environment.TEST,
        auth_mode=AuthMode.TOKEN,
        bearer_tokens=SecretStr(json.dumps(tokens)),
        demo_mode=True,
        run_dispatcher=False,
    )


def test_space_guard_command_context_and_non_enumerating_not_found() -> None:
    app = create_admin_app(
        scoped_settings(),
        hub=create_demo_hub(),
        route_handlers=(scoped_command,),
    )
    headers = {
        "Authorization": "Bearer operator-token",
        "Idempotency-Key": "command:12345678",
        "X-Request-ID": "space-command-001",
    }

    with TestClient(app) as client:
        allowed = client.post("/api/v1/spaces/demo-empty/probe-command", headers=headers)
        forbidden = client.post("/api/v1/spaces/demo-large/probe-command", headers=headers)
        missing = client.post("/api/v1/spaces/missing-but-allowed/probe-command", headers=headers)
        no_key = client.post(
            "/api/v1/spaces/demo-empty/probe-command",
            headers={"Authorization": "Bearer operator-token"},
        )

    assert allowed.status_code == 200
    assert allowed.json() == {
        "actor": "oidc:operator@example.test",
        "request_id": "space-command-001",
        "space_slug": "demo-empty",
        "idempotency_key": "command:12345678",
    }
    assert "operator-token" not in allowed.text
    assert forbidden.status_code == missing.status_code == 404
    assert forbidden.json() == missing.json()
    assert no_key.status_code == 422


@pytest.mark.asyncio
async def test_get_space_does_not_create_and_object_lookup_stays_scoped() -> None:
    hub = create_demo_hub()
    await hub.start()
    alpha = hub.space("alpha", "Alpha")
    beta = hub.space("beta", "Beta")
    await beta.ref()
    registered = await register(
        alpha,
        service=DEMO_SERVICE,
        external_ref="alpha-only",
        profile={"display_name": "Alpha only", "country": "UA"},
    )

    assert await hub.get_space("alpha") is not None
    assert await hub.get_space("does-not-exist") is None
    with pytest.raises(NotFoundException):
        await ensure_identity_in_space(beta, registered.identity_id)
    with pytest.raises(NotFound):
        await beta.identities.card(registered.identity_id)


@pytest.mark.asyncio
async def test_idempotency_coalesces_replays_and_detects_conflicts() -> None:
    coordinator = IdempotencyCoordinator()
    calls = 0

    async def operation() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"operation": calls}

    async def execute(payload: object = None) -> dict[str, Any]:
        return await coordinator.execute(
            scope="actor:space",
            operation="test",
            key="request:12345678",
            payload=payload,
            call=operation,
        )

    first, concurrent = await asyncio.gather(execute({"value": 1}), execute({"value": 1}))
    replay = await execute({"value": 1})

    assert first == concurrent == replay == {"operation": 1}
    assert calls == 1
    with pytest.raises(IdempotencyConflict):
        await execute({"value": 2})


@pytest.mark.asyncio
async def test_failed_idempotent_call_can_be_retried() -> None:
    coordinator = IdempotencyCoordinator()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        return "recovered"

    async def execute() -> str:
        return await coordinator.execute(
            scope="actor:space",
            operation="retry",
            key="request:retry-1234",
            payload={"same": True},
            call=operation,
        )

    with pytest.raises(RuntimeError, match="transient"):
        await execute()
    assert await execute() == "recovered"
    assert calls == 2


@pytest.mark.asyncio
async def test_one_time_idempotent_result_is_delivered_once_without_reexecution() -> None:
    coordinator = IdempotencyCoordinator()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        return "sensitive-once"

    async def execute() -> str:
        return await coordinator.execute(
            scope="actor:space",
            operation="rotate",
            key="request:one-time",
            payload={"same": True},
            call=operation,
            one_time_result=True,
        )

    assert await execute() == "sensitive-once"
    with pytest.raises(IdempotencyResultConsumed):
        await execute()
    assert calls == 1
