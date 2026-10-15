"""Tolerant Netscape/curl cookies.txt reader and writer."""

from __future__ import annotations

from typing import Literal

from identity_hub.errors import ValidationFailed
from identity_hub.formats.models import Cookie, Cookies, format_loss

__all__ = ["dump", "load"]

_HEADER = "# Netscape HTTP Cookie File"


def dump(
    cookies: Cookies,
    *,
    session_expires: Literal["zero", "empty"] = "zero",
    strict: bool = False,
) -> str:
    if session_expires not in {"zero", "empty"}:
        raise ValidationFailed("session_expires: ожидается 'zero' или 'empty'")
    lines = [_HEADER]
    for cookie in cookies:
        if cookie.partition_key is not None:
            format_loss("Netscape не поддерживает partitioned cookie; cookie пропущена", strict=strict)
            continue
        if cookie.same_site is not None:
            format_loss("Netscape теряет SameSite", strict=strict)
        if any(
            "\t" in item or "\r" in item or "\n" in item
            for item in (cookie.name, cookie.value.get_secret_value())
        ):
            raise ValidationFailed("Netscape cookie не допускает TAB и перевод строки")
        domain = cookie.domain if cookie.host_only else f".{cookie.domain}"
        if cookie.http_only:
            domain = f"#HttpOnly_{domain}"
        expires = (
            str(int(cookie.expires))
            if cookie.expires is not None
            else "0"
            if session_expires == "zero"
            else ""
        )
        lines.append(
            "\t".join(
                (
                    domain,
                    "FALSE" if cookie.host_only else "TRUE",
                    cookie.path,
                    "TRUE" if cookie.secure else "FALSE",
                    expires,
                    cookie.name,
                    cookie.value.get_secret_value(),
                )
            )
        )
    return "\n".join(lines) + "\n"


def load(text: str | bytes, *, strict: bool = False) -> Cookies:
    del strict
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValidationFailed("Netscape cookies должны быть UTF-8") from None
    result: list[Cookie] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r")
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line.removeprefix("#HttpOnly_")
        fields = line.split("\t")
        if len(fields) != 7:
            raise ValidationFailed(f"Netscape: строка {number} должна содержать 7 полей")
        domain, include_subdomains, path, secure, expires, name, value = fields
        host_only = include_subdomains.strip().upper() != "TRUE"
        result.append(
            Cookie(
                name=name,
                value=value,
                domain=domain.lstrip("."),
                host_only=host_only,
                path=path or "/",
                expires=None if expires in {"", "0"} else expires,
                secure=secure.strip().upper() == "TRUE",
                http_only=http_only,
            )
        )
    return Cookies(result)
