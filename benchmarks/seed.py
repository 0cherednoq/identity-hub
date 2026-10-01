"""Быстрый COPY seed, всегда выполняемый до измеряемого участка."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["seed_identities"]


async def seed_identities(
    engine: AsyncEngine,
    *,
    space_id: UUID,
    service: str,
    count: int,
    prefix: str = "load",
) -> list[UUID]:
    ids = [uuid4() for _ in range(count)]

    def records() -> Iterator[tuple[object, ...]]:
        for index, identity_id in enumerate(ids):
            external_ref = f"{prefix}-{index:06d}"
            yield (
                identity_id,
                space_id,
                service,
                external_ref,
                external_ref,  # normalized_ref: str.strip не меняет сгенерированный ref (D-149)
                "ready",
                "good",
                "authenticated",
                "available",
                external_ref,
                0.0,
                0,
                0,
            )

    async with engine.begin() as connection:
        raw = await connection.get_raw_connection()
        driver = raw.driver_connection
        if driver is None or not hasattr(driver, "copy_records_to_table"):
            raise RuntimeError("load seed требует asyncpg copy_records_to_table")
        await driver.copy_records_to_table(
            "ih_identities",
            records=records(),
            columns=(
                "id",
                "space_id",
                "service",
                "external_ref",
                "normalized_ref",
                "lifecycle",
                "standing",
                "auth_state",
                "availability",
                "search_text",
                "error_score",
                "fencing_seq",
                "event_seq",
            ),
        )
    return ids
