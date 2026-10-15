"""W3C WebDriver cookie dictionaries."""

from __future__ import annotations

from typing import Any

from identity_hub.formats.models import Cookie, Cookies, SameSite, format_loss

__all__ = ["dump", "load"]


def dump(cookies: Cookies, *, strict: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cookie in cookies:
        if cookie.partition_key is not None:
            format_loss("Selenium не поддерживает partitioned cookie; cookie пропущена", strict=strict)
            continue
        row: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value.get_secret_value(),
            "path": cookie.path,
            "secure": cookie.secure,
            "httpOnly": cookie.http_only,
        }
        if not cookie.host_only:
            row["domain"] = f".{cookie.domain}"
        if cookie.expires is not None:
            row["expiry"] = int(cookie.expires)
        if cookie.same_site is not None:
            row["sameSite"] = {
                SameSite.STRICT: "Strict",
                SameSite.LAX: "Lax",
                SameSite.NONE: "None",
            }[cookie.same_site]
        rows.append(row)
    return rows


def load(data: Any, *, strict: bool = False, domain: str | None = None) -> Cookies:
    del strict
    cookies = []
    for source in data:
        row = dict(source)
        if not row.get("domain"):
            if domain is None:
                raise ValueError("для host-only Selenium cookie нужен domain")
            row["domain"] = domain
            row["host_only"] = True
        cookies.append(Cookie.model_validate(row))
    return Cookies(cookies)
