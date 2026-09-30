from __future__ import annotations

import asyncio
import json
from typing import cast

from identity_hub.clock import FakeClock
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub
from identity_hub.testing.memory import MemoryStore
from litestar.testing import TestClient
from pydantic import SecretStr

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.demo import DEMO_SERVICE
from identity_hub_admin.settings import AdminSettings, Environment


def _settings() -> AdminSettings:
    tokens = {
        f"{role}-import-token": {
            "actor": f"imports:{role}",
            "role": role,
            "allowed_spaces": ["import-space"],
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
        "Authorization": f"Bearer {role}-import-token",
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
    await hub.space("import-space", "Imports").register(
        service=DEMO_SERVICE,
        external_ref="existing",
        profile={"display_name": "Existing", "country": "UA"},
        actor="test:seed",
    )


def _payload(content: str, *, format: str = "jsonl") -> dict[str, object]:
    return {
        "reason": "approved migration batch",
        "service": DEMO_SERVICE.name,
        "source": "C:\\private\\batch.jsonl",
        "format": format,
        "content": content,
        "on_existing": "skip",
        "verify_sessions": False,
        "labels": {"batch": "september"},
    }


def test_jsonl_import_is_idempotent_resumable_and_secret_safe() -> None:
    hub = _hub()
    asyncio.run(_seed(hub))
    store = cast(MemoryStore, hub.store)
    secret = "never-return-this-session-secret"
    content = "\n".join(
        (
            json.dumps(
                {
                    "external_ref": "new-account",
                    "profile": {"display_name": "New", "country": "PL"},
                    "note": secret,
                }
            ),
            json.dumps(
                {
                    "external_ref": "existing",
                    "profile": {"display_name": "Existing", "country": "UA"},
                }
            ),
        )
    )
    app = create_admin_app(_settings(), hub=hub)
    with TestClient(app) as client:
        denied = client.post(
            "/api/v1/spaces/import-space/imports",
            headers=_headers("viewer", "viewer-import-0001"),
            json=_payload(content),
        )
        first = client.post(
            "/api/v1/spaces/import-space/imports",
            headers=_headers("operator", "operator-import-0001"),
            json=_payload(content),
        )
        replay = client.post(
            "/api/v1/spaces/import-space/imports",
            headers=_headers("operator", "operator-import-0001"),
            json=_payload(content),
        )
        run_id = first.json()["data"]["id"]
        assert asyncio.run(hub.import_runner.run_once()) == 2
        detail = client.get(
            f"/api/v1/spaces/import-space/imports/{run_id}",
            headers={"Authorization": "Bearer viewer-import-token"},
        )
        listing = client.get(
            "/api/v1/spaces/import-space/imports",
            headers={"Authorization": "Bearer viewer-import-token"},
        )

    assert denied.status_code == 403
    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert (
        first.json()["data"]
        | {
            "source": "batch.jsonl",
            "state": "pending",
            "total": 2,
            "processed": 0,
        }
        == first.json()["data"]
    )
    assert detail.status_code == 200
    assert (
        detail.json()["data"]
        | {
            "state": "succeeded",
            "processed": 2,
            "created": 1,
            "skipped": 1,
            "failed": 0,
        }
        == detail.json()["data"]
    )
    assert len(detail.json()["data"]["created_ids"]) == 1
    assert detail.json()["data"]["skipped_items"][0]["external_ref"] == "existing"
    assert listing.status_code == 200
    assert listing.json()["data"]["limits"]["max_records"] == 10_000
    assert secret not in detail.text
    assert "content" not in detail.text
    audits = [event for event, _, _ in store._state.events if event.type == "admin.import.changed"]
    assert len(audits) == 1
    assert audits[0].data["action"] == "started"
    assert "source" not in audits[0].data
    assert secret not in json.dumps(audits[0].data)


def test_import_validation_reports_safe_line_and_csv_supports_structured_fields() -> None:
    hub = _hub()
    asyncio.run(_seed(hub))
    app = create_admin_app(_settings(), hub=hub)
    bad_secret = "do-not-reflect-this-value"
    with TestClient(app) as client:
        malformed = client.post(
            "/api/v1/spaces/import-space/imports",
            headers=_headers("operator", "invalid-import-0001"),
            json=_payload('{"external_ref":"ok"}\n{"credentials":"' + bad_secret),
        )
        unknown_column = client.post(
            "/api/v1/spaces/import-space/imports",
            headers=_headers("operator", "invalid-import-0002"),
            json=_payload("external_ref,unknown\nref,value\n", format="csv"),
        )
        csv_result = client.post(
            "/api/v1/spaces/import-space/imports",
            headers=_headers("operator", "csv-import-0001"),
            json=_payload(
                'external_ref,profile,tags\ncsv-account,"{""display_name"":""CSV"",""country"":""DE""}",one;two\n',
                format="csv",
            ),
        )

    assert malformed.status_code == 422
    assert "line 2" in malformed.json()["error"]["message"]
    assert bad_secret not in malformed.text
    assert unknown_column.status_code == 422
    assert "unknown columns" in unknown_column.json()["error"]["message"]
    assert csv_result.status_code == 201
    assert csv_result.json()["data"]["total"] == 1


def test_pending_import_can_be_cancelled_at_a_batch_boundary() -> None:
    hub = _hub()
    asyncio.run(_seed(hub))
    app = create_admin_app(_settings(), hub=hub)
    content = json.dumps(
        {
            "external_ref": "will-not-run",
            "profile": {"display_name": "Pending", "country": "US"},
        }
    )
    with TestClient(app) as client:
        started = client.post(
            "/api/v1/spaces/import-space/imports",
            headers=_headers("operator", "cancel-import-0001"),
            json=_payload(content),
        )
        run_id = started.json()["data"]["id"]
        cancelled = client.post(
            f"/api/v1/spaces/import-space/imports/{run_id}/cancel",
            headers=_headers("operator", "cancel-import-0002"),
            json={"reason": "operator stopped the batch"},
        )
        assert asyncio.run(hub.import_runner.run_once()) == 0

    assert started.status_code == 201
    assert cancelled.status_code == 201
    assert cancelled.json()["data"]["state"] == "cancelled"
    assert cancelled.json()["data"]["processed"] == 0
