"""Conversion to and from :mod:`http.cookiejar` without HTTP-client dependencies."""

from __future__ import annotations

from http.cookiejar import Cookie as StdlibCookie
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any

from identity_hub.formats.models import Cookie, Cookies, SameSite, format_loss

__all__ = ["dump", "load"]


def dump(cookies: Cookies, *, strict: bool = False) -> CookieJar:
    jar = CookieJar(policy=DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
    for cookie in cookies:
        if cookie.partition_key is not None:
            format_loss("http.cookiejar не поддерживает partitioned cookie; cookie пропущена", strict=strict)
            continue
        domain = cookie.domain if cookie.host_only else f".{cookie.domain}"
        rest: dict[str, Any] = {}
        if cookie.http_only:
            rest["HttpOnly"] = None
        if cookie.same_site is not None:
            rest["SameSite"] = {
                SameSite.STRICT: "Strict",
                SameSite.LAX: "Lax",
                SameSite.NONE: "None",
            }[cookie.same_site]
        jar.set_cookie(
            StdlibCookie(
                version=0,
                name=cookie.name,
                value=cookie.value.get_secret_value(),
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=not cookie.host_only,
                domain_initial_dot=not cookie.host_only,
                path=cookie.path,
                path_specified=True,
                secure=cookie.secure,
                expires=int(cookie.expires) if cookie.expires is not None else None,
                discard=cookie.expires is None,
                comment=None,
                comment_url=None,
                rest=rest,
                rfc2109=False,
            )
        )
    return jar


def load(jar: Any, *, strict: bool = False) -> Cookies:
    del strict
    result: list[Cookie] = []
    for item in jar:
        rest = getattr(item, "_rest", {})
        same_site = next(
            (value for key, value in rest.items() if str(key).lower() == "samesite"),
            None,
        )
        result.append(
            Cookie.model_validate(
                {
                    "name": item.name,
                    "value": item.value,
                    "domain": str(item.domain).lstrip("."),
                    "host_only": not bool(item.domain_specified),
                    "path": item.path or "/",
                    "expires": None if item.discard or item.expires is None else item.expires,
                    "secure": bool(item.secure),
                    "same_site": same_site,
                    "rest": rest,
                }
            )
        )
    return Cookies(result)
