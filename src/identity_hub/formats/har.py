"""HAR 1.2 response-cookie loader."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from identity_hub.errors import ValidationFailed
from identity_hub.formats.models import Cookie, Cookies

__all__ = ["load"]


def load(data: Any, *, strict: bool = False) -> Cookies:
    del strict
    try:
        entries = data["log"]["entries"]
    except (KeyError, TypeError):
        raise ValidationFailed("HAR: ожидается log.entries") from None
    result: list[Cookie] = []
    for entry in entries:
        request_url = entry.get("request", {}).get("url", "")
        request_host = (urlsplit(request_url).hostname or "").lower()
        for source in entry.get("response", {}).get("cookies", []):
            row = dict(source)
            raw_domain = row.get("domain")
            domain = str(raw_domain).lstrip(".").lower() if raw_domain else request_host
            if not domain:
                raise ValidationFailed("HAR cookie не содержит domain и request URL")
            row["domain"] = domain
            row["host_only"] = raw_domain is None or not str(raw_domain).startswith(".")
            result.append(Cookie.model_validate(row))
    return Cookies(result)
