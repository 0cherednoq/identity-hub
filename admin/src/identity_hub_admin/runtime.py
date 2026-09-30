"""Application lifecycle and readiness state."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from alembic.script import ScriptDirectory
from identity_hub import __version__ as library_version
from identity_hub.postgres.migrate import alembic_config
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from sqlalchemy import text

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub
    from litestar import Litestar
    from sqlalchemy.ext.asyncio import AsyncEngine

    from identity_hub_admin.settings import AdminSettings


@dataclass(slots=True)
class RuntimeState:
    ready: bool = False
    checks: dict[str, str] = field(default_factory=lambda: {"startup": "pending"})


@dataclass(slots=True)
class AdminRuntime:
    settings: AdminSettings
    hub: IdentityHub | None = None
    engine: AsyncEngine | None = None
    resources: tuple[Any, ...] = ()
    state: RuntimeState = field(default_factory=RuntimeState)

    async def check_database_revision(self) -> None:
        if self.engine is None:
            self.state.checks["database"] = "not_configured"
            return
        schema = self.settings.db_schema
        table = f'"{schema}".ih_alembic_version' if schema else "ih_alembic_version"
        async with self.engine.connect() as connection:
            current = str((await connection.execute(text(f"SELECT version_num FROM {table}"))).scalar_one())
        expected = ScriptDirectory.from_config(alembic_config(schema)).get_current_head()
        if current != expected:
            raise RuntimeError(f"database revision mismatch: current={current}, expected={expected}")
        self.state.checks["database"] = str(current)


@asynccontextmanager
async def runtime_lifespan(app: Litestar) -> AsyncIterator[None]:
    runtime: AdminRuntime = app.state.runtime
    errors = runtime.settings.startup_errors()
    if errors:
        runtime.state.checks["startup"] = "; ".join(errors)
        raise RuntimeError(runtime.state.checks["startup"])
    if Version(library_version) not in SpecifierSet(runtime.settings.library_version_spec):
        raise RuntimeError(
            f"identity-hub {library_version} does not satisfy {runtime.settings.library_version_spec}"
        )

    async with AsyncExitStack() as stack:
        if runtime.hub is not None:
            async with asyncio.timeout(runtime.settings.external_timeout_seconds):
                await runtime.hub.start()
            runtime.state.checks["hub"] = "ready"
            if runtime.settings.demo_mode:
                from identity_hub_admin.demo import seed_demo

                async with asyncio.timeout(runtime.settings.external_timeout_seconds):
                    seeded = await seed_demo(runtime.hub, runtime.settings.environment)
                runtime.state.checks["demo"] = (
                    f"{seeded.spaces} spaces; {seeded.accounts_created} created; "
                    f"{seeded.accounts_existing} existing"
                )
            if runtime.settings.run_dispatcher:
                await stack.enter_async_context(runtime.hub.dispatcher.running())
                runtime.state.checks["dispatcher"] = "running"
        async with asyncio.timeout(runtime.settings.external_timeout_seconds):
            await runtime.check_database_revision()
        for resource in runtime.resources:
            if hasattr(resource, "aclose"):

                async def close_resource(item: Any = resource) -> None:
                    async with asyncio.timeout(runtime.settings.shutdown_timeout_seconds):
                        await item.aclose()

                stack.push_async_callback(close_resource)
        runtime.state.checks["library_version"] = library_version
        runtime.state.checks["startup"] = "ready"
        runtime.state.ready = True
        try:
            yield
        finally:
            runtime.state.ready = False
            runtime.state.checks["startup"] = "stopped"
            if runtime.engine is not None:
                async with asyncio.timeout(runtime.settings.shutdown_timeout_seconds):
                    await runtime.engine.dispose()
