"""Импорт аккаунтов (ТЗ FR-2.4–2.7, G-4.4).

Записи валидируются и шифруются схемами сервиса **до** обращения к БД, пишутся пачками
(по умолчанию 500) в отдельных транзакциях; пачки независимы. Отчёт: created / updated / skipped / failed.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pydantic_core

from identity_hub._ids import new_id
from identity_hub._space import SpaceBase, logger
from identity_hub.api._base import legacy
from identity_hub.crypto import associated_data
from identity_hub.domain.regions import Observation
from identity_hub.enums import AuthState, ChangeSource, Lifecycle
from identity_hub.errors import IdentityHubError, describe_error
from identity_hub.events import EventDraft
from identity_hub.service import Service
from identity_hub.store.base import ImportItemResult, NewImportItem, NewImportRun

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

__all__ = [
    "BatchHook",
    "ImportRecord",
    "ImportReport",
    "ImportRunner",
    "ImportingMixin",
    "OnExisting",
]

BatchHook = Callable[[Any, list[UUID]], Awaitable[None]]
"""on_batch_committed(session, created_ids): доменные строки после каждой пачки (FR-2.7)."""


@dataclass(slots=True)
class ImportRecord:
    """Одна запись импорта. Данные валидируются схемами сервиса до обращения к БД.

    `links` — роль → `"<service>:<external_ref>"` или `external_ref` того же сервиса (D-118),
    в том числе аккаунт из той же пачки. `sessions` — слот → данные сессии; `proxy` — URL прокси
    (дедупликация по fingerprint). `verifications` — фактор → начальное состояние (например, `"verified"`).
    """

    external_ref: str
    id: UUID | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    profile: Mapping[str, Any] = field(default_factory=dict, repr=False)
    labels: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    tags: frozenset[str] = frozenset()
    note: str | None = None
    # секреты не попадают в repr: запись импорта печатают в логах и трассировках (R2-S14)
    credentials: Mapping[str, Any] | None = field(default=None, repr=False)
    sessions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict, repr=False)
    links: Mapping[str, str] = field(default_factory=dict)
    proxy: str | None = field(default=None, repr=False)
    verifications: Mapping[str, str] = field(default_factory=dict)


class OnExisting(StrEnum):
    SKIP = "skip"
    UPDATE_SESSION = "update_session"
    FAIL = "fail"


@dataclass
class ImportReport:
    created: list[UUID] = field(default_factory=list)
    updated: list[UUID] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (external_ref, причина)
    failed: list[tuple[str, str]] = field(default_factory=list)
    hook_failed: list[list[UUID]] = field(default_factory=list)  # пачки, для которых хук упал — повторить

    @property
    def total(self) -> int:
        return len(self.created) + len(self.updated) + len(self.skipped) + len(self.failed)


@dataclass
class _Prepared:
    record: ImportRecord
    prepared: Any  # identities.PreparedIdentity
    proxy: Any = None  # NewProxy


class ImportingMixin(SpaceBase):
    async def _import_many(
        self,
        *,
        service: Service | str,
        records: Iterable[ImportRecord] | AsyncIterable[ImportRecord],
        batch_size: int = 500,
        on_existing: OnExisting | str = OnExisting.SKIP,
        verify_sessions: bool = False,
        on_batch_committed: BatchHook | None = None,
        actor: str = "import",
        run_id: UUID | None = None,
    ) -> ImportReport:
        """Импорт потока записей (FR-2.4). Идемпотентен: повтор кладёт записи в `skipped`."""
        svc = self.hub.service(service)
        mode = OnExisting(on_existing)
        report = ImportReport()
        batch: list[ImportRecord] = []
        async for record in _aiter(records):
            batch.append(record)
            if len(batch) >= batch_size:
                await self._import_batch(
                    svc, batch, mode, verify_sessions, on_batch_committed, actor, report, run_id
                )
                batch = []
        if batch:
            await self._import_batch(
                svc, batch, mode, verify_sessions, on_batch_committed, actor, report, run_id
            )
        return report

    async def _import_files(
        self, *, service: Service | str, paths: Iterable[str | Path], **kwargs: Any
    ) -> ImportReport:
        """Импорт файлов импортёрами сервиса (`.session`, tdata, JSON с cookies — FR-2.6)."""
        svc = self.hub.service(service)

        async def records() -> AsyncIterable[ImportRecord]:
            for raw in paths:
                path = Path(raw)
                importer = next((i for i in svc.importers if i.can_import(path)), None)
                if importer is None:
                    raise IdentityHubError(f"нет импортёра для файла {path.name}")
                async for record in importer.read(path):
                    yield record

        return await self._import_many(service=svc, records=records(), **kwargs)

    # --------------------------------------------------------- пачка

    async def _stage_import(
        self,
        *,
        service: Service | str,
        records: Iterable[ImportRecord],
        source: str,
        on_existing: OnExisting | str = OnExisting.SKIP,
        verify_sessions: bool = False,
        labels: Mapping[str, str | int | float | bool] | None = None,
        actor: str = "import",
        run_id: UUID | None = None,
    ) -> UUID:
        """Persist a resumable import without retaining the source file."""
        svc = self.hub.service(service)
        mode = OnExisting(on_existing)
        batch_labels = dict(labels or {})
        staged = [
            replace(
                record,
                id=record.id or new_id(),
                labels={**dict(record.labels), **batch_labels},
            )
            for record in records
        ]
        if not staged:
            from identity_hub.errors import ValidationFailed

            raise ValidationFailed("import must contain at least one record")
        if len(staged) > 10_000:
            from identity_hub.errors import ValidationFailed

            raise ValidationFailed("import exceeds the 10000 record limit")
        refs = [record.external_ref for record in staged]
        if len(refs) != len(set(refs)):
            from identity_hub.errors import ValidationFailed

            raise ValidationFailed("import contains duplicate external_ref values")
        import_id = run_id or new_id()
        items = [
            NewImportItem(
                position=position,
                payload=self.hub.envelope.seal(
                    pydantic_core.to_json(_record_payload(record)),
                    _import_aad(import_id, position),
                ),
            )
            for position, record in enumerate(staged, start=1)
        ]
        async with self._tx() as tx:
            created = await tx.create_import_run(
                NewImportRun(
                    id=import_id,
                    service=svc.name,
                    actor=actor,
                    source=source[:256],
                    on_existing=mode.value,
                    verify_sessions=verify_sessions,
                    labels=batch_labels,
                    total=len(items),
                ),
                items,
            )
        if not created:
            from identity_hub.errors import AlreadyExists

            raise AlreadyExists("import_run", import_id)
        return import_id

    async def _cancel_import(self, run_id: UUID) -> bool:
        """Request cancellation; an in-flight batch is allowed to finish."""
        async with self._tx() as tx:
            return await tx.request_import_cancel(run_id) is not None

    import_many = legacy(_import_many, "import_many")
    import_files = legacy(_import_files, "import_files")
    stage_import = legacy(_stage_import, "stage_import")
    cancel_import = legacy(_cancel_import, "cancel_import")

    async def _import_batch(
        self,
        svc: Service,
        batch: list[ImportRecord],
        mode: OnExisting,
        verify_sessions: bool,
        hook: BatchHook | None,
        actor: str,
        report: ImportReport,
        run_id: UUID | None,
    ) -> None:
        from identity_hub.identities import prepare_identity

        ready: list[_Prepared] = []
        seen: set[str] = set()
        for record in batch:  # валидация и шифрование — до транзакции (FR-2.4, T-5)
            try:
                key = svc.normalize_external_ref(record.external_ref)
            except IdentityHubError as exc:
                report.failed.append((record.external_ref, str(exc)))
                continue
            if key in seen:  # дубль считается после нормализации (IMP-08)
                report.skipped.append((record.external_ref, "дубликат в пачке"))
                continue
            seen.add(key)
            try:
                sessions = dict(record.sessions)
                auth = AuthState.NO_SESSION
                if "default" in sessions and not verify_sessions:
                    auth = AuthState.AUTHENTICATED
                prepared = prepare_identity(
                    self,
                    svc,
                    external_ref=record.external_ref,
                    identity_id=record.id,
                    credentials=record.credentials,
                    sessions=sessions,
                    attributes=record.attributes,
                    profile=record.profile,
                    labels=record.labels,
                    tags=record.tags,
                    note=record.note,
                    lifecycle=Lifecycle.PROVISIONING,  # FR-2.5
                    auth_state=auth,
                )
                for factor in record.verifications:
                    svc.verification(factor)
                proxy = None
                if record.proxy:
                    pool = svc.proxy_policy.group if svc.proxy_policy else "import"
                    proxy = self._prepare_proxy(record.proxy, pool, None, None, None, None, None, 3, None)  # type: ignore[attr-defined]
                ready.append(_Prepared(record, prepared, proxy))
            except IdentityHubError as exc:
                report.failed.append((record.external_ref, str(exc)))
        if not ready:
            return

        created: list[UUID] = []
        updated: list[UUID] = []
        skipped: list[tuple[str, str]] = []
        failed: list[tuple[str, str]] = []
        try:
            async with self._tx(own=True) as tx:
                refs = [(svc.name, p.prepared.row.unique_ref) for p in ready]
                existing = await tx.find_by_refs(refs)
                by_id = await tx.existing_identities([p.prepared.row.id for p in ready if p.record.id])
                if mode is OnExisting.UPDATE_SESSION:
                    # обновляемые identity — одной блокировкой по возрастанию id (IMP-14): иначе
                    # две пачки с общими ref держат FOR SHARE и взаимно ждут повышения до UPDATE
                    await tx.lock_identities(
                        [found[0] for found in existing.values() if found[1] == tx.space.id]
                    )
                inserted: dict[str, UUID] = {}
                to_link: list[_Prepared] = []
                for item in ready:
                    row = item.prepared.row
                    found = existing.get((svc.name, row.unique_ref))
                    if found is not None or row.id in by_id:
                        await self._existing_record(tx, svc, item, found, mode, updated, skipped, failed)
                        continue
                    if not await tx.insert_identity(row):
                        failed.append((row.external_ref, "id занят другим аккаунтом"))
                        continue
                    await self._write_children(tx, item.prepared, source=ChangeSource.IMPORT)  # type: ignore[attr-defined]
                    if item.proxy is not None:
                        proxy_id, _ = await tx.insert_proxy(item.proxy)
                        await tx.bind_proxy(row.id, proxy_id, None)
                        await tx.adjust_proxy_load(proxy_id, 1)
                    for factor, state in item.record.verifications.items():
                        await self._initial_verification(tx, row.id, factor, state, actor)
                    inserted[row.unique_ref] = row.id
                    created.append(row.id)
                    if item.record.links:
                        to_link.append(item)
                await self._import_links(tx, svc, to_link, inserted)
                if created:
                    await self._emit(
                        tx,
                        EventDraft(
                            "identity.imported",
                            actor=actor,
                            service=svc.name,
                            data={
                                "count": len(created),
                                "updated": len(updated),
                                "skipped": len(skipped),
                                **({"run_id": str(run_id)} if run_id is not None else {}),
                            },
                        ),
                    )
        except Exception as exc:
            # Текст чужого исключения в лог и отчёт не идёт: в нём бывают параметры SQL и секреты.
            # Сообщения ошибок библиотеки безопасны по контракту (S-8) и объясняют причину отказа.
            logger.warning("пачка импорта отклонена целиком: %s", describe_error(exc))
            detail = f": {exc}" if isinstance(exc, IdentityHubError) else ""
            reason = f"пачка отклонена: {describe_error(exc)}{detail}"[:500]
            report.failed.extend((p.record.external_ref, reason) for p in ready)
            return
        report.created.extend(created)
        report.updated.extend(updated)
        report.skipped.extend(skipped)
        report.failed.extend(failed)
        if hook is not None and created:
            await self._run_hook(hook, created, report)

    async def _existing_record(
        self,
        tx: Any,
        svc: Service,
        item: _Prepared,
        found: tuple[UUID, UUID] | None,
        mode: OnExisting,
        updated: list[UUID],
        skipped: list[tuple[str, str]],
        failed: list[tuple[str, str]],
    ) -> None:
        ref = item.record.external_ref
        if found is not None and found[1] != tx.space.id:
            failed.append((ref, "аккаунт платформы уже в другом пространстве (FR-2.2)"))
            return
        if mode is OnExisting.FAIL:
            failed.append((ref, "уже существует"))
            return
        if mode is OnExisting.SKIP or found is None or not item.prepared.sessions:
            skipped.append((ref, "уже существует"))
            return
        identity_id = found[0]
        fencing = await tx.current_fencing(identity_id, lock=True)
        for slot, payload in item.prepared.sessions.items():
            # шифротекст привязан к id записи импорта через AAD — перешифровать под существующий id
            session = self.hub.envelope.open_json(payload, _aad(item.prepared.row.id, slot))
            resealed = self.hub.envelope.seal(_to_json(session), _aad(identity_id, slot))
            await tx.put_session(identity_id, slot, resealed, fencing=fencing or 0)
        from identity_hub.state import DEFAULT_SLOT, apply_region_change

        if DEFAULT_SLOT in item.prepared.sessions:
            await apply_region_change(
                self,
                tx,
                identity_id,
                "auth",
                DEFAULT_SLOT,
                Observation(
                    AuthState.AUTHENTICATED,
                    observed_at=await tx.now(),
                    source=ChangeSource.IMPORT,
                    actor="import",
                ),
            )
        updated.append(identity_id)

    async def _initial_verification(
        self, tx: Any, identity_id: UUID, factor: str, state: str, actor: str
    ) -> None:
        from identity_hub.verifications import apply_verification

        now = await tx.now()
        await apply_verification(
            self,
            tx,
            identity_id,
            factor,
            lambda flow, s, verified, _now: flow.reconcile(
                s, Observation(state, observed_at=now, source=ChangeSource.IMPORT, actor=actor), now
            ),
            actor=actor,
        )

    async def _import_links(
        self, tx: Any, svc: Service, items: list[_Prepared], inserted: dict[str, UUID]
    ) -> None:
        """Связи по external_ref: сначала из этой пачки, затем из БД (FR-2.4)."""
        if not items:
            return
        wanted: set[tuple[str, str]] = set()
        for item in items:
            for target in item.record.links.values():
                wanted.add(self._link_ref(svc, target))
        known = await tx.find_by_refs(sorted(wanted))
        for item in items:
            for role, target in item.record.links.items():
                service_name, ref = self._link_ref(svc, target)
                linked = inserted.get(ref) if service_name == svc.name else None
                if linked is None:
                    found = known.get((service_name, ref))
                    if found is None or found[1] != tx.space.id:
                        raise IdentityHubError(f"связь {role}: аккаунт {service_name}:{ref} не найден")
                    linked = found[0]
                await tx.insert_link(item.prepared.row.id, role, linked)

    def _link_ref(self, svc: Service, target: str) -> tuple[str, str]:
        """Ссылка `[service:]external_ref` → (сервис, нормализованный ref целевого сервиса)."""
        prefix, sep, rest = target.partition(":")
        if sep and prefix in {s.name for s in self.hub.services}:
            return prefix, self.hub.service(prefix).normalize_external_ref(rest)
        return svc.name, svc.normalize_external_ref(target)

    async def _run_hook(self, hook: BatchHook, created: list[UUID], report: ImportReport) -> None:
        sessionmaker = getattr(self.hub.store, "sessionmaker", None)
        try:
            if sessionmaker is None:
                await hook(None, list(created))
                return
            async with sessionmaker() as session, session.begin():
                await hook(session, list(created))
        except Exception:
            logger.exception("on_batch_committed упал; пачка сохранена, повторите хук для этих id")
            report.hook_failed.append(list(created))


class ImportRunner:
    """Dispatcher pump for committed, resumable admin import batches."""

    def __init__(
        self,
        hub: IdentityHub,
        *,
        batch_size: int = 100,
        claim_for: timedelta = timedelta(minutes=5),
    ) -> None:
        self.hub = hub
        self.batch_size = batch_size
        self.claim_for = claim_for
        self.worker = f"import:{id(self):x}"

    async def run_once(self) -> int:
        processed = 0
        for ref in await self.hub.store.maintenance_spaces():
            handle = self.hub.space(ref.slug, ref.title, space_id=ref.id)
            async with handle._tx(own=True) as tx:
                runs = await tx.active_import_runs(limit=1)
                if not runs:
                    continue
                run = runs[0]
                if run.state == "cancelling":
                    await tx.request_import_cancel(run.id)
                    continue
                claimed = await tx.claim_import_items(
                    run.id,
                    worker=self.worker,
                    limit=self.batch_size,
                    claim_for=self.claim_for,
                )
            if not claimed:
                continue
            results = await self._process(handle, run, claimed)
            async with handle._tx(own=True) as tx:
                await tx.finish_import_items(run.id, results)
            processed += len(results)
        return processed

    async def _process(self, handle: ImportingMixin, run: Any, claimed: list[Any]) -> list[ImportItemResult]:
        records: list[tuple[Any, ImportRecord]] = []
        results: list[ImportItemResult] = []
        for item in claimed:
            try:
                raw = self.hub.envelope.open_json(item.payload, _import_aad(run.id, item.position))
                records.append((item, _payload_record(raw)))
            except Exception as exc:
                results.append(
                    ImportItemResult(
                        item.position,
                        item.claim_token,
                        "failed",
                        error_code="staged_payload_invalid",
                        error=f"staged payload cannot be read ({type(exc).__name__})",
                    )
                )
        pending: list[tuple[Any, ImportRecord]] = []
        async with handle._tx(own=True) as tx:
            for item, record in records:
                existing = await tx.get_identity(record.id) if record.id is not None else None
                if (
                    existing is not None
                    and existing.service == run.service
                    and existing.normalized_ref
                    == handle.hub.service(run.service).normalize_external_ref(record.external_ref)
                ):
                    results.append(
                        ImportItemResult(
                            item.position,
                            item.claim_token,
                            "created",
                            identity_id=existing.id,
                            external_ref=record.external_ref,
                        )
                    )
                else:
                    pending.append((item, record))
        if not pending:
            return results
        report = await handle._import_many(
            service=run.service,
            records=[record for _, record in pending],
            batch_size=len(pending),
            on_existing=run.on_existing,
            verify_sessions=run.verify_sessions,
            actor=run.actor,
            run_id=run.id,
        )
        created = set(report.created)
        updated = set(report.updated)
        skipped = {external_ref: reason for external_ref, reason in report.skipped}
        failed = {external_ref: reason for external_ref, reason in report.failed}
        for item, record in pending:
            identity_id = record.id
            if identity_id in created:
                outcome = "created"
                reason = None
                code = None
            elif identity_id in updated:
                outcome = "updated"
                reason = None
                code = None
            elif record.external_ref in skipped:
                outcome = "skipped"
                reason = skipped[record.external_ref]
                code = "already_exists"
            else:
                outcome = "failed"
                reason = failed.get(record.external_ref, "record did not produce an import outcome")
                code = "record_rejected"
            results.append(
                ImportItemResult(
                    item.position,
                    item.claim_token,
                    outcome,
                    identity_id=identity_id if outcome in {"created", "updated"} else None,
                    external_ref=record.external_ref,
                    error_code=code,
                    error=reason,
                )
            )
        return results


def _record_payload(record: ImportRecord) -> dict[str, Any]:
    return {
        "external_ref": record.external_ref,
        "id": str(record.id) if record.id is not None else None,
        "attributes": dict(record.attributes),
        "profile": dict(record.profile),
        "labels": dict(record.labels),
        "tags": sorted(record.tags),
        "note": record.note,
        "credentials": dict(record.credentials) if record.credentials is not None else None,
        "sessions": {key: dict(value) for key, value in record.sessions.items()},
        "links": dict(record.links),
        "proxy": record.proxy,
        "verifications": dict(record.verifications),
    }


def _payload_record(value: Mapping[str, Any]) -> ImportRecord:
    raw_id = value.get("id")
    return ImportRecord(
        external_ref=str(value["external_ref"]),
        id=UUID(str(raw_id)) if raw_id else None,
        attributes=dict(value.get("attributes") or {}),
        profile=dict(value.get("profile") or {}),
        labels=dict(value.get("labels") or {}),
        tags=frozenset(value.get("tags") or ()),
        note=value.get("note"),
        credentials=dict(value["credentials"]) if value.get("credentials") is not None else None,
        sessions={key: dict(item) for key, item in (value.get("sessions") or {}).items()},
        links={str(key): str(item) for key, item in (value.get("links") or {}).items()},
        proxy=value.get("proxy"),
        verifications={str(key): str(item) for key, item in (value.get("verifications") or {}).items()},
    )


def _import_aad(run_id: UUID, position: int) -> bytes:
    return associated_data("ih_import_items", run_id, str(position))


def _aad(identity_id: UUID, slot: str) -> bytes:
    from identity_hub.crypto import associated_data

    return associated_data("ih_sessions", identity_id, slot)


def _to_json(value: Any) -> bytes:
    import pydantic_core

    return pydantic_core.to_json(value)


async def _aiter(
    records: Iterable[ImportRecord] | AsyncIterable[ImportRecord],
) -> AsyncIterable[ImportRecord]:
    if isinstance(records, AsyncIterable):
        async for record in records:
            yield record
    else:
        for record in records:
            yield record
