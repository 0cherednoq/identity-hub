"""Scrapy request cookie rows."""

from __future__ import annotations

from typing import Any

from identity_hub.formats.models import Cookies, format_loss

__all__ = ["dump"]


def dump(cookies: Cookies, *, strict: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cookie in cookies:
        if cookie.partition_key is not None:
            format_loss("Scrapy не поддерживает partitioned cookie; cookie пропущена", strict=strict)
            continue
        if cookie.expires is not None or cookie.http_only or cookie.same_site is not None:
            format_loss("Scrapy Request(cookies=) теряет expires, HttpOnly и SameSite", strict=strict)
        rows.append(
            {
                "name": cookie.name,
                "value": cookie.value.get_secret_value(),
                "domain": cookie.domain if cookie.host_only else f".{cookie.domain}",
                "path": cookie.path,
                "secure": cookie.secure,
            }
        )
    return rows
