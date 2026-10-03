"""Safe, deterministic demo data for local admin development."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from identity_hub.api.identities import IdentityCreate
from identity_hub.clock import FakeClock
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.enums import Severity
from identity_hub.hub import IdentityHub
from identity_hub.service import Service
from identity_hub.testing import MemoryStore
from pydantic import BaseModel, SecretStr

from identity_hub_admin.settings import Environment

DemoState = Literal["loading", "empty", "error", "large"]
DEMO_ACTOR = "identity-hub-admin:demo-seed"
DEMO_CLOCK = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
DEMO_KEYS = {"demo-only": b"identity-hub-admin-demo-key-0001"}


class DemoProfile(BaseModel):
    display_name: str
    country: str


DEMO_SERVICE = Service(
    name="demo_account",
    title="Demo accounts",
    profile=DemoProfile,
    filterable_profile=frozenset({"country"}),
    searchable_profile=frozenset({"display_name"}),
)


@dataclass(frozen=True, slots=True)
class DemoSpaceSpec:
    slug: str
    title: str
    state: DemoState
    account_count: int
    description: str


DEMO_SPACES = (
    DemoSpaceSpec(
        "demo-loading",
        "Loading state",
        "loading",
        8,
        "A small dataset used while exercising loading skeletons and delayed responses.",
    ),
    DemoSpaceSpec(
        "demo-empty",
        "Empty state",
        "empty",
        0,
        "An existing space with no accounts.",
    ),
    DemoSpaceSpec(
        "demo-error",
        "Error state",
        "error",
        0,
        "A deterministic UI error scenario; it never calls an external service.",
    ),
    DemoSpaceSpec(
        "demo-large",
        "Large dataset",
        "large",
        250,
        "Enough accounts to exercise pagination, selection, and dense table layouts.",
    ),
)


class NoopTelegramTransport:
    """Prevent demo mode from creating a real Telegram client or sending network traffic."""

    async def send_message(
        self,
        bot_token: SecretStr,
        chat_id: int | str,
        text: str,
        *,
        thread_id: int | None = None,
    ) -> None:
        del bot_token, chat_id, text, thread_id


@dataclass(frozen=True, slots=True)
class DemoSeedResult:
    spaces: int
    accounts_created: int
    accounts_existing: int


def create_demo_hub() -> IdentityHub:
    """Create an isolated, process-local hub containing no real credentials."""
    clock = FakeClock(DEMO_CLOCK)
    return IdentityHub(
        secrets=StaticSecretsProvider(DEMO_KEYS, "demo-only"),
        services=[DEMO_SERVICE],
        store=MemoryStore(clock),
        clock=clock,
        strict_events=True,
        telegram_transport=NoopTelegramTransport(),
    )


async def seed_demo(hub: IdentityHub, environment: Environment) -> DemoSeedResult:
    """Seed all demo scenarios through public, idempotent library operations."""
    if environment == Environment.PRODUCTION:
        raise RuntimeError("demo seed is forbidden in production")

    created = 0
    existing = 0
    for spec in DEMO_SPACES:
        space = hub.space(spec.slug, spec.title, create=True)
        await space.ref()
        for index in range(1, spec.account_count + 1):
            _identity, created_now = await space.identities.ensure(
                IdentityCreate(
                    service=DEMO_SERVICE,
                    external_ref=f"{spec.slug}-{index:04d}",
                    profile={
                        "display_name": f"Demo account {index:04d}",
                        "country": ("UA", "PL", "DE", "US")[index % 4],
                    },
                    labels={"scenario": spec.state, "batch": (index - 1) // 50 + 1},
                    tags=("demo", spec.state),
                    lifecycle="ready" if index % 7 else "provisioning",
                ),
                actor=DEMO_ACTOR,
            )
            if created_now:
                created += 1
            else:
                existing += 1
        if spec.slug == "demo-large":
            center = await space.admin.delivery_center()
            if not center.channels:
                channel = await space.notifications.create_telegram_channel(
                    "Demo operations",
                    bot_token=SecretStr("demo-only-telegram-token"),
                    chat_id=-1001234567890,
                    thread_id=42,
                )
                await space.notifications.create_rule(
                    name="Critical identity changes",
                    channel_id=channel.id,
                    event_types=("identity.*",),
                    min_severity=Severity.WARNING,
                    digest_seconds=300,
                    created_by=DEMO_ACTOR,
                )
            if not center.webhook_endpoints:
                await space.webhooks.create(
                    url="https://hooks.example.test/private/demo?token=demo-secret",
                    secret=SecretStr("demo-only-webhook-secret"),
                    event_types=("identity.*",),
                    description="Demo incident receiver",
                )
    return DemoSeedResult(len(DEMO_SPACES), created, existing)


async def demo_counts(hub: IdentityHub) -> dict[str, int]:
    """Return scenario counts without exposing account details or secrets."""
    counts: dict[str, int] = {}
    for spec in DEMO_SPACES:
        page = await hub.space(spec.slug, spec.title, create=True).identities.search(limit=1, count=True)
        counts[spec.slug] = page.total or 0
    return counts


def demo_manifest() -> list[dict[str, Any]]:
    """Return static scenario metadata for diagnostics and tests."""
    return [
        {
            "slug": spec.slug,
            "title": spec.title,
            "state": spec.state,
            "expected_count": spec.account_count,
            "description": spec.description,
        }
        for spec in DEMO_SPACES
    ]
