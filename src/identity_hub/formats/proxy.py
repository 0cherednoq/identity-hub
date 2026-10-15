"""Masked proxy URL plus client-specific argument shapes."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Literal
from urllib.parse import quote

from identity_hub.crypto import SecretValue
from identity_hub.proxies import ParsedProxy, parse_proxy_url

__all__ = ["ProxyUrl", "dump", "load"]


def _host(parsed: ParsedProxy) -> str:
    return f"[{parsed.host}]" if ":" in parsed.host else parsed.host


def _server(parsed: ParsedProxy) -> str:
    return f"{parsed.scheme}://{_host(parsed)}:{parsed.port}"


def _url(parsed: ParsedProxy) -> str:
    credentials = ""
    if parsed.username is not None:
        credentials = quote(parsed.username, safe="")
        if parsed.password is not None:
            credentials += f":{quote(parsed.password, safe='')}"
        credentials += "@"
    return f"{parsed.scheme}://{credentials}{_host(parsed)}:{parsed.port}"


class ProxyDump:
    def __init__(self, value: ProxyUrl) -> None:
        self._value = value

    def playwright(self) -> dict[str, str]:
        parsed = self._value._parsed
        result = {"server": _server(parsed)}
        if parsed.username is not None:
            result["username"] = parsed.username
        if parsed.password is not None:
            result["password"] = parsed.password
        return result

    def httpx(self) -> str:
        return _url(self._value._parsed)

    def requests(self) -> dict[str, str]:
        url = _url(self._value._parsed)
        return {"http": url, "https": url}

    def aiohttp(self) -> tuple[str, Any | None]:
        """Return ``(proxy_url, aiohttp.BasicAuth | None)`` with a lazy optional import."""
        parsed = self._value._parsed
        if parsed.username is None:
            return _server(parsed), None
        try:
            BasicAuth = import_module("aiohttp").BasicAuth
        except ImportError as exc:  # pragma: no cover - exercised only in a minimal installation
            raise RuntimeError("ProxyUrl.dump.aiohttp() требует optional dependency aiohttp") from exc
        return _server(parsed), BasicAuth(parsed.username, parsed.password or "")


class ProxyUrl(SecretValue):
    """Proxy URL that stays masked while exposing explicit client adapters."""

    __slots__ = ("_parsed",)

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self._parsed = parse_proxy_url(value)

    @property
    def dump(self) -> ProxyDump:
        return ProxyDump(self)


def load(url: str) -> ProxyUrl:
    return ProxyUrl(url)


def dump(value: ProxyUrl, client: Literal["playwright", "httpx", "requests", "aiohttp"]) -> Any:
    return getattr(value.dump, client)()
