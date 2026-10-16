"""An optional eazy_sdk ``SessionStore`` backed by an already-held hub lease."""

from __future__ import annotations

from typing import Any, Generic

from pydantic import BaseModel
from typing_extensions import TypeVar

from identity_hub.errors import RevisionConflict, ValidationFailed
from identity_hub.formats import BrowserSession
from identity_hub.lease import Lease

__all__ = ["EazySession", "HubSessionStore"]

TValue = TypeVar("TValue", default=Any)


class EazySession(BaseModel, Generic[TValue]):
    """Hub slot payload: the SDK value and its cookie snapshot."""

    value: TValue
    cookies: BrowserSession | None = None


def _types() -> tuple[type[Any], type[Any], type[Exception], type[Any]]:
    try:
        from eazy_sdk.auth.session import (  # type: ignore[import-not-found]
            SessionKey,
            SessionRevision,
            SessionRevisionError,
            StoredSession,
        )
    except ImportError:
        raise ImportError("identity_hub.integrations.eazy требует установленный eazy_sdk") from None
    return SessionKey, SessionRevision, SessionRevisionError, StoredSession


class HubSessionStore(Generic[TValue]):
    """eazy SessionStore using the encrypted session slot of one active lease.

    The store neither acquires nor releases the lease. Keys are created by :meth:`key` and bind
    every operation to this lease's identity plus one session slot.
    """

    def __init__(self, lease: Lease[Any, Any]) -> None:
        self.lease = lease

    def key(self, slot: str = "default") -> Any:
        """Return ``SessionKey('session:<identity UUID>:<slot>')`` for this lease."""
        if not slot or len(slot) > 64:
            raise ValidationFailed("слот eazy session — непустая строка до 64 символов")
        SessionKey, _revision, _error, _stored = _types()
        return SessionKey(f"session:{self.lease.identity_id}:{slot}")

    def _slot(self, key: Any) -> str:
        prefix = f"session:{self.lease.identity_id}:"
        raw = getattr(key, "value", None)
        if not isinstance(raw, str) or not raw.startswith(prefix):
            raise ValidationFailed("eazy SessionKey не принадлежит identity этой аренды")
        slot = raw[len(prefix) :]
        if not slot or len(slot) > 64:
            raise ValidationFailed("eazy SessionKey содержит недопустимый слот")
        return slot

    async def load(self, key: Any) -> Any | None:
        _key, SessionRevision, _error, StoredSession = _types()
        slot = self._slot(key)
        stored = await self.lease.session(slot)
        if stored is None:
            return None
        if not isinstance(stored, EazySession):
            raise ValidationFailed("схема session слота eazy должна быть EazySession[T]")
        cookies = None if stored.cookies is None else stored.cookies.dump.eazy_state()
        revision = await self.lease.session_revision(slot)
        return StoredSession(stored.value, SessionRevision(revision), cookies)

    async def save(
        self,
        key: Any,
        value: TValue,
        revision: Any,
        cookies: Any | None = None,
    ) -> None:
        _key, _revision, SessionRevisionError, _stored = _types()
        slot = self._slot(key)
        current = await self.lease.session_revision(slot)
        target = getattr(revision, "value", None)
        if not isinstance(target, int) or target != current + 1:
            raise SessionRevisionError(f"session revision must advance from {current} to {current + 1}")
        browser = None if cookies is None else BrowserSession.load.eazy_state(cookies)
        schema = self.lease.pool.service.session_schema(slot)
        model = schema.model_validate({"value": value, "cookies": browser})
        if not isinstance(model, EazySession):
            raise ValidationFailed("схема session слота eazy должна быть EazySession[T]")
        try:
            await self.lease.save_session(model, slot, expected_revision=current)
        except RevisionConflict as exc:
            raise SessionRevisionError("session revision changed concurrently") from exc

    async def invalidate(self, key: Any, expected: Any | None = None) -> None:
        _key, _revision, SessionRevisionError, _stored = _types()
        slot = self._slot(key)
        expected_value = None if expected is None else getattr(expected, "value", None)
        if expected is not None and not isinstance(expected_value, int):
            raise SessionRevisionError("expected session revision is not an integer")
        try:
            await self.lease._invalidate_session(slot, expected_revision=expected_value)
        except RevisionConflict as exc:
            raise SessionRevisionError("session revision changed concurrently") from exc
