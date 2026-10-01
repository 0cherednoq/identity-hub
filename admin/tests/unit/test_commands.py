from __future__ import annotations

import asyncio
import json
from uuid import UUID

from litestar import Request, post
from litestar.datastructures import State
from litestar.params import FromPath
from litestar.testing import TestClient
from pydantic import SecretStr
from tests.factories.api import register

from identity_hub_admin.api.contracts import ContractModel, MutationContext
from identity_hub_admin.api.routes import error_responses, route_meta
from identity_hub_admin.app import create_admin_app
from identity_hub_admin.auth import AdminPrincipal
from identity_hub_admin.commands import CommandGateway, CommandScope
from identity_hub_admin.demo import DEMO_SERVICE, create_demo_hub
from identity_hub_admin.settings import AdminSettings, Environment

IDENTITY_ID = UUID("00000000-0000-0000-0000-000000000301")


class NoteCommand(MutationContext):
    note: str


class NoteResult(ContractModel):
    revision: int
    note: str | None
    actor: str
    reason: str
    dry_run: bool


@post(
    "/api/v1/spaces/{space_slug:str}/identities/{identity_id:uuid}/note-command",
    operation_id="test_note_command",
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def note_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: NoteCommand,
) -> NoteResult:
    gateway: CommandGateway = state.command_gateway

    async def apply(scope: CommandScope) -> NoteResult:
        if not scope.dry_run:
            await scope.handle.identities.set_note(identity_id, data.note, actor=scope.actor)
        card = await scope.handle.identities.card(identity_id)
        return NoteResult(
            revision=card.revision,
            note=data.note if scope.dry_run else card.identity.note,
            actor=scope.actor,
            reason=scope.reason,
            dry_run=scope.dry_run,
        )

    return await gateway.execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.note.set",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload={"note": data.note},
    )


def _settings() -> AdminSettings:
    return AdminSettings(
        environment=Environment.TEST,
        run_dispatcher=False,
        bearer_tokens=SecretStr(
            json.dumps(
                {
                    "operator-token": {
                        "actor": "test:operator",
                        "role": "operator",
                        "allowed_spaces": ["command-space"],
                    },
                    "viewer-token": {
                        "actor": "test:viewer",
                        "role": "viewer",
                        "allowed_spaces": ["command-space"],
                    },
                }
            )
        ),
    )


def test_gateway_replays_maps_revision_conflict_and_rejects_viewer() -> None:
    hub = create_demo_hub()
    asyncio.run(
        register(
            hub.space("command-space", "Command space"),
            service=DEMO_SERVICE,
            external_ref="command-account",
            id=IDENTITY_ID,
            profile={"display_name": "Command account", "country": "UA"},
        )
    )
    operator = {
        "Authorization": "Bearer operator-token",
        "Idempotency-Key": "note-command-0001",
    }
    body = {"reason": "support request", "expected_revision": 1, "note": "checked"}
    with TestClient(create_admin_app(_settings(), hub=hub, route_handlers=(note_command,))) as client:
        first = client.post(
            f"/api/v1/spaces/command-space/identities/{IDENTITY_ID}/note-command",
            headers=operator,
            json=body,
        )
        replay = client.post(
            f"/api/v1/spaces/command-space/identities/{IDENTITY_ID}/note-command",
            headers=operator,
            json=body,
        )
        conflict = client.post(
            f"/api/v1/spaces/command-space/identities/{IDENTITY_ID}/note-command",
            headers=operator,
            json={**body, "note": "different"},
        )
        stale = client.post(
            f"/api/v1/spaces/command-space/identities/{IDENTITY_ID}/note-command",
            headers={**operator, "Idempotency-Key": "note-command-0002"},
            json={**body, "note": "stale"},
        )
        viewer = client.post(
            f"/api/v1/spaces/command-space/identities/{IDENTITY_ID}/note-command",
            headers={
                "Authorization": "Bearer viewer-token",
                "Idempotency-Key": "note-command-viewer",
            },
            json=body,
        )

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert first.json()["revision"] == 2
    assert first.json()["actor"] == "test:operator"
    assert first.json()["reason"] == "support request"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert stale.status_code == 412
    assert stale.json()["error"]["details"] == {
        "expected_revision": 1,
        "current_revision": 2,
    }
    assert viewer.status_code == 403
