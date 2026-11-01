"""Диагностический допуск: проверить конкретный аккаунт, даже если нагрузку он не получает (D-335).

Допуск нагрузки (`OperationRule`) и право на явную проверку — разные оси. Блокирующее состояние
(`banned`, `retired`, пауза) не должно запрещать проверку самого факта этого состояния. Политику
объявляет сервис; применяется только к адресным операциям с `actor` и `reason`, не к фоновой сверке.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from identity_hub.enums import Availability, Lifecycle, Standing
from identity_hub.errors import Blocker, ValidationFailed

__all__ = ["DEFAULT_DIAGNOSTIC_POLICY", "BlockerKind", "DiagnosticPolicy"]


class BlockerKind(StrEnum):
    """Коды `Blocker.code` диагностики: почему проверка не выполнена (без обращения к провайдеру)."""

    LIFECYCLE = "lifecycle"
    AVAILABILITY = "availability"
    STANDING = "standing"
    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_EXPIRED = "credential_expired"
    CREDENTIAL_DISABLED = "credential_disabled"
    CREDENTIAL_REVOKED = "credential_revoked"
    PROXY = "proxy"
    BUSY = "busy"
    NOT_FOUND = "not_found"


def _normalize[E: StrEnum](name: str, enum: type[E], values: Iterable[E | str]) -> frozenset[E]:
    try:
        result = frozenset(enum(value) for value in values)
    except ValueError as exc:
        raise ValidationFailed(f"DiagnosticPolicy.{name}: неизвестное значение ({exc})") from None
    if not result:
        raise ValidationFailed(f"DiagnosticPolicy.{name}: нужно хотя бы одно значение")
    return result


@dataclass(frozen=True, slots=True)
class DiagnosticPolicy:
    """Какие аккаунты сервис допускает до явной проверки.

    По умолчанию проверяются `provisioning`, `ready` и `retired`; аккаунты на паузе и в охлаждении;
    любой `standing`, кроме `deleted`. Карантин и `deleted` — только если сервис перечислит их явно:
    карантин обычно значит «не трогать аккаунт у провайдера».
    """

    lifecycles: frozenset[Lifecycle] = frozenset({Lifecycle.PROVISIONING, Lifecycle.READY, Lifecycle.RETIRED})
    availability: frozenset[Availability] = frozenset(
        {Availability.AVAILABLE, Availability.PAUSED, Availability.COOLING}
    )
    standing: frozenset[Standing] = frozenset(Standing) - {Standing.DELETED}

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycles", _normalize("lifecycles", Lifecycle, self.lifecycles))
        object.__setattr__(self, "availability", _normalize("availability", Availability, self.availability))
        object.__setattr__(self, "standing", _normalize("standing", Standing, self.standing))

    def blockers(
        self, lifecycle: Lifecycle | str, availability: Availability | str, standing: Standing | str
    ) -> tuple[Blocker, ...]:
        """Оси, по которым состояние не допущено (`Blocker.code`, detail — значение); пусто — разрешено."""
        found: list[Blocker] = []
        if Lifecycle(lifecycle) not in self.lifecycles:
            found.append(Blocker(BlockerKind.LIFECYCLE, str(Lifecycle(lifecycle))))
        if Availability(availability) not in self.availability:
            found.append(Blocker(BlockerKind.AVAILABILITY, str(Availability(availability))))
        if Standing(standing) not in self.standing:
            found.append(Blocker(BlockerKind.STANDING, str(Standing(standing))))
        return tuple(found)


DEFAULT_DIAGNOSTIC_POLICY = DiagnosticPolicy()
