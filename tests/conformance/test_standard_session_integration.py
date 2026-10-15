"""S6.6: standard browser sessions integrated with writes, imports, and pool filters."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import BaseModel

from identity_hub import (
    BrowserFingerprint,
    BrowserSession,
    Cookie,
    Cookies,
    IdentityCreate,
    IdentityHub,
    ImportRecord,
    Service,
    SpaceHandle,
    ValidationFailed,
)

pytestmark = pytest.mark.conformance


class BrowserProfile(BaseModel):
    fingerprint: BrowserFingerprint


def _fingerprint(browser: str) -> dict[str, object]:
    if browser == "chrome":
        return {
            "user_agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/130.0 Safari/537.36",
            "browser": "chrome",
            "browser_version": "130.0",
            "os": "linux",
        }
    return {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Firefox/128.0",
        "browser": "firefox",
        "browser_version": "128.0",
        "os": "linux",
    }


def _session(value: str, expires: float) -> BrowserSession:
    return BrowserSession(
        cookies=Cookies(
            [
                Cookie(
                    name="sid",
                    value=value,
                    domain="example.test",
                    host_only=True,
                    expires=expires,
                )
            ]
        )
    )


def _expiry(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)


async def _session_expiry(space: SpaceHandle, identity_id: UUID) -> datetime | None:
    access = await space.secrets.describe(identity_id)
    return next(item.expires_at for item in access.sessions if item.slot == "default")


def test_nested_filter_rejects_partially_encrypted_profile() -> None:
    with pytest.raises(ValidationFailed) as caught:
        Service(
            name="encrypted_nested_filter",
            title="Encrypted nested filter",
            profile=BrowserProfile,
            filterable_profile=frozenset({"fingerprint.browser"}),
            profile_encrypted=True,
        )
    assert any("profile_encrypted" in error for error in caught.value.errors)


async def test_browser_session_expiry_import_and_nested_pool_filter(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    service = Service(
        name="standard_browser_session",
        title="Standard browser session",
        profile=BrowserProfile,
        session=BrowserSession,
        session_auth_cookies=frozenset({"sid"}),
        filterable_profile=frozenset({"fingerprint.browser"}),
    )
    hub_under_test.register_service(service)
    await hub_under_test.start()

    chrome = await space.identities.create(
        IdentityCreate(
            service=service,
            external_ref="chrome-user",
            lifecycle="ready",
            profile={"fingerprint": _fingerprint("chrome")},
            session=_session("created", 2_000_000_000),
        ),
        actor="test",
    )
    await space.identities.create(
        IdentityCreate(
            service=service,
            external_ref="firefox-user",
            lifecycle="ready",
            profile={"fingerprint": _fingerprint("firefox")},
            session=_session("firefox", 2_000_000_100),
        ),
        actor="test",
    )
    assert await _session_expiry(space, chrome.id) == _expiry(2_000_000_000)

    pool = space.pools.define("nested-fingerprint", service=service, holder="worker")
    async with pool.lease(profile={"fingerprint.browser": "chrome"}) as lease:
        assert lease.identity_id == chrome.id
        await lease.save_session(_session("leased", 2_000_000_200))
    assert await _session_expiry(space, chrome.id) == _expiry(2_000_000_200)

    explicit = _expiry(2_100_000_000)
    await space.secrets.replace_session(
        chrome.id,
        _session("admin", 2_000_000_300),
        actor="admin",
        expires_at=explicit,
    )
    assert await _session_expiry(space, chrome.id) == explicit

    direct_cookies = Cookies.load(
        [
            {
                "name": "sid",
                "value": "cookies-import",
                "domain": "example.test",
                "hostOnly": True,
                "expires": 2_000_000_400,
            }
        ]
    )
    report = await space.imports.run(
        service=service,
        records=[
            ImportRecord(
                external_ref="browser-session-import",
                profile={"fingerprint": _fingerprint("chrome")},
                sessions={"default": _session("model-import", 2_000_000_350)},
            ),
            ImportRecord(
                external_ref="cookies-import",
                profile={"fingerprint": _fingerprint("chrome")},
                sessions={"default": direct_cookies},
            ),
        ],
        actor="test",
    )
    assert report.failed == [] and len(report.created) == 2
    assert await _session_expiry(space, report.created[0]) == _expiry(2_000_000_350)
    assert await _session_expiry(space, report.created[1]) == _expiry(2_000_000_400)

    run_id = await space.imports.stage(
        service=service,
        records=[
            ImportRecord(
                external_ref="staged-cookies-import",
                profile={"fingerprint": _fingerprint("chrome")},
                sessions={"default": direct_cookies},
            )
        ],
        source="cookies.json",
        actor="test",
    )
    assert (await space.imports.get(run_id)).total == 1
    assert await space.imports.cancel(run_id) is True
