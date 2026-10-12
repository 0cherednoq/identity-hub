from __future__ import annotations

import asyncio
import json
from typing import cast
from uuid import UUID

import pytest
from identity_hub.clock import FakeClock
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub
from identity_hub.testing.memory import MemoryStore
from litestar import Litestar
from litestar.testing import TestClient
from pydantic import SecretStr
from tests.factories.api import register

from identity_hub_admin.api.bulk import _safe_cell
from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import DEMO_SERVICE
from identity_hub_admin.settings import AdminSettings, Environment

FIRST = UUID("00000000-0000-0000-0000-000000003601")
SECOND = UUID("00000000-0000-0000-0000-000000003602")
LATE = UUID("00000000-0000-0000-0000-000000003603")


@pytest.mark.parametrize("value", ["=1+1", "+cmd", "-2+3", "@SUM(A1:A2)", "  =cmd", "\t=cmd"])
def test_csv_formula_cells_are_neutralized(value: str) -> None:
    assert _safe_cell(value).startswith("'")


def _settings() -> AdminSettings:
    tokens = {
        f"{role}-bulk-token": {
            "actor": f"bulk:{role}",
            "role": role,
            "allowed_spaces": ["bulk-space"],
        }
        for role in ("viewer", "operator", "admin")
    }
    return AdminSettings(
        environment=Environment.TEST,
        run_dispatcher=False,
        bearer_tokens=SecretStr(json.dumps(tokens)),
    )


def _headers(role: str, key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {role}-bulk-token",
        "Idempotency-Key": key,
    }


def _hub() -> IdentityHub:
    return IdentityHub(
        secrets=StaticSecretsProvider({"test": b"0123456789abcdef0123456789abcdef"}, "test"),
        services=[DEMO_SERVICE],
        store=MemoryStore(FakeClock()),
        strict_events=True,
    )


async def _seed(hub: IdentityHub) -> None:
    space = hub.space("bulk-space", "Bulk", create=True)
    for identity_id, external_ref in ((FIRST, "=2+2"), (SECOND, "second")):
        await register(
            space,
            service=DEMO_SERVICE,
            external_ref=external_ref,
            id=identity_id,
            profile={"display_name": external_ref, "country": "UA"},
            tags=["selected"],
            lifecycle="ready",
            actor="test:seed",
        )


def _preview(client: TestClient[Litestar], key: str = "bulk-preview-0001") -> dict[str, object]:
    response = client.post(
        "/api/v1/spaces/bulk-space/bulk/preview",
        headers=_headers("operator", key),
        json={
            "reason": "scheduled tagging",
            "filter": {"tag": "selected"},
            "operation": {"kind": "tag_add", "tags": ["processed"]},
            "limit": 100,
        },
    )
    assert response.status_code == 201
    return cast(dict[str, object], response.json()["data"])


def test_bulk_snapshot_freezes_filter_and_retry_skips_successes() -> None:
    hub = _hub()
    asyncio.run(_seed(hub))
    space = hub.space("bulk-space", create=True)
    app = create_admin_app(_settings(), hub=hub)
    with TestClient(app) as client:
        preview = _preview(client)
        assert preview["selected_count"] == 2
        asyncio.run(
            register(
                space,
                service=DEMO_SERVICE,
                external_ref="late",
                id=LATE,
                profile={"display_name": "Late", "country": "UA"},
                tags=["selected"],
                lifecycle="ready",
                actor="test:late",
            )
        )
        asyncio.run(space.identities.add_tags(SECOND, ["changed-after-preview"], actor="test:race"))
        payload = {
            "reason": "apply frozen selection",
            "snapshot_id": preview["snapshot_id"],
            "confirmation": {
                "confirmed": True,
                "consequence_codes": ["changes_many_accounts"],
            },
        }
        first = client.post(
            "/api/v1/spaces/bulk-space/bulk/execute",
            headers=_headers("operator", "bulk-execute-0001"),
            json=payload,
        )
        retried = client.post(
            "/api/v1/spaces/bulk-space/bulk/execute",
            headers=_headers("operator", "bulk-execute-0002"),
            json=payload,
        )
    assert first.status_code == 201
    assert (
        first.json()["data"]
        | {
            "succeeded": 1,
            "failed": 1,
            "complete": False,
        }
        == first.json()["data"]
    )
    assert retried.json()["data"]["skipped_already_succeeded"] == 1
    assert retried.json()["data"]["succeeded"] == 1
    assert "processed" in asyncio.run(space.identities.card(FIRST)).identity.tags
    assert "processed" not in asyncio.run(space.identities.card(SECOND)).identity.tags
    assert "processed" not in asyncio.run(space.identities.card(LATE)).identity.tags


