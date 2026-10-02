"""F1.8: ротация мастер-ключа доходит до конца и не теряет данные (R2-S08, R2-S09)."""

from __future__ import annotations

from typing import Any

import pytest
from tests.conftest import TEST_KEYS
from tests.fixtures.api import register

from identity_hub import IdentityHub, ImportRecord, SecretRef, SpaceHandle
from identity_hub.crypto import Envelope, StaticSecretsProvider

pytestmark = pytest.mark.conformance

K3 = bytes(range(32))


def _use_keys(hub: IdentityHub, keys: dict[str, bytes], active: str) -> None:
    """Процесс перезапущен с другой конфигурацией ключей."""
    hub._envelope = Envelope(StaticSecretsProvider(keys, active))


async def _account(space: SpaceHandle, ref: str) -> Any:
    return await register(
        space,
        service="fake_tg",
        external_ref=ref,
        credentials={"phone": ref, "password": f"pw-{ref}"},
        session={"auth_key": f"key-{ref}"},
    )


async def test_rotation_runs_until_nothing_is_left_under_old_key(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-S08: один вызов проходит все пачки, а не останавливается на первой неполной."""
    for n in range(5):
        await _account(space, f"+{n}")
    _use_keys(hub_under_test, TEST_KEYS, "k2")

    report = await hub_under_test.doctor.rotate_key(limit=2)

    assert (report.counts["credentials"], report.counts["sessions"]) == (5, 5)
    assert report.complete and report.remaining == {} and report.unreadable == ()
    assert await hub_under_test.doctor.inactive_keys() == {}
    again = await hub_under_test.doctor.rotate_key(limit=2)
    assert again.total == 0 and again.complete


async def test_unreadable_rows_are_skipped_and_reported(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-S08: строка под недоступным ключом не обрывает пачку и названа в отчёте."""
    lost = await _account(space, "+lost")  # записан под k1
    _use_keys(hub_under_test, TEST_KEYS, "k2")
    kept = await _account(space, "+kept")  # записан под k2
    _use_keys(hub_under_test, {"k2": TEST_KEYS["k2"], "k3": K3}, "k3")  # k1 убран раньше времени

    report = await hub_under_test.doctor.rotate_key(limit=1)

    assert not report.complete
    assert (report.counts["credentials"], report.counts["sessions"]) == (1, 1)
    assert {(row.table, row.id) for row in report.unreadable} == {
        ("credentials", str(lost.identity_id)),
        ("sessions", f"{lost.identity_id}/default"),
    }
    assert report.unreadable_total == 2
    assert report.remaining == {"credentials": {"k1": 1}, "sessions": {"k1": 1}}
    assert await hub_under_test.doctor.inactive_keys() == report.remaining
    revealed = await space.secrets.reveal(kept.identity_id, SecretRef.CREDENTIALS, actor="t", reason="тест")
    assert revealed["password"] == "pw-+kept"


async def test_staged_import_survives_rotation_and_old_key_removal(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-S09: незавершённый импорт переоборачивается вместе с остальными секретами."""
    run_id = await space.imports.stage(
        service="fake_tg",
        source="upload.csv",
        records=[
            ImportRecord(f"+st{n}", credentials={"phone": f"+st{n}", "password": "x"}) for n in range(3)
        ],
    )
    _use_keys(hub_under_test, TEST_KEYS, "k2")
    assert await hub_under_test.doctor.inactive_keys() == {"import_items": {"k1": 3}}

    report = await hub_under_test.doctor.rotate_key(limit=2)

    assert report.counts == {"import_items": 3} and report.complete
    _use_keys(hub_under_test, {"k2": TEST_KEYS["k2"]}, "k2")  # старый ключ удалён после ротации
    await hub_under_test.dispatcher.drain()
    run = await space.imports.get(run_id)
    assert (run.state, run.created, run.failed) == ("succeeded", 3, 0)
    assert await space.identities.count() == 3


async def test_import_with_undecryptable_item_fails_and_keeps_the_item(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-S09: без ключа прогон не «успешен», а зашифрованная запись не удаляется."""
    run_id = await space.imports.stage(
        service="fake_tg",
        source="upload.csv",
        records=[ImportRecord("+a", credentials={"phone": "+a"}), ImportRecord("+b")],
    )
    _use_keys(hub_under_test, {"k2": TEST_KEYS["k2"]}, "k2")  # ключ убран без ротации

    await hub_under_test.dispatcher.drain()

    run = await space.imports.get(run_id)
    assert (run.state, run.created, run.failed) == ("failed", 0, 2)
    assert {entry["code"] for entry in run.report["failed"]} == {"staged_payload_unreadable"}
    assert await space.identities.count() == 0
    # данные на месте: видны проверке ключей и переоборачиваются, когда ключ вернут
    assert await hub_under_test.doctor.inactive_keys() == {"import_items": {"k1": 2}}
    _use_keys(hub_under_test, TEST_KEYS, "k2")
    assert (await hub_under_test.doctor.rotate_key()).counts == {"import_items": 2}
