"""Converters for the storage primitives used by optional ``eazy_sdk``."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from identity_hub.errors import ValidationFailed
from identity_hub.formats.models import BrowserSession, Cookie, Cookies, SameSite, format_loss

__all__ = ["dump", "dump_state", "load", "load_state"]

_SAME_SITE = {
    SameSite.LAX: "Lax",
    SameSite.STRICT: "Strict",
    SameSite.NONE: "None",
}
_REQUIRED = frozenset({"name", "value", "domain", "path", "host_only", "expires_at"})


def dump(value: Cookies, *, strict: bool = False) -> list[dict[str, object]]:
    """Return the JSON-ready form of ``eazy_sdk.cookies.CookieState``."""
    rows: list[dict[str, object]] = []
    for cookie in value:
        if cookie.partition_key is not None:
            format_loss(
                f"eazy CookieState не хранит partition_key cookie {cookie.name!r}",
                strict=strict,
            )
        rows.append(
            {
                "name": cookie.name,
                "value": cookie.value.get_secret_value(),
                "domain": cookie.domain,
                "path": cookie.path,
                "host_only": cookie.host_only,
                "expires_at": (
                    None
                    if cookie.expires is None
                    else datetime.fromtimestamp(cookie.expires, UTC).isoformat()
                ),
                "secure": cookie.secure,
                "http_only": cookie.http_only,
                "same_site": "" if cookie.same_site is None else _SAME_SITE[cookie.same_site],
            }
        )
    return rows


def load(data: Any, *, strict: bool = False) -> Cookies:
    """Load ``CookieState.to_primitive()`` without importing ``eazy_sdk``."""
    del strict
    if not isinstance(data, list | tuple):
        raise ValidationFailed("eazy CookieState должен быть списком cookies")
    cookies: list[Cookie] = []
    for raw in data:
        if not isinstance(raw, Mapping):
            raise ValidationFailed("eazy cookie должен быть объектом")
        missing = sorted(_REQUIRED - raw.keys())
        if missing:
            raise ValidationFailed(f"eazy cookie не содержит: {', '.join(missing)}")
        row = dict(raw)
        expires = row.pop("expires_at")
        if expires is not None:
            if not isinstance(expires, str):
                raise ValidationFailed("eazy cookie expires_at должен быть ISO timestamp или null")
            try:
                parsed = datetime.fromisoformat(expires)
            except ValueError:
                raise ValidationFailed("eazy cookie expires_at должен быть ISO timestamp") from None
            if parsed.tzinfo is None:
                raise ValidationFailed("eazy cookie expires_at должен содержать timezone")
            expires = parsed.timestamp()
        row["expires"] = expires
        cookies.append(Cookie.model_validate(row))
    return Cookies(cookies)


def dump_state(value: BrowserSession, *, strict: bool = False) -> Any:
    """Build an eazy ``CookieState`` lazily, keeping eazy_sdk optional."""
    if value.origins or value.indexed_db is not None:
        format_loss("eazy CookieState хранит только cookies браузерной сессии", strict=strict)
    try:
        from eazy_sdk.cookies import CookieState  # type: ignore[import-not-found]
    except ImportError:
        raise ImportError(
            "BrowserSession.dump.eazy_state() требует установленный eazy_sdk"
        ) from None
    return CookieState.from_primitive(dump(value.cookies, strict=strict))


def load_state(data: Any, *, strict: bool = False) -> BrowserSession:
    """Build a browser session from an eazy ``CookieState`` (duck-typed)."""
    to_primitive = getattr(data, "to_primitive", None)
    if not callable(to_primitive):
        raise ValidationFailed("ожидается eazy CookieState с методом to_primitive()")
    return BrowserSession(cookies=load(to_primitive(), strict=strict))
