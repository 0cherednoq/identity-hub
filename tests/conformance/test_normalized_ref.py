"""IMP-08 (I1.1): нормализация external_ref сервисом — MemoryStore и PostgreSQL."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from identity_hub import (
    AlreadyExists,
    IdentityCreate,
    IdentityHub,
    ImportRecord,
    Service,
    SpaceHandle,
    ValidationFailed,
)

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


def normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not 10 <= len(digits) <= 15:
        raise ValueError(f"invalid phone: {raw!r}")
    return "+" + digits


PHONE = Service(name="norm_phone", title="Phone", normalize_ref=normalize_phone, normalize_ref_version=1)
MAIL = Service(name="norm_mail", title="Mail")  # str.strip, версия 0


@pytest.fixture
async def space_n(hub_under_test: IdentityHub) -> SpaceHandle:
    for service in (PHONE, MAIL):
        if service.name not in {s.name for s in hub_under_test.services}:
            hub_under_test.register_service(service)
    await hub_under_test.start()
    return hub_under_test.space(f"n-{uuid4().hex[:8]}")


def _phone(ref: str, **kwargs: Any) -> IdentityCreate:
    return IdentityCreate(service=PHONE, external_ref=ref, **kwargs)


async def test_phone_formats_are_one_identity(space_n: SpaceHandle) -> None:
    created = await space_n.identities.create(_phone("+380 (50) 123-45-67"))
    again, was_created = await space_n.identities.ensure(_phone("380501234567"))

    assert was_created is False and again.id == created.id
    assert created.external_ref == "+380 (50) 123-45-67"  # хранится как ввели
    assert created.normalized_ref == "+380501234567"
    with pytest.raises(AlreadyExists):
        await space_n.identities.create(_phone("38 050 123 45 67"))
    found = await space_n.identities.find(service=PHONE, external_ref="+38-050-123-45-67")
    assert found is not None and found.id == created.id


async def test_invalid_ref_rejected_without_value_in_message(space_n: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed) as exc:
        await space_n.identities.create(_phone("12-34"))
    assert "12-34" not in str(exc.value)


async def test_duplicate_between_spaces(hub_under_test: IdentityHub, space_n: SpaceHandle) -> None:
    await space_n.identities.create(_phone("+1 202 555 0123"))
    other = hub_under_test.space(f"o-{uuid4().hex[:8]}")
    with pytest.raises(AlreadyExists):
        await other.identities.ensure(_phone("12025550123"))


async def test_create_many_detects_duplicates_after_normalization(space_n: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed, match="повтор"):
        await space_n.identities.create_many([_phone("+1 202 555 0001"), _phone("12025550001")])


async def test_import_reports_duplicates_and_resolves_links_after_normalization(space_n: SpaceHandle) -> None:
    report = await space_n.imports.run(
        service=PHONE,
        records=[
            ImportRecord(external_ref="+1 202 555 0002", links={"backup": "1-202-555-0003"}),
            ImportRecord(external_ref="12025550002"),
            ImportRecord(external_ref="+1 (202) 555-0003"),
            ImportRecord(external_ref="bad"),
        ],
    )

    assert len(report.created) == 2
    assert [ref for ref, _ in report.skipped] == ["12025550002"]
    assert [ref for ref, _ in report.failed] == ["bad"]
    source = await space_n.identities.get_by_ref(PHONE, "+12025550002")
    target = await space_n.identities.get_by_ref(PHONE, "+12025550003")
    card = await space_n.identities.card(source.id)
    assert [(link.role, link.identity_id) for link in card.links.outgoing] == [("backup", target.id)]


async def test_default_normalization_strips_whitespace(space_n: SpaceHandle) -> None:
    created = await space_n.identities.create(IdentityCreate(service=MAIL, external_ref="  user@mail.com "))
    assert created.normalized_ref == "user@mail.com"
    with pytest.raises(AlreadyExists):
        await space_n.identities.create(IdentityCreate(service=MAIL, external_ref="user@mail.com"))


async def test_renormalize_after_normalizer_change(hub_under_test: IdentityHub, space_n: SpaceHandle) -> None:
    first = await space_n.identities.create(IdentityCreate(service=MAIL, external_ref="User@Mail.com"))
    second = await space_n.identities.create(IdentityCreate(service=MAIL, external_ref="Other@Mail.com"))

    lowered = replace(MAIL, normalize_ref=lambda ref: ref.strip().lower(), normalize_ref_version=1)
    hub_under_test._services[MAIL.name] = lowered  # объявление сервиса сменилось в коде
    report = await hub_under_test.doctor.renormalize(MAIL)
    again = await hub_under_test.doctor.renormalize(MAIL)

    assert (report.updated, report.complete) == (2, True)
    assert again.updated == 0
    found = await space_n.identities.find(service=MAIL, external_ref="user@mail.com")
    assert found is not None and found.id == first.id
    assert (await space_n.identities.get(second.id)).normalized_ref == "other@mail.com"


async def test_renormalize_collision_changes_nothing(
    hub_under_test: IdentityHub, space_n: SpaceHandle
) -> None:
    upper = await space_n.identities.create(IdentityCreate(service=MAIL, external_ref="A@X.COM"))
    lower = await space_n.identities.create(IdentityCreate(service=MAIL, external_ref="a@x.com"))

    lowered = replace(MAIL, normalize_ref=lambda ref: ref.strip().lower(), normalize_ref_version=1)
    hub_under_test._services[MAIL.name] = lowered
    report = await hub_under_test.doctor.renormalize(MAIL)

    assert report.updated == 0 and not report.complete
    assert report.collisions == [sorted([upper.id, lower.id])]
    assert (await space_n.identities.get(upper.id)).normalized_ref == "A@X.COM"


def test_custom_normalizer_requires_version() -> None:
    with pytest.raises(ValidationFailed) as exc:
        Service(name="bad_norm", title="Bad", normalize_ref=str.lower)
    assert any("normalize_ref_version" in error for error in exc.value.errors)
