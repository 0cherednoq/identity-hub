"""S6.1: canonical models, normalization, operations and JSON namespaces."""

from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path
from typing import assert_type

import pytest
from pydantic import SecretStr

from identity_hub import (
    ApiKey,
    BrowserFingerprint,
    BrowserSession,
    ClientCertificate,
    Cookie,
    Cookies,
    FormatLossWarning,
    HeaderSession,
    OAuth1Client,
    OAuth1Session,
    OAuthClient,
    OtpSecret,
    PasswordCredentials,
    PrivateKeyCredentials,
    RecoveryCodes,
    SameSite,
    SigningKey,
    TokenSession,
    ValidationFailed,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "formats" / "json"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            {
                "name": "a",
                "value": "v",
                "domain": ".Example.COM",
                "expires": -1,
                "sameSite": "unspecified",
                "httpOnly": True,
            },
            ("example.com", False, None, None, True),
        ),
        (
            {
                "key": "a",
                "value": "v",
                "domain": "Example.COM",
                "expirationDate": 0,
                "sameSite": "no_restriction",
                "secure": True,
                "hostOnly": True,
            },
            ("example.com", True, None, SameSite.NONE, False),
        ),
        (
            {
                "name": "a",
                "value": "v",
                "domain": "example.com",
                "expiry": "Wed, 01 Jan 2026 00:00:00 GMT",
                "rest": {"http_only": "False"},
            },
            ("example.com", True, 1767225600.0, None, False),
        ),
        (
            {
                "name": "a",
                "value": "v",
                "domain": "example.com",
                "expires": "2026-01-01T00:00:00Z",
                "rest": {"HTTPOnly": None},
            },
            ("example.com", True, 1767225600.0, None, True),
        ),
    ],
)
def test_cookie_normalizes_dialects(source: dict[str, object], expected: tuple[object, ...]) -> None:
    cookie = Cookie.model_validate(source)
    assert (cookie.domain, cookie.host_only, cookie.expires, cookie.same_site, cookie.http_only) == expected


def test_cookie_rules_matching_and_collection_operations() -> None:
    with pytest.raises(ValidationFailed, match="__Host-"):
        Cookie(name="__Host-id", value="x", domain="example.com", host_only=False, secure=True)
    with pytest.raises(ValidationFailed, match="__Secure-"):
        Cookie(name="__Secure-id", value="x", domain="example.com")
    with pytest.warns(FormatLossWarning):
        Cookie(name="insecure", value="x", domain="example.com", same_site="none")

    root = Cookie(name="sid", value="root", domain="example.com", host_only=False, secure=True)
    nested = Cookie(
        name="sid", value="nested", domain="example.com", host_only=False, path="/account", secure=True
    )
    host = Cookie(name="host", value="yes", domain="example.com", host_only=True)
    cookies = Cookies([root, nested, host])
    assert root.matches("https://sub.example.com/path")
    assert not root.matches("http://sub.example.com/path")
    assert not host.matches("https://sub.example.com/")
    assert cookies.header_for("https://example.com/account/settings") == "sid=nested; sid=root; host=yes"
    assert cookies.get("sid") == nested
    assert [cookie.name for cookie in cookies.for_url("https://sub.example.com/")] == ["sid"]


def test_json_namespaces_are_typed_round_trip_and_not_model_fields() -> None:
    cookies = Cookies.load.json((FIXTURES / "cookies.json").read_text(encoding="utf-8"))
    assert_type(cookies.dump.json(), list[dict[str, object]])
    assert Cookies.load(cookies.dump.json()) == cookies

    session = BrowserSession.load.json(
        json.loads((FIXTURES / "browser_session.json").read_text(encoding="utf-8"))
    )
    assert_type(session.dump.json(), dict[str, object])
    assert BrowserSession.load(session.dump.json()) == session
    assert "dump" not in session.model_dump() and "load" not in session.model_dump()
    assert "fixture-secret" not in repr(session)


