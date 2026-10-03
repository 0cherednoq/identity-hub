"""F3.8: одна запись не роняет импорт и не блокирует очередь (R2-F05, R2-S12, R2-F07–F09, R2-F24)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from identity_hub import (
    IdentityHub,
    ImportRecord,
    ProxyPolicy,
    SpaceHandle,
    StaticSecretsProvider,
    ValidationFailed,
)
from identity_hub.clock import FakeClock
from identity_hub.proxies import parse_proxy_url
from identity_hub.read.events import EventFilters
from identity_hub.testing import MemoryStore, make_identity, make_service

pytestmark = pytest.mark.unit


class Creds(BaseModel):
    phone: str
    password: SecretStr


class Sess(BaseModel):
    auth_key: str


async def _hub(**service: Any) -> tuple[IdentityHub, FakeClock, SpaceHandle, Any]:
    svc = make_service("svc", credentials=Creds, session=Sess, **service)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    hub = IdentityHub(
        store=MemoryStore(),
        secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"),
        services=[svc],
        clock=clock,
    )
    await hub.start()
    return hub, clock, hub.space("s", "S", create=True), svc


async def _events(space: SpaceHandle, type_: str) -> list[Any]:
    page = await space.admin.event_page(filters=EventFilters(type=type_))
    return list(page.items)


# ---------------------------------------------------------------- R2-S12


@pytest.mark.parametrize(
    "url",
    [
        "http://h:abc",
        "http://user:LEAKPW/tail@h:8080",
        "http://[user:LEAKPW@h:80",
        "http://user:LEAKPW@h:99999999",
    ],
)
def test_parse_proxy_url_never_raises_raw_value_error(url: str) -> None:
    """Разбор URL прокси даёт `ValidationFailed` без фрагмента пароля, а не сырой `ValueError`."""
    with pytest.raises(ValidationFailed) as exc:
        parse_proxy_url(url)
    assert "LEAKPW" not in str(exc.value) and "abc" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None


# ---------------------------------------------------------------- R2-F05 (E1, E4)


async def test_bad_proxy_url_fails_one_record_and_keeps_the_report() -> None:
    """E1: запись с негодным прокси — в `failed`; остальные созданы, отчёт не потерян."""
    _, _clock, space, svc = await _hub()
    records = [
        ImportRecord("a1"),
        ImportRecord("a2"),
        ImportRecord("a3", proxy="http://h:abc"),
        ImportRecord("a4"),
    ]

    report = await space.imports.run(service=svc, records=records, batch_size=2)

    assert len(report.created) == 3 and [ref for ref, _ in report.failed] == ["a3"]
    assert "abc" not in report.failed[0][1]
    assert await space.identities.count() == 3


async def test_unexpected_error_in_one_record_does_not_abort_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Предвалидация ловит любое исключение записи, а не только ошибки библиотеки."""
    _, _clock, space, svc = await _hub()
    original = svc.normalize_ref

    def broken(ref: str) -> str:
        if ref == "boom":
            raise RuntimeError("normalizer bug with secret-value")
        return original(ref)

    object.__setattr__(svc, "normalize_ref", broken)

    report = await space.imports.run(service=svc, records=[ImportRecord("ok"), ImportRecord("boom")])

    assert len(report.created) == 1
    assert report.failed == [("boom", "RuntimeError")]  # текст чужого исключения в отчёт не идёт


async def test_staged_poison_record_does_not_block_the_queue() -> None:
    """E4: прогон с негодной записью завершается; следующий прогон пространства обрабатывается."""
    hub, _clock, space, svc = await _hub()
    bad = await space.imports.stage(
        service=svc, records=[ImportRecord("d1", proxy="http://h:abc")], source="bad"
    )
    good = await space.imports.stage(service=svc, records=[ImportRecord("d2")], source="good")

    await hub.dispatcher.drain()

    first, second = await space.imports.get(bad), await space.imports.get(good)
    assert (first.state, first.failed) == ("succeeded", 1)
    assert (second.state, second.created) == ("succeeded", 1)


