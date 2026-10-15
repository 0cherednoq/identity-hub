"""Crawlee Python ``CookieParam`` rows."""

from __future__ import annotations

from typing import Any

from identity_hub.formats.models import Cookie, Cookies, SameSite

__all__ = ["dump", "load"]


def dump(cookies: Cookies, *, strict: bool = False) -> list[dict[str, Any]]:
    del strict
    rows = []
    for cookie in cookies:
        row: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value.get_secret_value(),
            "domain": cookie.domain if cookie.host_only else f".{cookie.domain}",
            "path": cookie.path,
            "http_only": cookie.http_only,
            "secure": cookie.secure,
        }
        if cookie.expires is not None:
            row["expires"] = cookie.expires
        if cookie.same_site is not None:
            row["same_site"] = {
                SameSite.STRICT: "Strict",
                SameSite.LAX: "Lax",
                SameSite.NONE: "None",
            }[cookie.same_site]
        rows.append(row)
    return rows


def load(data: Any, *, strict: bool = False) -> Cookies:
    del strict
    return Cookies([Cookie.model_validate(dict(row)) for row in data])
