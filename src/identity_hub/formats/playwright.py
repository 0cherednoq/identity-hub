"""Playwright cookie rows and ``storage_state`` without importing Playwright."""

from __future__ import annotations

from typing import Any

from identity_hub.formats.models import (
    BrowserSession,
    Cookie,
    Cookies,
    OriginStorage,
    SameSite,
    format_loss,
)

__all__ = ["dump", "load"]


def _dump_cookie(cookie: Cookie) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": cookie.name,
        "value": cookie.value.get_secret_value(),
        "domain": cookie.domain if cookie.host_only else f".{cookie.domain}",
        "path": cookie.path,
        "expires": cookie.expires if cookie.expires is not None else -1,
        "httpOnly": cookie.http_only,
        "secure": cookie.secure,
    }
    if cookie.same_site is not None:
        row["sameSite"] = {SameSite.STRICT: "Strict", SameSite.LAX: "Lax", SameSite.NONE: "None"}[
            cookie.same_site
        ]
    if cookie.partition_key is not None:
        row["partitionKey"] = cookie.partition_key.top_level_site
    return row


def dump(value: Cookies | BrowserSession, *, strict: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
    if isinstance(value, Cookies):
        return [_dump_cookie(cookie) for cookie in value]
    origins: list[dict[str, Any]] = []
    for origin in value.origins:
        if origin.session_storage:
            format_loss("Playwright storage_state не хранит sessionStorage", strict=strict)
        origins.append(
            {
                "origin": origin.origin,
                "localStorage": [
                    {"name": name, "value": item} for name, item in origin.local_storage.items()
                ],
                **({"indexedDB": value.indexed_db} if value.indexed_db is not None else {}),
            }
        )
    return {"cookies": dump(value.cookies, strict=strict), "origins": origins}


def _load_cookie(row: dict[str, Any]) -> Cookie:
    value = dict(row)
    partition = value.get("partitionKey")
    if isinstance(partition, str):
        value["partition_key"] = {"top_level_site": partition}
    return Cookie.model_validate(value)


def load(data: Any, *, session: bool = False, strict: bool = False) -> Cookies | BrowserSession:
    del strict
    if session or (isinstance(data, dict) and "cookies" in data):
        state = dict(data)
        origins = []
        indexed_db: Any | None = None
        for raw in state.get("origins", []):
            origin = dict(raw)
            local = {item["name"]: item["value"] for item in origin.get("localStorage", [])}
            session_values = {item["name"]: item["value"] for item in origin.get("sessionStorage", [])}
            if "indexedDB" in origin:
                indexed_db = origin["indexedDB"]
            origins.append(
                OriginStorage(
                    origin=origin["origin"],
                    local_storage=local,
                    session_storage=session_values,
                )
            )
        return BrowserSession(
            cookies=Cookies([_load_cookie(dict(row)) for row in state.get("cookies", [])]),
            origins=origins,
            indexed_db=indexed_db,
        )
    return Cookies([_load_cookie(dict(row)) for row in data])
