"""F6.3b (R2-F27): контексты адаптеров и импортёры файлов."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from identity_hub import (
    IdentityHub,
    ImportRecord,
    LoginContext,
    ProbeContext,
    ProbeResult,
    SpaceHandle,
    StaticSecretsProvider,
    ValidationFailed,
)
from identity_hub.clock import FakeClock
from identity_hub.enums import SessionStatus
from identity_hub.testing import MemoryStore, make_identity, make_service

pytestmark = pytest.mark.unit


class Sess(BaseModel):
    auth_key: str


async def _hub(**service: Any) -> tuple[SpaceHandle, Any]:
    svc = make_service("svc", session=Sess, **service)
    hub = IdentityHub(
        store=MemoryStore(),
        secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"),
        services=[svc],
        clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    await hub.start()
    return hub.space("s", "S", create=True), svc


# ---------------------------------------------------------------- контексты


async def test_probe_context_has_external_ref() -> None:
    seen: list[str] = []

    class AuthOnly:
        async def auth(self, ctx: ProbeContext) -> ProbeResult | None:
            seen.append(ctx.external_ref)
            return ProbeResult("authenticated")

    space, svc = await _hub(probes=AuthOnly())
    identity_id = await make_identity(
        space, service=svc, external_ref="+79990001122", session={"auth_key": "k"}
    )

    await space.sync.refresh(identity_id, ["auth"])

    assert seen == ["+79990001122"]


async def test_login_context_reads_previous_session() -> None:
    seen: list[BaseModel | None] = []

    async def login(ctx: LoginContext) -> BaseModel:
        seen.append(await ctx.session())
        return Sess(auth_key="new")

    class Dead:
        async def auth(self, ctx: ProbeContext) -> ProbeResult | None:
            return ProbeResult("session_invalid")

    space, svc = await _hub(authenticator=login, probes=Dead())
    identity_id = await make_identity(space, service=svc, session={"auth_key": "old"})
    await space.sync.refresh(identity_id, ["auth"])
    async with space._tx(own=True) as tx:
        await tx.set_session_status(identity_id, "default", SessionStatus.INVALID)
    fresh = await make_identity(space, service=svc, authenticated=False)

    await space.auth.login(identity_id)
    await space.auth.login(fresh)

    # недействительная сессия доступна при повторном входе (устройство, dc); у нового аккаунта — None
    assert seen == [Sess(auth_key="old"), None]


# ---------------------------------------------------------------- импортёры


class ListImporter:
    """Синхронный `read`, возвращающий список."""

    def can_import(self, path: Path) -> bool:
        return path.suffix == ".list"

    def read(self, path: Path) -> Iterable[ImportRecord]:
        return [ImportRecord(path.stem)]


class AsyncListImporter:
    """`async def read`, возвращающий список."""

    def can_import(self, path: Path) -> bool:
        return path.suffix == ".alist"

    async def read(self, path: Path) -> Iterable[ImportRecord]:
        return [ImportRecord(path.stem)]


async def test_importer_may_return_iterable(tmp_path: Path) -> None:
    space, svc = await _hub(importers=(ListImporter(), AsyncListImporter()))

    report = await space.imports.run_files(service=svc, paths=["one.list", "two.alist"], actor="test")

    assert len(report.created) == 2 and report.failed == []


def test_importer_without_callable_read_is_rejected() -> None:
    class NoRead:
        read = None

        def can_import(self, path: Path) -> bool:
            return True

    with pytest.raises(ValidationFailed) as exc:
        make_service("svc", session=Sess, importers=(NoRead(),))
    assert "NoRead" in str(exc.value.errors)


class FileImporter:
    """Читает файл с диска: одна строка — один аккаунт."""

    def can_import(self, path: Path) -> bool:
        return path.suffix == ".txt"

    def read(self, path: Path) -> Iterable[ImportRecord]:
        return [ImportRecord(line) for line in path.read_text().split()]


async def test_run_files_resolves_relative_paths_against_base_dir(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("+79990000001")
    (tmp_path.parent / "outside.txt").write_text("+79990000002")
    space, svc = await _hub(importers=(FileImporter(),))

    report = await space.imports.run_files(
        service=svc,
        paths=["a.txt", "../outside.txt", tmp_path.parent / "outside.txt"],
        base_dir=tmp_path,
        actor="test",
    )

    assert len(report.created) == 1
    assert dict(report.failed) == {"outside.txt": "файл вне base_dir"}
    assert len(report.failed) == 2  # и относительный выход, и абсолютный путь снаружи


async def test_run_files_limits_size(tmp_path: Path) -> None:
    (tmp_path / "small.txt").write_text("+79990000001")
    (tmp_path / "big.txt").write_text("+79990000002 " * 100)
    folder = tmp_path / "tdata.txt"
    folder.mkdir()
    (folder / "part").write_bytes(b"x" * 500)
    space, svc = await _hub(importers=(FileImporter(),))

    report = await space.imports.run_files(
        service=svc,
        paths=[tmp_path / "small.txt", tmp_path / "big.txt", folder, tmp_path / "missing.txt"],
        max_bytes=100,
        actor="test",
    )

    assert len(report.created) == 1
    assert dict(report.failed) == {
        "big.txt": "файл больше max_bytes",
        "tdata.txt": "файл больше max_bytes",  # каталог (tdata) — суммарный размер файлов
        "missing.txt": "файл не найден",
    }


async def test_run_files_validates_limits() -> None:
    space, svc = await _hub()
    with pytest.raises(ValidationFailed):
        await space.imports.run_files(service=svc, paths=[], max_bytes=0, actor="test")
