"""F1.4: санитайзер свободного текста, `scrub` и sink логов аренды (R2-S06, R2-S07, R2-S11)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr

from identity_hub.context import LeaseContext, _current
from identity_hub.domain.redaction import REDACTED, redact_inline_secrets, sanitize_text
from identity_hub.events import SecretLeak, scrub
from identity_hub.logging import LeaseLogPolicy, LeaseLogSink

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- sanitize_text


@pytest.mark.parametrize(
    ("raw", "leaked"),
    [
        ("401 for url https://api.example/v1/send?api_key=SK-LIVE-12345", "SK-LIVE-12345"),
        ("via proxy socks5://user:PROXYPASS@1.2.3.4:1080", "PROXYPASS"),
        ("header Authorization: Bearer eyJhbGciOi.abc.def", "eyJhbGciOi.abc.def"),
        ("GET /bot/send?token=123:ABC failed", "123:ABC"),
        ("login failed password=hunter2 for user", "hunter2"),
        ("session=abcdef-SESSION-KEY expired", "abcdef-SESSION-KEY"),
    ],
)
def test_sanitize_text_removes_inline_secrets(raw: str, leaked: str) -> None:
    clean = sanitize_text(raw)
    assert clean is not None and leaked not in clean
    assert REDACTED in clean


def test_sanitize_text_keeps_platform_statuses_and_limits_length() -> None:
    assert sanitize_text("USER_DEACTIVATED_BAN") == "USER_DEACTIVATED_BAN"
    assert sanitize_text("FLOOD_WAIT_300: слишком часто?") == "FLOOD_WAIT_300: слишком часто?"
    assert sanitize_text(None) is None
    assert sanitize_text("x" * 5000) == "x" * 256
    assert sanitize_text("y" * 500, limit=64) == "y" * 64
    assert sanitize_text("socks5://host:1080") == "socks5://host:1080"  # без учётных данных — не трогаем


def test_redact_inline_secrets_does_not_truncate() -> None:
    text = "a" * 1000 + " http://u:PW@h/"
    assert len(redact_inline_secrets(text)) > 1000 and "PW@" not in redact_inline_secrets(text)


# ---------------------------------------------------------------- scrub


@dataclass
class _Ctx:
    login: str
    password: str


SECRET_KEYS = [
    "password",
    "bot_token",
    "AuthKey",
    "auth_key",
    "session_cookie",
    "api_key",
    "apikey",
    "pwd",
    "passphrase",
    "authorization",
    "private_key",
    "seed",
    "otp",
    "jwt",
    "access_key",
    "sid",
    "proxy",
    "proxy_url",
    "totp_secret",
    "credentials",
    "evidence",
    "cookies",
    "tokens",
]

PLAIN_KEYS = [
    "tokens_used",
    "evidence_count",
    "password_changed_at",
    "proxy_id",
    "from_proxy_id",
    "proxy_pool",
    "key",
    "key_id",
    "code",
    "auth",
    "auth_state",
    "authenticated",
    "pinned_until",
    "secret_configuration_error",
    "kind",
]


@pytest.mark.parametrize("key", SECRET_KEYS)
def test_scrub_redacts_secret_key_names(key: str) -> None:
    assert scrub({key: "LEAK"}) == {key: REDACTED}
    assert scrub({"nested": {"list": [{key: "LEAK"}]}}) == {"nested": {"list": [{key: REDACTED}]}}
    with pytest.raises(SecretLeak):
        scrub({key: "LEAK"}, strict=True)


@pytest.mark.parametrize("key", PLAIN_KEYS)
def test_scrub_keeps_keys_that_only_resemble_secrets(key: str) -> None:
    assert scrub({key: "value"}, strict=True) == {key: "value"}


def test_scrub_checks_strings_everywhere_including_lists() -> None:
    data: dict[str, Any] = {
        "error": "boom password=LEAK-in-message",
        "note": "прокси socks5://u:LEAK-proxypass@h:1",
        "items": ["ok", "Authorization: Bearer LEAK-bearer", ["token=LEAK-nested"]],
    }
    out = scrub(data)
    assert "LEAK" not in json.dumps(out, ensure_ascii=False)
    assert out["items"][0] == "ok"
    with pytest.raises(SecretLeak):
        scrub({"items": ["token=LEAK"]}, strict=True)


def test_scrub_replaces_unknown_objects_with_type_name() -> None:
    out = scrub({"obj": _Ctx("u", "LEAK-dataclass"), "typed": SecretStr("x"), "raw": b"x"})
    assert out == {"obj": "<_Ctx>", "typed": REDACTED, "raw": REDACTED}
    with pytest.raises(SecretLeak):
        scrub({"obj": _Ctx("u", "LEAK")}, strict=True)


def test_scrub_keeps_plain_scalars() -> None:
    uid = uuid4()
    data = {"count": 3, "ratio": 0.5, "ok": True, "none": None, "id": uid, "wait": timedelta(seconds=90)}
    assert scrub(data, strict=True) == {
        "count": 3,
        "ratio": 0.5,
        "ok": True,
        "none": None,
        "id": str(uid),
        "wait": "0:01:30",
    }


# ---------------------------------------------------------------- LeaseLogSink


def _context() -> LeaseContext:
    return LeaseContext(uuid4(), uuid4(), "svc", "pool", "holder")


def _log(sink: LeaseLogSink, ctx: LeaseContext, message: str, *args: Any, **extra: Any) -> None:
    logger = logging.getLogger(f"ih-test-{uuid4().hex}")
    logger.addHandler(sink)
    logger.propagate = False
    token = _current.set(ctx)
    try:
        logger.warning(message, *args, extra=extra)
    finally:
        _current.reset(token)


def test_sink_scrubs_fields_and_message() -> None:
    sink, ctx = LeaseLogSink(), _context()
    _log(
        sink,
        ctx,
        "login failed for %s via %s",
        "user",
        "socks5://u:LEAK-msg@h:1",
        proxy="socks5://u:LEAK-extra@h:1",
        session={"auth_key": "LEAK-nested"},
        attempt=3,
    )
    (entry,) = sink.drain(ctx.lease_id)
    assert "LEAK" not in entry.message and "LEAK" not in json.dumps(entry.fields)
    assert entry.fields["attempt"] == 3 and entry.fields["proxy"] == REDACTED


def test_sink_limits_field_and_entry_size() -> None:
    policy = LeaseLogPolicy()
    sink, ctx = LeaseLogSink(policy), _context()
    _log(sink, ctx, "big", blob="A" * 5_000_000, **{f"f{i}": "B" * 900 for i in range(40)})
    (entry,) = sink.drain(ctx.lease_id)
    assert len(entry.fields["blob"]) <= policy.max_field_chars
    assert len(json.dumps(entry.fields)) <= policy.max_entry_bytes
    assert entry.fields["_truncated"] is True


def test_sink_drops_buffers_of_leases_that_never_released() -> None:
    """Аренда из `pool.acquire()` без `release()` не оставляет буфер в памяти навсегда."""
    now = [0.0]
    sink = LeaseLogSink(LeaseLogPolicy(buffer_ttl=timedelta(minutes=10)), monotonic=lambda: now[0])
    stale, fresh = _context(), _context()
    _log(sink, stale, "забытая аренда")
    now[0] = 601.0
    _log(sink, fresh, "новая аренда")
    assert stale.lease_id not in sink._buffers and stale.lease_id not in sink._counts
    assert len(sink.drain(fresh.lease_id)) == 1
