"""IMP-23 (I8.5): `space.imports` и `space.secrets` на MemoryStore и PostgreSQL."""

from __future__ import annotations

from uuid import uuid4

import pytest

from identity_hub import (
    IdentityCreate,
    ImportRecord,
    Lifecycle,
    NotFound,
    SecretRef,
    SpaceHandle,
    ValidationFailed,
)
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.conformance]


async def test_run_reports_and_resolves_links_by_ref(space: SpaceHandle) -> None:
    records = [
        ImportRecord(external_ref="+10", links={"recovery": "+11"}, tags=frozenset({"batch"})),
        ImportRecord(external_ref="+11", sessions={"default": {"auth_key": "k"}}),
    ]

    report = await space.imports.run(service="fake_tg", records=records, actor="test")
    again = await space.imports.run(service="fake_tg", records=records, actor="test")

    assert len(report.created) == 2 and report.failed == []
    assert [ref for ref, _ in again.skipped] == ["+10", "+11"]
    first = await space.identities.get_by_ref("fake_tg", "+10")
    second = await space.identities.get_by_ref("fake_tg", "+11")
    assert first.lifecycle is Lifecycle.PROVISIONING  # FR-2.5: импорт не переводит в ready
    card = await space.identities.card(first.id)
    assert [(link.role, link.identity_id) for link in card.links.outgoing] == [("recovery", second.id)]


async def test_run_partial_failure_keeps_valid_records(space: SpaceHandle) -> None:
    report = await space.imports.run(
        service="fake_tg",
        records=[
            ImportRecord(external_ref="+20"),
            ImportRecord(external_ref="+21", profile={"birthday": "x"}),
        ],
        actor="test",
    )
    assert len(report.created) == 1
    assert [ref for ref, _ in report.failed] == ["+21"]


async def test_stage_get_and_cancel(space: SpaceHandle) -> None:
    run_id = await space.imports.stage(
        service="fake_tg", records=[ImportRecord(external_ref="+30")], source="upload.csv", actor="test"
    )

    info = await space.imports.get(run_id)
    assert info.id == run_id and info.total == 1
    assert await space.imports.cancel(run_id) is True
    with pytest.raises(NotFound):
        await space.imports.get(uuid4())


async def test_secrets_describe_reveal_and_replace(space: SpaceHandle) -> None:
    identity = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="+40",
            credentials={"phone": "+40", "password": "old-pass"},
            sessions={"default": {"auth_key": "k1"}, "web": {"auth_key": "k2"}},
        ),
        actor="test",
    )

    card = await space.secrets.describe(identity.id)
    assert card.credential.present
    assert sorted(s.slot for s in card.sessions) == ["default", "web"]
    assert "old-pass" not in repr(card) and "k1" not in repr(card)

    creds = await space.secrets.reveal(identity.id, SecretRef.CREDENTIALS, actor="admin", reason="проверка")
    web = await space.secrets.reveal(identity.id, SecretRef.session("web"), actor="admin", reason="тест")
    legacy_string = await space.secrets.reveal(identity.id, "session:web", actor="admin", reason="тест")
    assert creds["password"] == "old-pass"
    assert web == legacy_string and web["auth_key"] == "k2"

    await space.secrets.replace_credentials(
        identity.id, {"phone": "+40", "password": "new-pass"}, actor="admin"
    )
    await space.secrets.replace_session(identity.id, {"auth_key": "k3"}, actor="admin", slot="web")
    assert (await space.secrets.reveal(identity.id, "credentials", actor="admin", reason="тест"))[
        "password"
    ] == "new-pass"
    assert (await space.secrets.reveal(identity.id, SecretRef.session("web"), actor="admin", reason="тест"))[
        "auth_key"
    ] == "k3"
    # каждое раскрытие — событие аудита; ревизию меняют только две замены (D-205)
    revealed = await space.admin.event_page(
        filters=EventFilters(type="identity.secret.revealed", identity_id=identity.id)
    )
    assert len(revealed.items) == 5
    assert (await space.identities.get(identity.id)).revision == identity.revision + 2


async def test_reveal_missing_and_unknown(space: SpaceHandle) -> None:
    identity = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+50"), actor="test"
    )
    with pytest.raises(NotFound):
        await space.secrets.reveal(identity.id, SecretRef.CREDENTIALS, actor="admin", reason="тест")
    with pytest.raises(ValidationFailed):
        await space.secrets.reveal(identity.id, "password", actor="admin", reason="тест")
    with pytest.raises(ValidationFailed):
        SecretRef.evidence("")


def test_secret_ref_string_forms() -> None:
    assert str(SecretRef.CREDENTIALS) == "credentials"
    assert str(SecretRef.session()) == "session:default"
    assert str(SecretRef.evidence("kyc")) == "evidence:kyc"
    assert SecretRef.parse("session:web") == SecretRef.session("web")


async def test_rejected_batch_report_has_no_foreign_exception_text(
    space: SpaceHandle, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """F1.3 (R2-S03): текст чужого исключения (SQL-параметры, секреты) не идёт в отчёт и лог."""

    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("[parameters: ('TOKEN-123', 'password=SECRET')]")

    monkeypatch.setattr(type(space), "_import_links", broken)
    report = await space.imports.run(
        service="fake_tg", records=[ImportRecord(external_ref="+20", links={"recovery": "+21"})], actor="test"
    )

    assert report.created == []
    assert [reason for _ref, reason in report.failed] == ["пачка отклонена: RuntimeError"]
    assert "TOKEN-123" not in caplog.text and "SECRET" not in caplog.text
    assert "пачка импорта отклонена целиком: RuntimeError" in caplog.text


async def test_missing_link_rejects_only_its_record_with_a_safe_reason(space: SpaceHandle) -> None:
    """R2-F08: несуществующая связь отклоняет свою запись, остальные создаются; ref в причину не идёт."""
    report = await space.imports.run(
        service="fake_tg",
        records=[
            ImportRecord(external_ref="+30", links={"recovery": "fake_tg:+404"}),
            ImportRecord(external_ref="+31"),
        ],
        actor="test",
    )
    assert len(report.created) == 1
    assert report.failed == [("+30", "связь recovery: связанный аккаунт не найден")]
