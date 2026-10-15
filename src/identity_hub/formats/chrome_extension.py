"""chrome.cookies / Cookie-Editor / EditThisCookie / AdsPower / GoLogin."""

from __future__ import annotations

from typing import Any

from identity_hub.formats.models import Cookie, Cookies, SameSite

__all__ = ["dump", "load"]


def dump(cookies: Cookies, *, strict: bool = False) -> list[dict[str, Any]]:
    del strict
    rows = []
    for cookie in cookies:
        row: dict[str, Any] = {
            "domain": cookie.domain if cookie.host_only else f".{cookie.domain}",
            "hostOnly": cookie.host_only,
            "httpOnly": cookie.http_only,
            "name": cookie.name,
            "path": cookie.path,
            "sameSite": (
                "unspecified"
                if cookie.same_site is None
                else "no_restriction"
                if cookie.same_site is SameSite.NONE
                else cookie.same_site.value
            ),
            "secure": cookie.secure,
            "session": cookie.expires is None,
            "storeId": "0",
            "value": cookie.value.get_secret_value(),
        }
        if cookie.expires is not None:
            row["expirationDate"] = cookie.expires
        if cookie.partition_key is not None:
            row["partitionKey"] = {
                "topLevelSite": cookie.partition_key.top_level_site,
                "hasCrossSiteAncestor": cookie.partition_key.has_cross_site_ancestor,
            }
        rows.append(row)
    return rows


def load(data: Any, *, strict: bool = False) -> Cookies:
    del strict
    return Cookies([Cookie.model_validate(dict(row)) for row in data])
