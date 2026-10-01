"""Factories for isolated spaces, identities, and future auth principals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from identity_hub.hub import SpaceHandle

from tests.factories.api import register
from tests.factories.services import ADMIN_TEST_SERVICE

AdminRole = Literal["viewer", "operator", "admin"]


@dataclass(frozen=True, slots=True)
class AdminPrincipalFixture:
    actor: str
    role: AdminRole
    allowed_spaces: frozenset[str]


@dataclass(slots=True)
class AccountFactory:
    space: SpaceHandle
    prefix: str
    sequence: int = 0

    async def create(self, *, external_ref: str | None = None, country: str = "UA") -> UUID:
        self.sequence += 1
        result = await register(
            self.space,
            service=ADMIN_TEST_SERVICE,
            external_ref=external_ref or f"{self.prefix}-{self.sequence:03d}",
            profile={"country": country, "display_name": f"Account {self.sequence}"},
            labels={"batch": self.prefix},
            actor="test-factory",
        )
        return result.identity_id


@dataclass(frozen=True, slots=True)
class SeededSpaces:
    alpha: SpaceHandle
    beta: SpaceHandle
    alpha_ids: tuple[UUID, ...]
    beta_ids: tuple[UUID, ...]
