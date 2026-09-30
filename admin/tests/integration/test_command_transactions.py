from __future__ import annotations

import asyncio

import pytest
from identity_hub.errors import ConcurrentModification
from identity_hub.hub import SpaceHandle
from tests.factories import SeededSpaces

from identity_hub_admin.api.contracts import MutationContext
from identity_hub_admin.auth import AdminPrincipal, AdminRole
from identity_hub_admin.commands import CommandGateway, CommandScope
from identity_hub_admin.context import AdminCommandContext
from identity_hub_admin.idempotency import IdempotencyCoordinator

pytestmark = pytest.mark.integration


def _context(space: SpaceHandle) -> AdminCommandContext:
    return AdminCommandContext(
        principal=AdminPrincipal(
            actor="test:operator",
            role=AdminRole.OPERATOR,
            allowed_spaces=frozenset({space.slug}),
        ),
        actor="test:operator",
        request_id="command-integration-001",
        space_slug=space.slug,
        idempotency_key="command-integration-key",
    )


async def test_pg_gateway_rolls_back_dry_run_and_serializes_revision_guard(
    seeded_spaces: SeededSpaces,
) -> None:
    space = seeded_spaces.alpha
    identity_id = seeded_spaces.alpha_ids[0]
    gateway = CommandGateway(IdempotencyCoordinator())
    initial = await space.identity_card(identity_id)

    async def update_note(scope: CommandScope, note: str) -> int:
        updated = await scope.handle.set_note(identity_id, note, actor=scope.actor)
        return updated.event_seq

    dry_revision = await gateway._invoke(
        space,
        _context(space),
        MutationContext(
            reason="preview support change",
            expected_revision=initial.revision,
            dry_run=True,
        ),
        lambda scope: update_note(scope, "dry-run-note"),
        identity_id,
    )
    after_dry_run = await space.identity_card(identity_id)
    assert dry_revision == initial.revision + 1
    assert after_dry_run.revision == initial.revision
    assert after_dry_run.identity.note != "dry-run-note"

    mutation = MutationContext(
        reason="concurrent support change",
        expected_revision=initial.revision,
    )
    results = await asyncio.gather(
        gateway._invoke(
            space,
            _context(space),
            mutation,
            lambda scope: update_note(scope, "winner-a"),
            identity_id,
        ),
        gateway._invoke(
            space,
            _context(space),
            mutation,
            lambda scope: update_note(scope, "winner-b"),
            identity_id,
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, int) for item in results) == 1
    failures = [item for item in results if isinstance(item, ConcurrentModification)]
    assert len(failures) == 1
    assert failures[0].expected_version == initial.revision
    assert failures[0].actual_version == initial.revision + 1
    final = await space.identity_card(identity_id)
    assert final.revision == initial.revision + 1
    assert final.identity.note in {"winner-a", "winner-b"}


async def test_pg_gateway_joins_requirement_resolution_to_locked_transaction(
    seeded_spaces: SeededSpaces,
) -> None:
    space = seeded_spaces.alpha
    identity_id = seeded_spaces.alpha_ids[0]
    await space.add_requirement(identity_id, "support_review", actor="test:platform")
    revision = (await space.identity_card(identity_id)).revision
    gateway = CommandGateway(IdempotencyCoordinator())

    async def resolve(scope: CommandScope) -> bool:
        return await scope.handle.resolve_requirement(
            identity_id,
            "support_review",
            actor=scope.actor,
            reason=scope.reason,
        )

    changed = await asyncio.wait_for(
        gateway._invoke(
            space,
            _context(space),
            MutationContext(reason="documents reviewed", expected_revision=revision),
            resolve,
            identity_id,
        ),
        timeout=5,
    )

    assert changed is True
    assert all(
        item.get("code") != "support_review"
        for item in (await space.identity_card(identity_id)).identity.requirements
    )
