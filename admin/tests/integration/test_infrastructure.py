from __future__ import annotations

import pytest
from identity_hub.hub import IdentityHub
from litestar import Litestar
from litestar.testing import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.factories import AdminPrincipalFixture, SeededSpaces
from tests.fakes import RecordingTelegramTransport, RecordingWebhookTransport

from identity_hub_admin.app import create_admin_app
from identity_hub_admin.runtime import AdminRuntime
from identity_hub_admin.settings import AdminSettings, Environment

pytestmark = pytest.mark.integration


async def test_spaces_are_isolated_by_library_read_contract(seeded_spaces: SeededSpaces) -> None:
    alpha_page = await seeded_spaces.alpha.search()
    beta_page = await seeded_spaces.beta.search()

    assert {item.id for item in alpha_page.items} == set(seeded_spaces.alpha_ids)
    assert {item.id for item in beta_page.items} == set(seeded_spaces.beta_ids)
    assert set(seeded_spaces.alpha_ids).isdisjoint(item.id for item in beta_page.items)


def test_all_three_roles_have_stable_fixtures(
    principals: dict[str, AdminPrincipalFixture],
) -> None:
    assert set(principals) == {"viewer", "operator", "admin"}
    assert all(principal.allowed_spaces == {"alpha", "beta"} for principal in principals.values())


def test_http_client_preserves_request_id(http_client: TestClient[Litestar], request_id: str) -> None:
    response = http_client.get("/health/live")

    assert response.status_code == 200, f"request_id={request_id} body={response.text}"
    assert response.headers["X-Request-ID"] == request_id


async def test_fake_transports_record_without_network(
    telegram_transport: RecordingTelegramTransport,
    webhook_transport: RecordingWebhookTransport,
) -> None:
    from pydantic import SecretStr

    await telegram_transport.send_message(SecretStr("test"), 42, "message", thread_id=7)
    status = await webhook_transport.post("https://example.test/hook", b"{}", {"X-Test": "1"})

    assert telegram_transport.messages[0].text == "message"
    assert webhook_transport.requests[0].url == "https://example.test/hook"
    assert status == 204


async def test_database_revision_readiness_check(db: AsyncEngine) -> None:
    runtime = AdminRuntime(
        settings=AdminSettings(environment=Environment.TEST),
        engine=db,
    )

    await runtime.check_database_revision()

    assert runtime.state.checks["database"].isdigit()


async def test_app_lifecycle_starts_injected_hub(hub: IdentityHub) -> None:
    settings = AdminSettings(environment=Environment.TEST, run_dispatcher=False)
    app = create_admin_app(settings, hub=hub)

    async with app.lifespan():
        assert app.state.runtime.state.ready is True
        assert app.state.runtime.state.checks["hub"] == "ready"

    assert app.state.runtime.state.ready is False