def test_exports_escape_csv_formulas_are_audited_and_viewer_is_denied() -> None:
    hub = _hub()
    asyncio.run(_seed(hub))
    store = cast(MemoryStore, hub._store)
    app = create_admin_app(_settings(), hub=hub)
    payload = {
        "reason": "support export",
        "source": "accounts",
        "format": "csv",
        "account_filter": {"tag": "selected"},
        "limit": 100,
    }
    with TestClient(app) as client:
        denied = client.post(
            "/api/v1/spaces/bulk-space/exports",
            headers=_headers("viewer", "viewer-export-0001"),
            json=payload,
        )
        exported = client.post(
            "/api/v1/spaces/bulk-space/exports",
            headers=_headers("operator", "account-export-0001"),
            json=payload,
        )
        events = client.post(
            "/api/v1/spaces/bulk-space/exports",
            headers=_headers("operator", "event-export-0001"),
            json={
                "reason": "incident timeline",
                "source": "events",
                "format": "json",
                "event_filter": {"group": "identity"},
                "limit": 100,
            },
        )
    assert denied.status_code == 403
    assert exported.status_code == 201
    assert "no-store" in exported.headers["cache-control"]
    assert "'=2+2" in exported.content.decode("utf-8-sig")
    assert events.status_code == 201
    event_rows = events.json()
    assert event_rows and all(row["group"] == "identity" for row in event_rows)
    assert "ciphertext" not in events.text
    audits = [event for event, _, _ in store._state.events if event.type == "admin.accounts.exported"]
    assert len(audits) == 1
    assert audits[0].data["count"] == 2
    assert "=2+2" not in json.dumps(audits[0].data)
    assert len([event for event, _, _ in store._state.events if event.type == "admin.events.exported"]) == 1


def test_bulk_lifecycle_runs_as_one_batch_and_reports_each_account() -> None:
    """C2.2b: массовая смена жизненного цикла идёт пакетной командой ядра; отчёт прежнего формата."""
    hub = _hub()
    asyncio.run(_seed(hub))
    space = hub.space("bulk-space", create=True)
    app = create_admin_app(_settings(), hub=hub)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/spaces/bulk-space/bulk/preview",
            headers=_headers("operator", "bulk-lifecycle-preview"),
            json={
                "reason": "retire the batch",
                "filter": {"tag": "selected"},
                "operation": {"kind": "lifecycle", "target": "retired"},
                "limit": 100,
            },
        )
        assert response.status_code == 201
        snapshot_id = response.json()["data"]["snapshot_id"]
        asyncio.run(space.identities.add_tags(SECOND, ["changed-after-preview"], actor="test:race"))
        payload = {
            "reason": "retire the batch",
            "snapshot_id": snapshot_id,
            "confirmation": {"confirmed": True, "consequence_codes": ["changes_many_accounts"]},
        }
        first = client.post(
            "/api/v1/spaces/bulk-space/bulk/execute",
            headers=_headers("operator", "bulk-lifecycle-execute"),
            json=payload,
        )
    report = first.json()["data"]
    assert first.status_code == 201
    assert (report["total"], report["succeeded"], report["failed"], report["complete"]) == (2, 1, 1, False)
    assert report["failures"] == [
        {"identity_id": str(SECOND), "code": "revision_mismatch", "message": "account changed after preview"}
    ]
    assert asyncio.run(space.identities.get(FIRST)).lifecycle == "retired"
    assert asyncio.run(space.identities.get(SECOND)).lifecycle == "ready"


def test_bulk_lifecycle_keeps_domain_failure_codes() -> None:
    """C2.2b: отказ команды ядра приходит в отчёт прежним кодом — именем класса ошибки."""
    hub = _hub()
    asyncio.run(_seed(hub))
    space = hub.space("bulk-space", create=True)
    asyncio.run(space.identities.retire(SECOND, actor="test:seed", reason="seed"))
    app = create_admin_app(_settings(), hub=hub)
    with TestClient(app) as client:
        preview = client.post(
            "/api/v1/spaces/bulk-space/bulk/preview",
            headers=_headers("operator", "bulk-activate-preview"),
            json={
                "reason": "activate the batch",
                "filter": {"tag": "selected"},
                "operation": {"kind": "lifecycle", "target": "provisioning"},
                "limit": 100,
            },
        ).json()["data"]
        executed = client.post(
            "/api/v1/spaces/bulk-space/bulk/execute",
            headers=_headers("operator", "bulk-activate-execute"),
            json={
                "reason": "activate the batch",
                "snapshot_id": preview["snapshot_id"],
                "confirmation": {"confirmed": True, "consequence_codes": ["changes_many_accounts"]},
            },
        ).json()["data"]
    # ready и retired в provisioning не возвращаются: обе строки — отказ ядра
    assert (executed["succeeded"], executed["failed"]) == (0, 2)
    assert {item["code"] for item in executed["failures"]} == {"transitionnotallowed"}
    assert {item["message"] for item in executed["failures"]} == {"domain command rejected"}
    # отказ пакетной команды ядра приходит стабильным кодом и переводится в тот же формат
    from identity_hub_admin.api.bulk import _failure_name

    assert _failure_name("invalid_transition") == "transitionnotallowed"
    assert _failure_name("not_found") == "notfound"