def test_eazy_cookie_state_golden_round_trip_and_loss_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    golden = json.loads((FIXTURES.parent / "eazy_cookie_state.json").read_text(encoding="utf-8"))
    cookies = Cookies.load.eazy(golden)
    assert_type(cookies.dump.eazy(), list[dict[str, object]])
    assert cookies.dump.eazy() == golden
    assert cookies.root[0].expires == 1767225600.0
    assert cookies.root[1].expires is None and cookies.root[1].same_site is None
    naive = [dict(golden[0], expires_at="2026-01-01T00:00:00")]
    with pytest.raises(ValidationFailed, match="timezone"):
        Cookies.load.eazy(naive)

    partitioned = Cookies(
        [
            Cookie(
                name="chips",
                value="secret",
                domain="example.com",
                secure=True,
                partition_key={"top_level_site": "https://top.example"},
            )
        ]
    )
    with pytest.warns(FormatLossWarning, match="partition_key"):
        primitive = partitioned.dump.eazy()
    assert Cookies.load.eazy(primitive).root[0].partition_key is None
    with pytest.raises(ValidationFailed, match="partition_key"):
        partitioned.dump.eazy(strict=True)

    class FakeCookieState:
        def __init__(self, value: list[dict[str, object]]) -> None:
            self.value = value

        @classmethod
        def from_primitive(cls, value: object) -> FakeCookieState:
            assert isinstance(value, list)
            return cls(value)

        def to_primitive(self) -> list[dict[str, object]]:
            return self.value

    package = types.ModuleType("eazy_sdk")
    module = types.ModuleType("eazy_sdk.cookies")
    module.CookieState = FakeCookieState  # type: ignore[attr-defined]
    package.cookies = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "eazy_sdk", package)
    monkeypatch.setitem(sys.modules, "eazy_sdk.cookies", module)

    session = BrowserSession(cookies=cookies)
    state = session.dump.eazy_state()
    assert isinstance(state, FakeCookieState)
    assert BrowserSession.load.eazy_state(state) == session


def test_browser_session_expiry_and_token_helpers() -> None:
    session = BrowserSession(
        cookies=Cookies(
            [
                Cookie(name="sid", value="x", domain="example.com", expires=200),
                Cookie(name="refresh", value="y", domain="example.com", expires=300),
            ]
        )
    )
    assert session.expires_at({"sid", "refresh"}) == 200

    payload = base64.urlsafe_b64encode(b'{"sub":"42"}').decode().rstrip("=")
    token = TokenSession(access_token="access-secret", id_token=f"x.{payload}.y", expires_at=100)
    assert token.authorization_header() == {"Authorization": "Bearer access-secret"}
    assert token.is_expired(50, leeway=60)
    assert token.id_claims() == {"sub": "42"}
    assert "access-secret" not in repr(token)


def test_hotp_and_totp_rfc_vectors_and_uri() -> None:
    sha1 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    hotp = [755224, 287082, 359152, 969429, 338314, 254676, 287922, 162583, 399871, 520489]
    for counter, expected in enumerate(hotp):
        assert OtpSecret(type="hotp", secret=sha1, counter=counter).code(0) == f"{expected:06d}"
    assert OtpSecret(secret=sha1, digits=8).code(59) == "94287082"
    for algorithm, raw, expected in (
        ("SHA256", b"12345678901234567890123456789012", "46119246"),
        ("SHA512", b"1234567890123456789012345678901234567890123456789012345678901234", "90693936"),
    ):
        secret = base64.b32encode(raw).decode().rstrip("=")
        assert OtpSecret(secret=secret, algorithm=algorithm, digits=8).code(59) == expected

    otp = OtpSecret(secret=sha1, issuer="Example", account="alice@example.com")
    assert OtpSecret.from_uri(otp.to_uri()) == otp
    assert sha1 not in repr(otp)


def test_recovery_codes_are_consumed_immutably() -> None:
    codes = RecoveryCodes(codes=["one", "two"])
    first, after_first = codes.take()
    second, after_second = after_first.take()
    assert (first, second, codes.used, after_second.used) == ("one", "two", frozenset(), {0, 1})
    with pytest.raises(ValidationFailed):
        after_second.take()
    assert "one" not in repr(codes)


def test_all_secret_models_hide_values_in_repr() -> None:
    marker = "DO-NOT-LEAK"
    models = [
        PasswordCredentials(login="u", password=marker),
        ApiKey(key=marker, secret=marker),
        SigningKey(key_id="k", secret=marker),
        OAuthClient(client_id="c", client_secret=marker),
        OAuth1Client(consumer_key="c", consumer_secret=marker),
        PrivateKeyCredentials(private_key=marker),
        ClientCertificate(cert="public", key=marker, passphrase=marker),
        OAuth1Session(token=marker, token_secret=marker),
        HeaderSession(headers={"X-Token": SecretStr(marker)}),
    ]
    assert all(marker not in repr(model) and marker not in str(model) for model in models)


def test_browser_fingerprint_is_a_profile_model() -> None:
    fingerprint = BrowserFingerprint(
        user_agent="Mozilla/5.0",
        browser="chrome",
        browser_version="130.0.0.0",
        os="windows",
        languages=["en-US"],
    )
    assert fingerprint.model_dump(mode="json")["browser"] == "chrome"
