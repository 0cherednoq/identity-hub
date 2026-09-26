"""P1.6: данные аккаунта, search_text, парсер поиска."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from identity_hub.domain.data import (
    build_search_text,
    short_id,
    split_profile,
    validate_labels,
    validate_model,
    validate_tags,
)
from identity_hub.domain.query import MIN_TERM_LENGTH, parse_query
from identity_hub.errors import ValidationFailed
from tests.fixtures.services import FAKE_TG, TgCredentials, TgProfile

pytestmark = pytest.mark.unit

ID = UUID("01920000-0000-7000-8000-00000000abcd")


def test_labels_flat_scalars_only() -> None:
    assert validate_labels({"farm": "3", "level": 2, "vip": True, "score": 1.5}) == {
        "farm": "3",
        "level": 2,
        "vip": True,
        "score": 1.5,
    }
    with pytest.raises(ValidationFailed) as exc:
        validate_labels({"nested": {"a": 1}, "list": [1], "none": None, "bad key": "x"})
    assert len(exc.value.errors) == 4


def test_tags_are_unique_and_sorted() -> None:
    assert validate_tags(["parser", "a", "parser"]) == ["a", "parser"]
    with pytest.raises(ValidationFailed):
        validate_tags(["with space"])


def test_validate_model_collects_errors() -> None:
    assert isinstance(validate_model(TgCredentials, {"phone": "+1"}, "credentials"), TgCredentials)
    with pytest.raises(ValidationFailed) as exc:
        validate_model(TgCredentials, {"password": "x"}, "credentials")
    assert any("credentials.phone" in e for e in exc.value.errors)


def test_split_profile_plain_and_encrypted() -> None:
    profile = TgProfile(first_name="Olena", country="UA", birthday=date(1990, 5, 1))
    open_part, secret = split_profile(FAKE_TG, profile)
    assert open_part == {"first_name": "Olena", "country": "UA", "birthday": "1990-05-01"}
    assert secret is None
    encrypted = replace(FAKE_TG, profile_encrypted=True)
    open_part, secret = split_profile(encrypted, profile)
    assert open_part == {"country": "UA"}
    assert secret == {"first_name": "Olena", "birthday": "1990-05-01"}


def test_search_text() -> None:
    text = build_search_text(
        FAKE_TG,
        ID,
        "+380501112233",
        {"first_name": "Olena", "last_name": "Shevchenko", "country": "UA"},
        {"farm": 3, "vip": True},
        "Куплен  у Ивана",
    )
    assert text == "+380501112233 0000abcd olena shevchenko farm:3 vip:true куплен у ивана"
    assert short_id(ID) == "0000abcd"


def test_search_text_does_not_leak_encrypted_profile() -> None:
    encrypted = replace(FAKE_TG, profile_encrypted=True)
    text = build_search_text(encrypted, ID, "ref", {"first_name": "Olena", "country": "UA"}, {}, None)
    assert "olena" not in text


def test_parse_query_example_from_spec() -> None:
    q = parse_query('service:instagram status:active country:UA farm:3 olena ab "две слова" tag:parser')
    assert q.services == {"instagram"}
    assert q.regions == {"lifecycle": {"ready"}, "standing": {"good", "limited"}}
    assert q.filters == {"country": {"UA"}, "farm": {"3"}}
    assert q.tags == {"parser"}
    assert q.terms == ("olena", "две слова")
    assert q.exact_id is None


def test_parse_query_regions_status_and_uuid() -> None:
    q = parse_query(f"standing:banned standing:frozen status:cooling {ID}")
    assert q.regions == {"standing": {"banned", "frozen"}}
    assert q.any_region == {"cooling"}
    assert q.exact_id == ID
    assert parse_query(f"id:{ID.hex}").exact_id == ID
    assert parse_query("id:0000abcd").terms == ("0000abcd",)


def test_parse_query_tolerates_unbalanced_quotes() -> None:
    assert parse_query('olena "unfinished').terms == ("olena", "unfinished")
    assert parse_query("").is_empty


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=80))
def test_parse_query_never_fails_and_drops_short_terms(text: str) -> None:
    q = parse_query(text)
    assert all(len(term) >= MIN_TERM_LENGTH for term in q.terms)
