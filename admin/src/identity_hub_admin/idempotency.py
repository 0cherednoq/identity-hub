"""Concurrent request coalescing for admin mutations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from litestar.exceptions import HTTPException
from pydantic import BaseModel

ResultT = TypeVar("ResultT")


class IdempotencyConflict(HTTPException):
    status_code = 409

    def __init__(self) -> None:
        super().__init__("idempotency key was already used with a different request", status_code=409)


class IdempotencyResultConsumed(HTTPException):
    status_code = 409

    def __init__(self) -> None:
        super().__init__("one-time command result was already delivered", status_code=409)


@dataclass(slots=True)
class _Entry:
    fingerprint: str
    task: asyncio.Future[Any]
    created_at: float
    delivered: bool = False


def request_fingerprint(payload: object) -> str:
    if isinstance(payload, BaseModel):
        serializable: object = payload.model_dump(mode="json")
    elif isinstance(payload, Mapping | Sequence) and not isinstance(payload, str | bytes):
        serializable = payload
    else:
        serializable = payload
    encoded = json.dumps(
        serializable,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class IdempotencyCoordinator:
    """Coalesce identical in-flight calls and replay successful results for a short TTL."""

    def __init__(self, *, ttl_seconds: float = 3600, max_entries: int = 10_000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def execute(
        self,
        *,
        scope: str,
        operation: str,
        key: str,
        payload: object,
        call: Callable[[], Awaitable[ResultT]],
        retain_result: bool = True,
        one_time_result: bool = False,
    ) -> ResultT:
        cache_key = f"{scope}:{operation}:{key}"
        fingerprint = request_fingerprint(payload)
        async with self._lock:
            self._prune()
            entry = self._entries.get(cache_key)
            if entry is not None:
                if not hmac_safe_equal(entry.fingerprint, fingerprint):
                    raise IdempotencyConflict()
                if entry.delivered:
                    raise IdempotencyResultConsumed()
                task = entry.task
            else:
                task = asyncio.ensure_future(call())
                self._entries[cache_key] = _Entry(fingerprint, task, time.monotonic())
        try:
            result = await asyncio.shield(task)
            if one_time_result:
                async with self._lock:
                    current = self._entries.get(cache_key)
                    if current is None or current.task is not task:
                        raise IdempotencyResultConsumed()
                    if current.delivered:
                        raise IdempotencyResultConsumed()
                    replacement = asyncio.get_running_loop().create_future()
                    replacement.set_result(None)
                    current.task = replacement
                    current.delivered = True
                return cast(ResultT, result)
            if not retain_result and task.done():
                async with self._lock:
                    current = self._entries.get(cache_key)
                    if current is not None and current.task is task:
                        self._entries.pop(cache_key, None)
            return cast(ResultT, result)
        except asyncio.CancelledError:
            if task.done():
                async with self._lock:
                    current = self._entries.get(cache_key)
                    if current is not None and current.task is task:
                        self._entries.pop(cache_key, None)
            raise
        except BaseException:
            async with self._lock:
                current = self._entries.get(cache_key)
                if current is not None and current.task is task:
                    self._entries.pop(cache_key, None)
            raise

    def _prune(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        expired = [
            key for key, entry in self._entries.items() if entry.created_at <= cutoff and entry.task.done()
        ]
        for key in expired:
            self._entries.pop(key, None)
        overflow = len(self._entries) - self.max_entries
        if overflow > 0:
            completed = sorted(
                ((key, entry) for key, entry in self._entries.items() if entry.task.done()),
                key=lambda item: item[1].created_at,
            )
            for key, _entry in completed[:overflow]:
                self._entries.pop(key, None)


def hmac_safe_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)
