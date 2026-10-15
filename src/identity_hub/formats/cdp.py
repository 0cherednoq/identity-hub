"""Chrome DevTools Protocol cookie rows and Puppeteer partition-key dialect."""

from __future__ import annotations

from typing import Any

from identity_hub.formats.models import Cookie, Cookies, PartitionKey, SameSite

__all__ = ["dump", "load"]


def dump(cookies: Cookies, *, strict: bool = False, puppeteer: bool = False) -> list[dict[str, Any]]:
    del strict
    rows: list[dict[str, Any]] = []
    for cookie in cookies:
        row: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value.get_secret_value(),
            "path": cookie.path,
            "httpOnly": cookie.http_only,
            "secure": cookie.secure,
            **cookie.extras,
        }
        if cookie.host_only:
            row["url"] = f"{'https' if cookie.secure else 'http'}://{cookie.domain}{cookie.path}"
        else:
            row["domain"] = f".{cookie.domain}"
        if cookie.expires is not None:
            row["expires"] = cookie.expires
        if cookie.same_site is not None:
            row["sameSite"] = {
                SameSite.STRICT: "Strict",
                SameSite.LAX: "Lax",
                SameSite.NONE: "None",
            }[cookie.same_site]
        if cookie.partition_key is not None:
            key = cookie.partition_key
            row["partitionKey"] = (
                {
                    "sourceOrigin": key.top_level_site,
                    **(
                        {"hasCrossSiteAncestor": key.has_cross_site_ancestor}
                        if key.has_cross_site_ancestor is not None
                        else {}
                    ),
                }
                if puppeteer
                else key.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
            if not puppeteer:
                row["partitionKey"] = {
                    "topLevelSite": key.top_level_site,
                    **(
                        {"hasCrossSiteAncestor": key.has_cross_site_ancestor}
                        if key.has_cross_site_ancestor is not None
                        else {}
                    ),
                }
        rows.append(row)
    return rows


def load(data: Any, *, strict: bool = False) -> Cookies:
    del strict
    rows = data.get("cookies", []) if isinstance(data, dict) else data
    cookies: list[Cookie] = []
    known = {
        "name",
        "value",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
        "partitionKey",
        "session",
        "url",
    }
    for source in rows:
        row = dict(source)
        url = row.get("url")
        if not row.get("domain") and url:
            from urllib.parse import urlsplit

            row["domain"] = urlsplit(url).hostname
            row["host_only"] = True
        partition = row.get("partitionKey")
        if isinstance(partition, dict):
            top = partition.get("topLevelSite") or partition.get("sourceOrigin")
            if top:
                row["partition_key"] = PartitionKey(
                    top_level_site=top,
                    has_cross_site_ancestor=partition.get("hasCrossSiteAncestor"),
                )
        if row.get("session") is True or row.get("expires") == -1:
            row["expires"] = None
        row["extras"] = {key: value for key, value in source.items() if key not in known}
        cookies.append(Cookie.model_validate(row))
    return Cookies(cookies)
