"""S6.2: dependency-free lease helpers for Playwright and Selenium."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from pydantic import BaseModel
from tests.fixtures.services import FAKE_TG

from identity_hub import (
    BrowserFingerprint,
    BrowserSession,
    Cookie,
    Cookies,
    IdentityCreate,
    IdentityHub,
    SecretRef,
    SpaceHandle,
)

pytestmark = pytest.mark.conformance


class BrowserProfile(BaseModel):
    fingerprint: BrowserFingerprint


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    async def storage_state(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "cookies": [
                {
                    "name": "sid",
                    "value": "updated",
                    "domain": "example.com",
                    "path": "/",
                    "expires": -1,
                    "secure": False,
                    "httpOnly": True,
                }
            ],
            "origins": [],
        }

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.options: dict[str, Any] = {}
        self.context = FakeContext()

    async def new_context(self, **options: Any) -> FakeContext:
        self.options = options
        return self.context


class FakeDriver:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.cookies: list[dict[str, Any]] = []

    def get(self, url: str) -> None:
        self.urls.append(url)

    def add_cookie(self, cookie: dict[str, Any]) -> None:
        self.cookies.append(cookie)


async def test_browser_helpers_apply_and_persist_session(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    service = replace(
        FAKE_TG,
        name="browser_helpers",
        session=BrowserSession,
        profile=BrowserProfile,
        filterable_profile=frozenset({"fingerprint"}),
        searchable_profile=frozenset(),
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()
    identity = await space.identities.create(
        IdentityCreate(
            service=service,
            external_ref="browser-user",
            lifecycle="ready",
            profile={
                "fingerprint": {
                    "user_agent": "Test UA",
                    "browser": "chrome",
                    "browser_version": "130.0",
                    "os": "linux",
                    "locale": "en-US",
                    "viewport": {"width": 1200, "height": 800},
                }
            },
            session=BrowserSession(
                cookies=Cookies([Cookie(name="sid", value="initial", domain="example.com", http_only=True)])
            ),
        ),
        actor="test",
    )

    async with space.pools.define("browser", service=service, holder="worker").lease() as lease:
        browser = FakeBrowser()
        async with lease.playwright_context(browser) as context:
            assert context is browser.context
            assert browser.options["user_agent"] == "Test UA"
            assert browser.options["viewport"] == {"width": 1200, "height": 800}
            assert browser.options["storage_state"]["cookies"][0]["value"] == "initial"
        assert browser.context.closed

        driver = FakeDriver()
        await lease.selenium_apply(driver)
        assert driver.urls == ["https://example.com/"]
        assert driver.cookies[0]["value"] == "updated"

    saved = await space.secrets.reveal(
        identity.id, SecretRef.session(), actor="admin", reason="проверка browser helper"
    )
    assert saved["cookies"][0]["value"] == "updated"
