"""S6.4: safe files, content detection, and explicit seller-line templates."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from identity_hub import BrowserSession, Cookie, Cookies, FormatLossWarning, ValidationFailed
from identity_hub.formats.lines import load_account, load_proxy

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "formats"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _json(name: str) -> object:
    return json.loads(_text(name))


@pytest.mark.parametrize("fixture", ["netscape/curl.txt", "netscape/mozilla.txt"])
def test_netscape_fixtures_round_trip(fixture: str) -> None:
    cookies = Cookies.load.netscape(_text(fixture))
    assert Cookies.load.netscape(cookies.dump.netscape()) == cookies


def test_netscape_session_modes_http_only_and_tolerant_domain_flag() -> None:
    cookies = Cookies.load.netscape(_text("netscape/curl.txt"))
    sid = cookies.get("sid")
    host = cookies.get("host")
    assert sid is not None and sid.http_only and not sid.host_only
    assert host is not None and host.host_only and host.expires is None

    empty = Cookies([host]).dump.netscape(session_expires="empty")
    assert "\t\t" in empty
    mismatch = "example.test\tTRUE\t/\tFALSE\t0\tx\ty\r\n"
    assert Cookies.load.netscape(mismatch).root[0].host_only is False


def test_netscape_reports_losses() -> None:
    cookie = Cookie(name="sid", value="secret", domain="example.test", same_site="lax")
    with pytest.warns(FormatLossWarning, match="SameSite"):
        Cookies([cookie]).dump.netscape()
    with pytest.raises(ValidationFailed, match="SameSite"):
        Cookies([cookie]).dump.netscape(strict=True)


def test_har_fixture_uses_request_host_for_host_only_cookie() -> None:
    cookies = Cookies.load.har(_json("har/session.har.json"))
    sid = cookies.get("sid")
    domain = cookies.get("domain")
    assert sid is not None and sid.host_only and sid.domain == "example.test"
    assert domain is not None and not domain.host_only


@pytest.mark.parametrize(
    "fixture",
    [
        "cdp/cookies.json",
        "chrome_extension/cookies.json",
        "crawlee/cookies.json",
        "selenium/cookies.json",
        "tough_cookie/jar.json",
    ],
)
def test_cookie_autodetects_json_dialects(fixture: str) -> None:
    cookies = Cookies.load(_text(fixture))
    assert len(cookies) == 1


def test_autodetects_netscape_cookie_header_and_base64() -> None:
    assert Cookies.load(_text("netscape/curl.txt")).get("sid") is not None
    encoded = _text("autodetect/cookies.base64.txt")
    assert Cookies.load(encoded).get("sid") is not None
    header = Cookies.load("a=1; b=two=parts", url="https://example.test/path")
    assert header.header_for("https://example.test/") == "a=1; b=two=parts"
    with pytest.raises(ValidationFailed, match="параметр url"):
        Cookies.load("a=1")


def test_browser_session_autodetects_storage_state_and_canonical_json() -> None:
    state = _text("playwright/storage_state.json")
    expected = BrowserSession.load.playwright(json.loads(state))
    assert BrowserSession.load(state) == expected
    assert BrowserSession.load(base64.b64encode(state.encode())) == expected

    canonical = expected.dump.json()
    assert BrowserSession.load(canonical) == expected


@pytest.mark.parametrize("payload", [b"\x80\x04pickle", b"SQLite format 3\x00data"])
def test_unsafe_binary_formats_are_never_loaded(payload: bytes) -> None:
    with pytest.raises(ValidationFailed, match="не загружается"):
        Cookies.load(payload)


def test_unknown_format_lists_expected_safe_formats() -> None:
    with pytest.raises(ValidationFailed, match="Netscape"):
        Cookies.load("definitely not a cookie export")


def test_template_account_and_proxy_lines() -> None:
    encoded = _text("autodetect/cookies.base64.txt").strip()
    account = load_account(
        f"alice|p:ass|{encoded}",
        "login|password|cookies",
        sep=r"\|",
    )
    assert account["login"] == "alice"
    assert isinstance(account["cookies"], Cookies)

    proxy = load_proxy("proxy.test:1080:user:pass", "host:port:user:pass")
    assert proxy.dump.playwright() == {
        "server": "http://proxy.test:1080",
        "username": "user",
        "password": "pass",
    }
    octo = load_proxy("socks5;proxy.test;1080;user;pass", "protocol;host;port;login;password")
    assert octo.dump.httpx() == "socks5://user:pass@proxy.test:1080"
