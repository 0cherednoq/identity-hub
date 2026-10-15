"""tough-cookie JSON used by Crawlee JS."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from identity_hub.formats.models import Cookie, Cookies

__all__ = ["dump", "load"]


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dump(cookies: Cookies, *, strict: bool = False) -> dict[str, Any]:
    del strict
    rows = []
    for cookie in cookies:
        row: dict[str, Any] = {
            "key": cookie.name,
            "value": cookie.value.get_secret_value(),
            "domain": cookie.domain,
            "path": cookie.path,
            "expires": _iso(cookie.expires) if cookie.expires is not None else "Infinity",
            "hostOnly": cookie.host_only,
            "httpOnly": cookie.http_only,
            "secure": cookie.secure,
        }
        if cookie.same_site is not None:
            row["sameSite"] = cookie.same_site.value
        if cookie.created_at is not None:
            row["creation"] = _iso(cookie.created_at)
        rows.append(row)
    return {"version": "tough-cookie@6", "storeType": "MemoryCookieStore", "cookies": rows}


def load(data: Any, *, strict: bool = False) -> Cookies:
    del strict
    rows = data.get("cookies", []) if isinstance(data, dict) else data
    return Cookies([Cookie.model_validate(dict(row)) for row in rows])
