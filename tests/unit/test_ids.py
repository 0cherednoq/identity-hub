"""F4.6 (R2-D24): `new_id()` строго растёт в пределах процесса при любом генераторе UUIDv7."""

from __future__ import annotations

from uuid import UUID

import pytest

from identity_hub import _ids
from identity_hub._ids import new_id

pytestmark = pytest.mark.unit


def _v7(ms: int, counter: int) -> UUID:
    rand_a, rand_b = counter >> 62, counter & ((1 << 62) - 1)
    return UUID(int=(ms << 80) | (7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b)


def test_ids_grow_even_when_generator_repeats_or_goes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Генератор отдал повтор и значение из прошлой миллисекунды — new_id всё равно растёт."""
    sequence = iter([_v7(1000, 5), _v7(1000, 5), _v7(999, 9), _v7(1000, 3), _v7(1001, 0)])
    monkeypatch.setattr(_ids, "_uuid7", lambda: next(sequence))
    monkeypatch.setattr(_ids, "_last", 0)

    ids = [new_id() for _ in range(5)]

    assert ids == sorted(ids) and len(set(ids)) == 5
    assert all(value.version == 7 and value.variant == "specified in RFC 4122" for value in ids)
    assert ids[-1] == _v7(1001, 0)  # реальное время снова впереди — берётся значение генератора


def test_counter_overflow_moves_to_next_millisecond(monkeypatch: pytest.MonkeyPatch) -> None:
    top = (1 << 74) - 1
    sequence = iter([_v7(1000, top), _v7(1000, 0)])
    monkeypatch.setattr(_ids, "_uuid7", lambda: next(sequence))
    monkeypatch.setattr(_ids, "_last", 0)

    first, second = new_id(), new_id()

    assert second > first and second == _v7(1001, 0)
    assert second.version == 7


def test_real_generator_is_monotonic() -> None:
    ids = [new_id() for _ in range(20_000)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
