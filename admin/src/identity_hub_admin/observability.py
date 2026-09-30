"""Structured request observability without logging request secrets."""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from collections.abc import Mapping
from threading import Lock
from typing import Any

from litestar.enums import ScopeType
from litestar.exceptions import HTTPException
from litestar.types import ASGIApp, Message, Receive, Scope, Send

ACCESS_LOGGER = logging.getLogger("identity_hub_admin.access")
_SECRET_KEY = re.compile(r"password|secret|token|authorization|cookie|credential", re.IGNORECASE)
_SPACE_PATH = re.compile(r"/spaces/([^/]+)")


def scrub_secrets(value: Any) -> Any:
    """Recursively redact secret-looking mapping fields before structured logging."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else scrub_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [scrub_secrets(item) for item in value]
    return value


class RequestMetrics:
    """Small in-process counters suitable for health diagnostics and metric adapters."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._requests = 0
        self._latency_ms = 0.0
        self._statuses: Counter[str] = Counter()

    def observe(self, status: int, latency_ms: float) -> None:
        with self._lock:
            self._requests += 1
            self._latency_ms += latency_ms
            self._statuses[f"{status // 100}xx"] += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            average = self._latency_ms / self._requests if self._requests else 0.0
            return {
                "requests_total": self._requests,
                "latency_ms_average": round(average, 3),
                "status_classes": dict(sorted(self._statuses.items())),
            }


class AccessLogMiddleware:
    """Emit one JSON event per request; query strings and bodies are never included."""

    scopes = (ScopeType.HTTP,)

    def __init__(self, app: ASGIApp, metrics: RequestMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != ScopeType.HTTP:
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def capture_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        except HTTPException as exception:
            status = exception.status_code
            raise
        finally:
            latency_ms = (time.perf_counter() - started) * 1000
            self.metrics.observe(status, latency_ms)
            state = scope.get("state", {})
            user = scope.get("user")
            path = str(scope.get("path", ""))
            route_handler = scope.get("route_handler")
            route = str(getattr(route_handler, "path", path))
            match = _SPACE_PATH.search(path)
            event = {
                "event": "admin_http_request",
                "request_id": state.get("request_id", "unknown"),
                "actor": getattr(user, "actor", "anonymous"),
                "method": scope.get("method", ""),
                "route": route,
                "space": match.group(1) if match else None,
                "status": status,
                "latency_ms": round(latency_ms, 3),
            }
            ACCESS_LOGGER.log(logging.INFO if status < 500 else logging.WARNING, json.dumps(event))