async def test_item_is_failed_after_three_claims_and_other_spaces_proceed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сбой обработки элемента: после трёх захватов он помечается `failed`; другое пространство не ждёт."""
    hub, clock, space, svc = await _hub()
    other = hub.space("zz-other", "Other", create=True)
    poisoned = await space.imports.stage(
        service=svc, records=[ImportRecord("p1"), ImportRecord("p2")], source="x"
    )
    healthy = await other.imports.stage(service=svc, records=[ImportRecord("h1")], source="y")
    original = type(space)._import_many

    async def flaky(self: Any, **kwargs: Any) -> Any:
        if self.slug == "s" and any(record.external_ref == "p2" for record in kwargs["records"]):
            raise RuntimeError("unexpected failure with secret-value")
        return await original(self, **kwargs)

    monkeypatch.setattr(type(space), "_import_many", flaky)

    for _ in range(4):
        await hub._import_runner.run_once()
        clock.advance(timedelta(minutes=6))  # захват элемента истекает

    assert (await other.imports.get(healthy)).state == "succeeded"
    run = await space.imports.get(poisoned)
    assert (run.state, run.created, run.failed) == ("succeeded", 1, 1)  # p1 создан отдельно от p2
    (entry,) = run.report["failed"]
    assert (entry["code"], entry["reason"]) == ("staged_item_failed", "RuntimeError")


# ---------------------------------------------------------------- R2-F08 (E2)


async def test_missing_link_rejects_only_its_record() -> None:
    _, _clock, space, svc = await _hub()
    records = [ImportRecord(f"b{n}") for n in range(5)]
    records.append(ImportRecord("b5", links={"email": "nope"}))
    records.append(ImportRecord("b6", links={"backup": "b0"}))  # связь с записью той же пачки
    records.append(ImportRecord("b7", links={"chain": "b5"}))  # цель сама отклонена

    report = await space.imports.run(service=svc, records=records)

    assert len(report.created) == 6
    assert sorted(ref for ref, _ in report.failed) == ["b5", "b7"]
    assert all("связь" in reason and "nope" not in reason for _, reason in report.failed)


# ---------------------------------------------------------------- R2-F09


async def test_import_respects_proxy_capacity() -> None:
    """Импорт не привязывает к прокси больше аккаунтов, чем `max_identities`."""
    _, _clock, space, svc = await _hub(proxy_policy=ProxyPolicy(group="g"))
    records = [ImportRecord(f"r{n}", proxy="http://u:p@10.0.0.1:80") for n in range(5)]

    report = await space.imports.run(service=svc, records=records, max_identities=2)

    assert len(report.created) == 2 and len(report.failed) == 3
    assert all("ёмкост" in reason for _, reason in report.failed)
    (proxy,) = (await space.proxies.list()).items
    assert (proxy.max_identities, proxy.bound_count) == (2, 2)

    again = await space.imports.run(
        service=svc, records=[ImportRecord("late", proxy="http://u:p@10.0.0.1:80")], max_identities=2
    )
    assert again.created == [] and len(again.failed) == 1  # прокси уже заполнен прошлым импортом


# ---------------------------------------------------------------- R2-F07 (E3), R2-F24


async def test_staged_update_session_reports_updated_and_honours_verify_sessions() -> None:
    """E3: фоновый `update_session` — исход `updated`; `verify_sessions` действует и для существующих."""
    hub, _clock, space, svc = await _hub()
    identity_id = await make_identity(space, service=svc, external_ref="c1", session={"auth_key": "old"})
    run_id = await space.imports.stage(
        service=svc,
        records=[ImportRecord("c1", sessions={"default": {"auth_key": "new"}})],
        source="f.csv",
        on_existing="update_session",
        verify_sessions=True,
    )

    await hub.dispatcher.drain()

    run = await space.imports.get(run_id)
    assert (run.state, run.created, run.updated, run.skipped, run.failed) == ("succeeded", 0, 1, 0, 0)
    assert str((await space.identities.get(identity_id)).auth) == "no_session"  # сессию нужно проверить
    revealed = await space.secrets.reveal(identity_id, "session", actor="t", reason="тест")
    assert revealed["auth_key"] == "new"
    (event,) = await _events(space, "identity.imported")  # пачка из одних обновлений тоже видна
    assert (event.data["count"], event.data["updated"]) == (0, 1)


async def test_update_session_without_verify_marks_authenticated() -> None:
    _, _clock, space, svc = await _hub()
    identity_id = await make_identity(space, service=svc, external_ref="c1", authenticated=False)

    report = await space.imports.run(
        service=svc,
        records=[ImportRecord(" c1 ", sessions={"default": {"auth_key": "new"}})],
        on_existing="update_session",
    )

    assert report.updated == [identity_id] and report.failed == []
    assert str((await space.identities.get(identity_id)).auth) == "authenticated"


# ---------------------------------------------------------------- R2-F24 (E5)


async def test_rejection_reason_names_the_field_without_the_value() -> None:
    hub, _clock, space, svc = await _hub()
    report = await space.imports.run(
        service=svc,
        records=[
            ImportRecord(
                "+79990001122", credentials={"phone": "+79990001122", "password": {"oops": "Sup3rSecret"}}
            ),
            ImportRecord("x2", credentials={"phone": 1}),
        ],
    )

    reasons = dict(report.failed)
    assert "credentials.password" in reasons["+79990001122"]
    assert "credentials.phone" in reasons["x2"] and "credentials.password" in reasons["x2"]
    assert "Sup3rSecret" not in str(report.failed)

    run_id = await space.imports.stage(
        service=svc,
        records=[ImportRecord("+79990001133", credentials={"phone": ["Sup3rSecret"]})],
        source="s",
    )
    await hub.dispatcher.drain()
    run = await space.imports.get(run_id)
    (entry,) = run.report["failed"]
    assert "credentials.phone" in entry["reason"] and "Sup3rSecret" not in str(run.report)


# ---------------------------------------------------------------- файлы


async def test_file_and_importer_errors_go_to_failed() -> None:
    class Importer:
        def can_import(self, path: Path) -> bool:
            return path.suffix == ".ok" or path.suffix == ".bad"

        async def read(self, path: Path) -> Any:
            if path.suffix == ".bad":
                raise OSError(f"cannot read {path} secret-path")
            yield ImportRecord(path.stem)

    _, _clock, space, svc = await _hub(importers=(Importer(),))

    report = await space.imports.run_files(
        service=svc, paths=["one.ok", "broken.bad", "unknown.txt", "two.ok"]
    )

    assert len(report.created) == 2
    assert dict(report.failed) == {"broken.bad": "OSError", "unknown.txt": "нет импортёра для файла"}
