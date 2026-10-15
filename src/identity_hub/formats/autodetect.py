"""Safe content-based detection for cookie and browser-session representations."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, cast
from urllib.parse import urlsplit

from identity_hub.errors import ValidationFailed
from identity_hub.formats.models import BrowserSession, Cookie, Cookies

__all__ = ["load_browser_session", "load_cookies"]

_EXPECTED = "JSON cookies/storage_state, Netscape, Cookie header или base64 от них"


def _decode(data: Any) -> Any:
    if isinstance(data, bytes):
        if data.startswith(b"\x80"):
            raise ValidationFailed("pickle не загружается; ожидается " + _EXPECTED)
        if data.startswith(b"SQLite format 3\x00"):
            raise ValidationFailed("Chrome Cookies SQLite не загружается; ожидается " + _EXPECTED)
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValidationFailed("неизвестный бинарный формат; ожидается " + _EXPECTED) from None
    return data


def _json_or_base64(data: str) -> Any:
    stripped = data.strip()
    if stripped.startswith(("[", "{")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            raise ValidationFailed("некорректный JSON; ожидается " + _EXPECTED) from None
    try:
        decoded = base64.b64decode("".join(stripped.split()), validate=True)
        if decoded and decoded != stripped.encode():
            return _decode(decoded)
    except (binascii.Error, ValueError):
        pass
    return data


def _json_cookies(data: Any, *, strict: bool) -> Cookies:
    from identity_hub.formats import cdp, chrome_extension, crawlee, playwright, selenium, tough_cookie

    if isinstance(data, dict) and "cookies" in data and "version" in data:
        return tough_cookie.load(data, strict=strict)
    rows = data.get("cookies") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValidationFailed("неизвестный JSON; ожидается " + _EXPECTED)
    if not rows:
        return Cookies()
    first = rows[0]
    if not isinstance(first, dict):
        raise ValidationFailed("cookie JSON должен быть массивом объектов")
    if "key" in first:
        return tough_cookie.load(rows, strict=strict)
    if "expirationDate" in first or "hostOnly" in first:
        return chrome_extension.load(rows, strict=strict)
    if "expiry" in first:
        return selenium.load(rows, strict=strict)
    if "http_only" in first or "same_site" in first:
        return crawlee.load(rows, strict=strict)
    if "url" in first or "priority" in first or "sourceScheme" in first:
        return cdp.load(rows, strict=strict)
    return cast(Cookies, playwright.load(rows, strict=strict))


def _cookie_header(text: str, *, url: str | None) -> Cookies:
    if url is None:
        raise ValidationFailed("для строки Cookie нужен параметр url")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValidationFailed("для строки Cookie нужен URL с host")
    result = []
    for part in text.split(";"):
        name, separator, value = part.strip().partition("=")
        if not separator or not name:
            raise ValidationFailed("некорректная строка Cookie")
        result.append(Cookie(name=name, value=value, domain=host, host_only=True))
    return Cookies(result)


def load_cookies(data: Any, *, strict: bool = False, url: str | None = None) -> Cookies:
    from identity_hub.formats import netscape

    decoded = _decode(data)
    if isinstance(decoded, str):
        detected = _json_or_base64(decoded)
        if detected is not decoded:
            return load_cookies(detected, strict=strict, url=url)
        stripped = decoded.strip()
        if stripped.startswith("# Netscape HTTP Cookie File") or "\t" in stripped:
            return netscape.load(decoded, strict=strict)
        if "=" in stripped:
            return _cookie_header(stripped, url=url)
        raise ValidationFailed("неизвестный формат; ожидается " + _EXPECTED)
    if isinstance(decoded, list | dict):
        return _json_cookies(decoded, strict=strict)
    raise ValidationFailed("неизвестный формат; ожидается " + _EXPECTED)


def load_browser_session(data: Any, *, strict: bool = False) -> BrowserSession:
    from identity_hub.formats import json as json_format
    from identity_hub.formats import playwright

    decoded = _decode(data)
    if isinstance(decoded, str):
        detected = _json_or_base64(decoded)
        if detected is decoded:
            raise ValidationFailed("BrowserSession: ожидается JSON/base64 storage_state")
        return load_browser_session(detected, strict=strict)
    if isinstance(decoded, dict):
        origins = decoded.get("origins", [])
        if any(isinstance(item, dict) and "local_storage" in item for item in origins):
            return json_format.load(BrowserSession, decoded, strict=strict)
        if "cookies" in decoded:
            return cast(BrowserSession, playwright.load(decoded, session=True, strict=strict))
    raise ValidationFailed("BrowserSession: ожидается JSON/base64 storage_state")
