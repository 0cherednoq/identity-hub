"""`space.imports` — импорт внешних данных: пачки, частичный успех, отчёт (IMP-23, D-144).

Импорт отличается от `identities.create_many`: пачки коммитятся независимо, ошибка записи не
роняет пачку, identity всегда создаётся в `provisioning` (FR-2.5), есть `on_existing`,
ссылки по ref, прокси и начальные верификации. Для атомарного создания — `create_many`.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from identity_hub.api._base import Namespace
from identity_hub.errors import NotFound
from identity_hub.importing import BatchHook, ImportRecord, ImportReport, OnExisting
from identity_hub.service import Service

if TYPE_CHECKING:
    from identity_hub.read.admin_state import ImportRunInfo

__all__ = ["Imports"]


class Imports(Namespace):
    """Импорт identity: `space.imports`."""

    __slots__ = ()

    async def run(
        self,
        *,
        service: Service | str,
        records: Iterable[ImportRecord] | AsyncIterable[ImportRecord],
        batch_size: int = 500,
        on_existing: OnExisting | str = OnExisting.SKIP,
        verify_sessions: bool = False,
        on_batch_committed: BatchHook | None = None,
        actor: str,
        run_id: UUID | None = None,
        max_identities: int = 3,
    ) -> ImportReport:
        """Импортировать поток записей пачками по `batch_size`, каждая — своя транзакция.

        Повтор идемпотентен: существующие попадают в `skipped` (или обновляют сессии при
        `on_existing="update_session"`, или падают в `failed` при `"fail"`). Негодная запись —
        в `failed` с причиной, остальные импортируются. `max_identities` — ёмкость прокси,
        создаваемых из `ImportRecord.proxy`; запись сверх ёмкости прокси отклоняется.
        """
        return await self._space._import_many(
            service=service,
            records=records,
            batch_size=batch_size,
            on_existing=on_existing,
            verify_sessions=verify_sessions,
            on_batch_committed=on_batch_committed,
            actor=actor,
            run_id=run_id,
            max_identities=max_identities,
        )

    async def run_files(
        self,
        *,
        service: Service | str,
        paths: Iterable[str | Path],
        batch_size: int = 500,
        on_existing: OnExisting | str = OnExisting.SKIP,
        verify_sessions: bool = False,
        on_batch_committed: BatchHook | None = None,
        actor: str,
        run_id: UUID | None = None,
        max_identities: int = 3,
        base_dir: str | Path | None = None,
        max_bytes: int | None = None,
    ) -> ImportReport:
        """Прочитать файлы импортёрами `Service.importers` и импортировать; параметры — как у `run`.

        `base_dir` — относительные пути считаются от него, путь за его пределами (в том числе через
        `..` или абсолютный) попадает в `failed`. `max_bytes` — файл (каталог — суммарно) больше
        лимита в `failed`, не читается. Пути из внешнего ввода передавайте только с `base_dir`.
        """
        return await self._space._import_files(
            service=service,
            paths=paths,
            base_dir=base_dir,
            max_bytes=max_bytes,
            batch_size=batch_size,
            on_existing=on_existing,
            verify_sessions=verify_sessions,
            on_batch_committed=on_batch_committed,
            actor=actor,
            run_id=run_id,
            max_identities=max_identities,
        )

    async def stage(
        self,
        *,
        service: Service | str,
        records: Iterable[ImportRecord],
        source: str,
        on_existing: OnExisting | str = OnExisting.SKIP,
        verify_sessions: bool = False,
        labels: Mapping[str, str | int | float | bool] | None = None,
        actor: str,
        run_id: UUID | None = None,
    ) -> UUID:
        """Сохранить импорт до 10 000 записей зашифрованным; обработает диспетчер. Возвращает id прогона."""
        return await self._space._stage_import(
            service=service,
            records=records,
            source=source,
            on_existing=on_existing,
            verify_sessions=verify_sessions,
            labels=labels,
            actor=actor,
            run_id=run_id,
        )

    async def get(self, run_id: UUID) -> ImportRunInfo:
        """Состояние фонового прогона этого пространства или `NotFound`."""
        info = await self._space._import_run(run_id)
        if info is None:
            raise NotFound("import_run", run_id)
        return info

    async def cancel(self, run_id: UUID) -> bool:
        """Запросить отмену; пачка, которая уже обрабатывается, доводится до конца."""
        return await self._space._cancel_import(run_id)
