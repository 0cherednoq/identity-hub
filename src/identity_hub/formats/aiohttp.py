"""aiohttp ``SimpleCookie`` updates without importing aiohttp or yarl."""

from __future__ import annotations

from email.utils import formatdate
from http.cookies import SimpleCookie

from identity_hub.formats.models import Cookies, SameSite, format_loss

__all__ = ["dump"]


def dump(cookies: Cookies, *, strict: bool = False) -> list[tuple[SimpleCookie, str]]:
    result: list[tuple[SimpleCookie, str]] = []
    for cookie in cookies:
        if cookie.partition_key is not None:
            format_loss("aiohttp не поддерживает partitioned cookie; cookie пропущена", strict=strict)
            continue
        morsels: SimpleCookie = SimpleCookie()
        morsels[cookie.name] = cookie.value.get_secret_value()
        morsel = morsels[cookie.name]
        morsel["path"] = cookie.path
        if not cookie.host_only:
            morsel["domain"] = cookie.domain
        if cookie.secure:
            morsel["secure"] = True
        if cookie.http_only:
            morsel["httponly"] = True
        if cookie.same_site is not None:
            morsel["samesite"] = {
                SameSite.STRICT: "Strict",
                SameSite.LAX: "Lax",
                SameSite.NONE: "None",
            }[cookie.same_site]
        if cookie.expires is not None:
            morsel["expires"] = formatdate(cookie.expires, usegmt=True)
        scheme = "https" if cookie.secure else "http"
        result.append((morsels, f"{scheme}://{cookie.domain}{cookie.path}"))
    return result
