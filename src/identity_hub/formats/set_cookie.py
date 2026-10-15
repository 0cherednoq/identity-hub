"""Small RFC 6265 Set-Cookie parser that does not use ``SimpleCookie``."""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from identity_hub.errors import ValidationFailed
from identity_hub.formats.models import Cookie, Cookies

__all__ = ["load"]

_COMBINED = re.compile(r",(?=\s*[^;,=\s]+\s*=)")


def _headers(value: str | Iterable[str]) -> list[str]:
    source = [value] if isinstance(value, str) else list(value)
    return [part.strip() for line in source for part in _COMBINED.split(line) if part.strip()]


def _default_path(path: str) -> str:
    if not path.startswith("/") or path.count("/") <= 1:
        return "/"
    return path.rsplit("/", 1)[0] or "/"


def _timestamp(value: float | datetime | None) -> float:
    if value is None:
        return time.time()
    return value.timestamp() if isinstance(value, datetime) else float(value)


def load(
    headers: str | Iterable[str],
    *,
    url: str,
    received_at: float | datetime | None = None,
    strict: bool = False,
) -> Cookies:
    del strict
    parsed_url = urlsplit(url)
    host = (parsed_url.hostname or "").lower()
    if not host:
        raise ValidationFailed("Set-Cookie требует URL с host")
    now = _timestamp(received_at)
    result: list[Cookie] = []
    for header in _headers(headers):
        parts = [part.strip() for part in header.split(";")]
        if not parts or "=" not in parts[0]:
            raise ValidationFailed("некорректный заголовок Set-Cookie")
        name, value = parts[0].split("=", 1)
        if not name:
            raise ValidationFailed("некорректный заголовок Set-Cookie")
        attrs: dict[str, str | None] = {}
        for part in parts[1:]:
            key, separator, item = part.partition("=")
            attrs[key.strip().lower()] = item.strip() if separator else None

        raw_domain = attrs.get("domain")
        domain = str(raw_domain).lstrip(".").lower() if raw_domain else host
        host_only = raw_domain is None
        if not host_only and host != domain and not host.endswith(f".{domain}"):
            raise ValidationFailed("Domain из Set-Cookie не соответствует URL")

        expires: float | None = None
        max_age = attrs.get("max-age")
        if max_age is not None:
            try:
                delta = int(max_age)
            except ValueError:
                delta = None
            if delta is not None:
                if delta <= 0:
                    continue
                expires = now + delta
        if expires is None and attrs.get("expires"):
            try:
                expires = parsedate_to_datetime(str(attrs["expires"])).timestamp()
            except (TypeError, ValueError, OverflowError):
                expires = None

        result.append(
            Cookie(
                name=name,
                value=value,
                domain=domain,
                host_only=host_only,
                path=str(attrs.get("path") or _default_path(parsed_url.path)),
                expires=expires,
                secure="secure" in attrs,
                http_only="httponly" in attrs,
                same_site=attrs.get("samesite"),
                extras={
                    "set_cookie": {
                        key: item
                        for key, item in attrs.items()
                        if key
                        not in {"domain", "path", "expires", "max-age", "secure", "httponly", "samesite"}
                    }
                },
            )
        )
    return Cookies(result)
