"""S6.1: разные Pydantic-схемы сессии для разных слотов на обоих Store."""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.fixtures.services import FAKE_TG

from identity_hub import (
    ApiKey,
    BrowserSession,
    Cookie,
    Cookies,
    CredentialInput,
    IdentityCreate,
    IdentityHub,
    PasswordCredentials,
    SecretRef,
    SpaceHandle,
    TokenSession,
    ValidationFailed,
)

pytestmark = pytest.mark.conformance


async def test_session_slots_validate_store_and_read_own_schemas(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    service = replace(
        FAKE_TG,
        name="format_slots",
        session=BrowserSession,
        session_slots={"api": TokenSession},
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()
    browser = BrowserSession(
        cookies=Cookies([Cookie(name="sid", value="browser-secret", domain="example.com")])
    )
    token = TokenSession(access_token="token-secret")
    identity = await space.identities.create(
        IdentityCreate(
            service=service,
            external_ref="slot-user",
            lifecycle="ready",
            sessions={"default": browser, "api": token},
        ),
        actor="test",
    )

    async with space.pools.define("slots", service=service, holder="worker").lease() as lease:
        assert isinstance(await lease.session(), BrowserSession)
        api = await lease.session("api")
        assert isinstance(api, TokenSession)
        assert api.access_token.get_secret_value() == "token-secret"
        await lease.save_session(TokenSession(access_token="next-token"), "api")
        with pytest.raises(ValidationFailed):
            await lease.save_session(browser, "api")

    revealed = await space.secrets.reveal(
        identity.id, SecretRef.session("api"), actor="admin", reason="проверка"
    )
    assert revealed["access_token"] == "next-token"


async def test_standard_credentials_are_service_schemas(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    service = replace(
        FAKE_TG,
        name="format_credentials",
        credentials=PasswordCredentials,
        credential_kinds={"api": ApiKey},
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()
    await space.identities.create(
        IdentityCreate(
            service=service,
            external_ref="credential-user",
            lifecycle="ready",
            credentials={"login": "alice", "password": "password-secret"},
            session={"auth_key": "session-secret"},
            credential_records=[CredentialInput("api", {"key": "api-secret"})],
        ),
        actor="test",
    )
    async with space.pools.define("credentials", service=service, holder="worker").lease() as lease:
        credentials = await lease.credentials()
        api = await lease.credential("api")
        assert isinstance(credentials, PasswordCredentials)
        assert isinstance(api, ApiKey)
        assert credentials.password.get_secret_value() == "password-secret"
        assert api.key.get_secret_value() == "api-secret"
