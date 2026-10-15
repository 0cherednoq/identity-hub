"""S6.5: browser fingerprint generator and client formats."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from identity_hub import BrowserFingerprint, FormatLossWarning, ProxyUrl, ValidationFailed
from identity_hub.formats.fingerprint import IMPERSONATION_TARGETS

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "formats"


def _read(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_browserforge_fixture_loads_and_preserves_raw() -> None:
    raw = _read("browserforge/chrome.json")
    fingerprint = BrowserFingerprint.load.browserforge(raw)
    assert fingerprint.source == "browserforge" and fingerprint.raw == raw
    assert (fingerprint.browser, fingerprint.browser_version, fingerprint.os) == (
        "chrome",
        "130.0.6723.92",
        "windows",
    )
    assert fingerprint.user_agent_data is not None
    assert fingerprint.user_agent_data.full_version_list[0]["version"] == "130.0.6723.92"
    assert fingerprint.screen is not None and fingerprint.screen.avail_height == 1040


def test_fingerprint_suite_fixture_loads_headers_outside_fingerprint() -> None:
    raw = _read("fingerprint_suite/firefox.json")
    fingerprint = BrowserFingerprint.load.fingerprint_suite(raw)
    assert fingerprint.source == "fingerprint-suite" and fingerprint.raw == raw
    assert fingerprint.browser == "firefox" and fingerprint.os == "linux"
    assert fingerprint.headers == {"Accept-Language": "de-DE,de;q=0.9"}


def test_playwright_context_and_antidetect_shapes() -> None:
    fingerprint = BrowserFingerprint.load.browserforge(_read("browserforge/chrome.json"))
    proxy = ProxyUrl("http://user:pass@proxy.test:8080")
    context = fingerprint.dump.playwright_context(proxy=proxy)
    assert context["user_agent"] == fingerprint.user_agent
    assert context["viewport"] == {"width": 1280, "height": 720}
    assert context["screen"] == {"width": 1920, "height": 1080}
    assert context["proxy"] == {
        "server": "http://proxy.test:8080",
        "username": "user",
        "password": "pass",
    }
    assert fingerprint.dump.adspower()["screen_resolution"] == "1920x1080"
    assert fingerprint.dump.gologin()["resolution"] == "1920x1080"


def test_camoufox_warns_for_non_firefox() -> None:
    chrome = BrowserFingerprint.load.browserforge(_read("browserforge/chrome.json"))
    with pytest.warns(FormatLossWarning, match="Firefox"):
        assert chrome.dump.camoufox()["navigator.userAgent"] == chrome.user_agent
    with pytest.raises(ValidationFailed, match="Firefox"):
        chrome.dump.camoufox(strict=True)

    firefox = BrowserFingerprint.load.fingerprint_suite(_read("fingerprint_suite/firefox.json"))
    assert firefox.dump.camoufox()["timezone"] == "Europe/Berlin"


@pytest.mark.parametrize(
    ("browser", "version", "client", "expected"),
    [
        ("chrome", "130.0", "curl_cffi", "chrome124"),
        ("chrome", "131.0", "curl_cffi", "chrome124"),
        ("chrome", "150.0", "curl_cffi", "chrome124"),
        ("firefox", "128.0", "curl_cffi", "firefox120"),
        ("safari", "17.4", "curl_cffi", "safari17_0"),
        ("chrome", "130.0", "tls_client", "chrome_120"),
        ("chrome", "150.0", "primp", "chrome_150"),
        ("chrome", "131.0", "rnet", "chrome_131"),
    ],
)
def test_impersonate_selects_nearest_not_newer(
    browser: str, version: str, client: str, expected: str
) -> None:
    base = BrowserFingerprint.load.browserforge(_read("browserforge/chrome.json"))
    fingerprint = base.model_copy(update={"browser": browser, "browser_version": version})
    requested = int(version.split(".")[0])
    target_version = next(
        version for version, target in IMPERSONATION_TARGETS[client][browser] if target == expected
    )
    if target_version == requested:
        assert fingerprint.dump.impersonate(client) == expected
    else:
        with pytest.warns(FormatLossWarning, match="TLS-профиля"):
            assert fingerprint.dump.impersonate(client) == expected
        with pytest.raises(ValidationFailed, match="TLS-профиля"):
            fingerprint.dump.impersonate(client, strict=True)
    assert target_version <= requested


def test_impersonate_rejects_unknown_client() -> None:
    fingerprint = BrowserFingerprint.load.browserforge(_read("browserforge/chrome.json"))
    with pytest.raises(ValidationFailed, match="неизвестный клиент"):
        fingerprint.dump.impersonate("unknown")
