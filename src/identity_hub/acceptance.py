"""Policies and worker support for accepting imported identities."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from identity_hub.enums import AuthState, Lifecycle
from identity_hub.errors import LeaseUnavailable, ValidationFailed, describe_error

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub, SpaceHandle

__all__ = ["AcceptPolicy", "AcceptanceWorker", "Workers"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AcceptPolicy:
    """Declarative policy applied to newly imported identities."""

    activate_on_login: bool = False
    retire_on: frozenset[str] = frozenset()
    max_attempts: int = 1
    skip_on_challenge: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.activate_on_login, bool):
            raise ValidationFailed("accept.activate_on_login must be bool")
        if not isinstance(self.skip_on_challenge, bool):
            raise ValidationFailed("accept.skip_on_challenge must be bool")
        if (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.max_attempts < 1
        ):
            raise ValidationFailed("accept.max_attempts must be at least 1")
        if any(not isinstance(code, str) or not code or len(code) > 64 for code in self.retire_on):
            raise ValidationFailed("accept.retire_on values must be non-empty strings up to 64 characters")

    def dump(self) -> dict[str, object]:
        return {
            "activate_on_login": self.activate_on_login,
            "retire_on": sorted(self.retire_on),
            "max_attempts": self.max_attempts,
            "skip_on_challenge": self.skip_on_challenge,
        }

    @classmethod
    def load(cls, value: dict[str, object]) -> AcceptPolicy:
        retire_on = value.get("retire_on", [])
        max_attempts = value.get("max_attempts", 1)
        activate_on_login = value.get("activate_on_login", False)
        skip_on_challenge = value.get("skip_on_challenge", True)
        if not isinstance(retire_on, list):
            raise ValidationFailed("stored accept.retire_on must be a list")
        if not isinstance(max_attempts, int):
            raise ValidationFailed("stored accept.max_attempts must be an integer")
        if not isinstance(activate_on_login, bool) or not isinstance(skip_on_challenge, bool):
            raise ValidationFailed("stored accept flags must be bool")
        return cls(
            activate_on_login=activate_on_login,
            retire_on=frozenset(str(code) for code in retire_on),
            max_attempts=max_attempts,
            skip_on_challenge=skip_on_challenge,
        )


class AcceptanceWorker:
    """Apply durable import acceptance policies, one leased identity at a time."""

    def __init__(self, hub: IdentityHub) -> None:
        self.hub = hub

    async def run_once(self, limit: int = 100) -> int:
        if limit <= 0:
            return 0
        processed = 0
        for ref in await self.hub._store.maintenance_spaces():
            if processed >= limit:
                break
            space = self.hub.space(ref.slug, ref.title, space_id=ref.id)
            async with space._tx(own=True) as tx:
                pending = await tx.list_acceptance(limit=limit - processed)
            for entry in pending:
                if processed >= limit:
                    break
                try:
                    handled = await self._accept(space, entry.identity_id, entry.policy, entry.attempts)
                except LeaseUnavailable:
                    continue
                except Exception as exc:
                    logger.warning(
                        "acceptance skipped identity %s: %s",
                        entry.identity_id,
                        describe_error(exc),
                    )
                    continue
                processed += int(handled)
        return processed

    async def _accept(
        self,
        space: SpaceHandle,
        identity_id: UUID,
        stored_policy: dict[str, object],
        attempts: int,
    ) -> bool:
        from identity_hub.auth import LoginOutcome
        from identity_hub.state import DEFAULT_SLOT

        policy = AcceptPolicy.load(stored_policy)
        identity = await space.identities.get(identity_id)
        if identity.lifecycle != Lifecycle.PROVISIONING:
            await self._finish(space, identity_id)
            return True
        if identity.auth == AuthState.AUTHENTICATED:
            if policy.activate_on_login:
                await space.identities.activate(identity_id, actor="acceptance")
            await self._finish(space, identity_id)
            return True
        if identity.auth == AuthState.CHALLENGE:
            if policy.skip_on_challenge:
                await self._finish(space, identity_id)
                return True
            return False
        if identity.auth not in {AuthState.NO_SESSION, AuthState.SESSION_INVALID}:
            return False
        async with space._tx(own=True) as tx:
            auth = await tx.load_region(identity_id, "auth", DEFAULT_SLOT)
        if auth is not None and auth.retryable is False:
            await self._retire_or_finish(space, identity_id, policy, identity.auth_code, identity.standing)
            return True

        outcome = await space.auth.login(identity_id)
        if outcome is LoginOutcome.SUCCEEDED:
            if policy.activate_on_login:
                await space.identities.activate(identity_id, actor="acceptance")
            await self._finish(space, identity_id)
            return True
        if outcome is LoginOutcome.CHALLENGED:
            if policy.skip_on_challenge:
                await self._finish(space, identity_id)
            return True

        current = await space.identities.get(identity_id)
        async with space._tx(own=True) as tx:
            auth = await tx.load_region(identity_id, "auth", DEFAULT_SLOT)
        code = current.auth_code
        if code in policy.retire_on or current.standing in policy.retire_on:
            await space.identities.retire(
                identity_id,
                actor="acceptance",
                reason=f"acceptance rejected: {code or current.standing}",
            )
            await self._finish(space, identity_id)
        elif (auth is not None and auth.retryable is False) or attempts + 1 >= policy.max_attempts:
            await self._finish(space, identity_id)
        else:
            async with space._tx(own=True) as tx:
                await tx.update_acceptance(identity_id, attempts=attempts + 1)
        return True

    async def _retire_or_finish(
        self,
        space: SpaceHandle,
        identity_id: UUID,
        policy: AcceptPolicy,
        code: str | None,
        standing: str,
    ) -> None:
        if code in policy.retire_on or standing in policy.retire_on:
            await space.identities.retire(
                identity_id,
                actor="acceptance",
                reason=f"acceptance rejected: {code or standing}",
            )
        await self._finish(space, identity_id)

    @staticmethod
    async def _finish(space: SpaceHandle, identity_id: UUID) -> None:
        async with space._tx(own=True) as tx:
            await tx.update_acceptance(identity_id, done=True)


class Workers:
    """Explicit long-running workers exposed by an :class:`IdentityHub`."""

    def __init__(self, hub: IdentityHub) -> None:
        self.acceptance = AcceptanceWorker(hub)
