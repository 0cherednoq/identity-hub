"""S6.2: browser cookie dialects and explicit conversion losses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from identity_hub import (
    BrowserSession,
    Cookie,
    Cookies,
    FormatLossWarning,
    OriginStorage,
    PartitionKey,
    ValidationFailed,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "formats"


def _read(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("format_name", "fixture"),
    [
        ("cdp", "cdp/cookies.json"),
        ("selenium", "selenium/cookies.json"),
        ("chrome_extension", "chrome_extension/cookies.json"),
        ("crawlee", "crawlee/cookies.json"),
        ("tough_cookie", "tough_cookie/jar.json"),
    ],
)
def test_browser_fixture_round_trips(format_name: str, fixture: str) -> None:
    loaded = getattr(Cookies.load, format_name)(_read(fixture))
    dumped = getattr(loaded.dump, format_name)()
    again = getattr(Cookies.load, format_name)(dumped)
    assert again == loaded
    assert loaded.get("sid") is not None


def test_playwright_storage_state_round_trip_and_losses() -> None:
    session = BrowserSession.load.playwright(_read("playwright/storage_state.json"))
    assert BrowserSession.load.playwright(session.dump.playwright()) == session

    with pytest.warns(FormatLossWarning, match="sessionStorage"):
        BrowserSession(
            origins=[OriginStorage(origin="https://example.com", session_storage={"draft": "x"})]
        ).dump.playwright()
    with pytest.raises(ValidationFailed, match="sessionStorage"):
        BrowserSession(
            origins=[OriginStorage(origin="https://example.com", session_storage={"draft": "x"})]
        ).dump.playwright(strict=True)


def test_cdp_partition_and_extras_are_preserved() -> None:
    cookie = Cookie(
        name="partitioned",
        value="secret",
        domain="example.com",
        secure=True,
        partition_key=PartitionKey(top_level_site="https://top.example", has_cross_site_ancestor=False),
        extras={"priority": "High", "sourceScheme": "Secure"},
    )
    (row,) = Cookies([cookie]).dump.cdp()
    assert row["partitionKey"] == {
        "topLevelSite": "https://top.example",
        "hasCrossSiteAncestor": False,
    }
    loaded = Cookies.load.cdp([row]).root[0]
    assert loaded.partition_key == cookie.partition_key
    assert loaded.extras == cookie.extras
    assert Cookies([cookie]).dump.puppeteer()[0]["partitionKey"]["sourceOrigin"] == "https://top.example"


def test_selenium_skips_partitioned_cookie_and_uses_integer_expiry() -> None:
    cookie = Cookie(
        name="p",
        value="secret",
        domain="example.com",
        secure=True,
        expires=123.9,
        partition_key=PartitionKey(top_level_site="https://top.example"),
    )
    with pytest.warns(FormatLossWarning, match="partitioned"):
        assert Cookies([cookie]).dump.selenium() == []
    with pytest.raises(ValidationFailed, match="partitioned"):
        Cookies([cookie]).dump.selenium(strict=True)

    ordinary = cookie.model_copy(update={"partition_key": None})
    assert Cookies([ordinary]).dump.selenium()[0]["expiry"] == 123


def test_playwright_cookie_partition_key_is_string() -> None:
    cookie = Cookie(
        name="p",
        value="secret",
        domain="example.com",
        secure=True,
        partition_key=PartitionKey(top_level_site="https://top.example"),
    )
    dumped = Cookies([cookie]).dump.playwright()
    assert dumped[0]["partitionKey"] == "https://top.example"
    assert Cookies.load.playwright(dumped) == Cookies([cookie])
