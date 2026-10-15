"""Explicit-template loaders for seller account and proxy lines."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from identity_hub.errors import ValidationFailed
from identity_hub.formats.models import Cookies
from identity_hub.formats.proxy import ProxyUrl

__all__ = ["load_account", "load_proxy"]


def _separator(value: str) -> str:
    if value == r"\|":
        return "|"
    if not value:
        raise ValidationFailed("разделитель строки не может быть пустым")
    return value


def load_account(line: str, template: str, sep: str = ":") -> dict[str, Any]:
    separator = _separator(sep)
    names = [item.strip() for item in template.split(separator)]
    values = line.rstrip("\r\n").split(separator)
    if len(names) != len(values) or not names or any(not name for name in names):
        raise ValidationFailed("строка аккаунта не соответствует шаблону")
    result: dict[str, Any] = dict(zip(names, values, strict=True))
    if result.get("cookies"):
        result["cookies"] = Cookies.load(result["cookies"])
    return result


def load_proxy(line: str, template: str) -> ProxyUrl:
    separator = ";" if ";" in template else ":"
    names = [item.strip().lower() for item in template.split(separator)]
    values = line.rstrip("\r\n").split(separator)
    if len(names) != len(values):
        raise ValidationFailed("строка прокси не соответствует шаблону")
    fields = dict(zip(names, values, strict=True))
    host = fields.get("host")
    port = fields.get("port")
    if not host or not port:
        raise ValidationFailed("шаблон прокси должен содержать host и port")
    scheme = fields.get("protocol") or fields.get("scheme") or "http"
    username = fields.get("username") or fields.get("user") or fields.get("login")
    password = fields.get("password") or fields.get("pass")
    credentials = ""
    if username:
        credentials = quote(username, safe="")
        if password is not None:
            credentials += f":{quote(password, safe='')}"
        credentials += "@"
    address = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return ProxyUrl(f"{scheme}://{credentials}{address}:{port}")
