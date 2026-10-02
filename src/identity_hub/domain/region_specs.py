"""Спецификации регионов по диаграммам ТЗ §3.1–3.4, 3.7, 3.8.

Таблица `transitions` — допустимые **команды**. Наблюдения таблицу не проходят (FR-9.1).
"""

from __future__ import annotations

from identity_hub.domain.regions import RegionSpec
from identity_hub.enums import (
    AuthState,
    Availability,
    DeliveryStatus,
    Lifecycle,
    ProxyStatus,
    Severity,
    Standing,
)

__all__ = ["AUTH", "AVAILABILITY", "DELIVERY", "LIFECYCLE", "PROXY_HEALTH", "REGIONS", "STANDING"]


def _fs(*values: str) -> frozenset[str]:
    return frozenset(values)


# §3.1 — решения владельца, не синхронизируется
LIFECYCLE = RegionSpec(
    name="lifecycle",
    states=_fs(*Lifecycle),
    initial=Lifecycle.PROVISIONING,
    synced=False,
    event_type="identity.lifecycle.changed",
    transitions={
        Lifecycle.PROVISIONING: _fs(Lifecycle.READY, Lifecycle.RETIRED),
        Lifecycle.READY: _fs(Lifecycle.RETIRED),
        Lifecycle.RETIRED: _fs(Lifecycle.READY),
    },
    requires_reason=frozenset({(Lifecycle.RETIRED, Lifecycle.READY)}),
)

# §3.2 — положение у платформы
STANDING = RegionSpec(
    name="standing",
    states=_fs(*Standing),
    initial=Standing.GOOD,
    event_type="identity.standing.changed",
    transitions={
        Standing.GOOD: _fs(Standing.LIMITED, Standing.FROZEN, Standing.BANNED),
        Standing.LIMITED: _fs(Standing.GOOD, Standing.FROZEN, Standing.BANNED),
        Standing.FROZEN: _fs(Standing.APPEALING, Standing.DELETED, Standing.BANNED),
        Standing.APPEALING: _fs(Standing.GOOD, Standing.FROZEN),
        Standing.BANNED: _fs(Standing.GOOD),  # restore — только с причиной
        Standing.DELETED: _fs(),
    },
    requires_reason=frozenset({(Standing.BANNED, Standing.GOOD)}),
    severity={
        Standing.LIMITED: Severity.WARNING,
        Standing.APPEALING: Severity.WARNING,
        Standing.FROZEN: Severity.CRITICAL,
        Standing.BANNED: Severity.CRITICAL,
        Standing.DELETED: Severity.CRITICAL,
    },
    confirm_leaving=_fs(Standing.BANNED, Standing.FROZEN),  # FR-9.4, D-104
    sticky=_fs(Standing.BANNED),  # D-102
    sticky_exits=_fs(Standing.DELETED),
    on_expire={Standing.LIMITED: Standing.GOOD},
)

# §3.3 — вход; session_invalid — не бан, standing не трогает
AUTH = RegionSpec(
    name="auth",
    states=_fs(*AuthState),
    initial=AuthState.NO_SESSION,
    event_type="identity.auth.changed",
    transitions={
        AuthState.NO_SESSION: _fs(AuthState.LOGGING_IN, AuthState.AUTHENTICATED),
        # → session_invalid: вход начинался из session_invalid и был прерван — состояние возвращается
        AuthState.LOGGING_IN: _fs(
            AuthState.CHALLENGE, AuthState.AUTHENTICATED, AuthState.NO_SESSION, AuthState.SESSION_INVALID
        ),
        # challenge → authenticated: вход завершён вручную (mark_authenticated, FR-5.6)
        AuthState.CHALLENGE: _fs(AuthState.LOGGING_IN, AuthState.AUTHENTICATED, AuthState.NO_SESSION),
        AuthState.AUTHENTICATED: _fs(AuthState.SESSION_INVALID, AuthState.NO_SESSION),
        AuthState.SESSION_INVALID: _fs(AuthState.LOGGING_IN, AuthState.AUTHENTICATED, AuthState.NO_SESSION),
    },
    severity={AuthState.SESSION_INVALID: Severity.WARNING, AuthState.CHALLENGE: Severity.WARNING},
)

# §3.4 — внутреннее планирование, не синхронизируется; временные состояния снимает таймер
AVAILABILITY = RegionSpec(
    name="availability",
    states=_fs(*Availability),
    initial=Availability.AVAILABLE,
    synced=False,
    event_type="identity.availability.changed",
    transitions={
        Availability.AVAILABLE: _fs(Availability.PAUSED, Availability.COOLING, Availability.QUARANTINED),
        Availability.PAUSED: _fs(Availability.AVAILABLE, Availability.COOLING, Availability.QUARANTINED),
        Availability.COOLING: _fs(Availability.AVAILABLE, Availability.QUARANTINED),
        Availability.QUARANTINED: _fs(Availability.AVAILABLE),
    },
    severity={Availability.QUARANTINED: Severity.WARNING},
    on_expire={Availability.PAUSED: Availability.AVAILABLE, Availability.COOLING: Availability.AVAILABLE},
)

# §3.7 — здоровье прокси
PROXY_HEALTH = RegionSpec(
    name="proxy",
    states=_fs(*ProxyStatus),
    initial=ProxyStatus.ALIVE,
    event_type="proxy.status.changed",
    transitions={
        ProxyStatus.ALIVE: _fs(ProxyStatus.QUARANTINED),
        ProxyStatus.QUARANTINED: _fs(ProxyStatus.ALIVE, ProxyStatus.DEAD),
        ProxyStatus.DEAD: _fs(ProxyStatus.ALIVE),
    },
    severity={ProxyStatus.QUARANTINED: Severity.WARNING, ProxyStatus.DEAD: Severity.CRITICAL},
    confirm_leaving=_fs(ProxyStatus.QUARANTINED, ProxyStatus.DEAD),
)

# §3.8 — доставка вебхука / уведомления (события не порождает)
DELIVERY = RegionSpec(
    name="delivery",
    states=_fs(*DeliveryStatus),
    initial=DeliveryStatus.PENDING,
    synced=False,
    event_type="delivery.status.changed",
    transitions={
        DeliveryStatus.PENDING: _fs(DeliveryStatus.SUCCEEDED, DeliveryStatus.FAILED, DeliveryStatus.PAUSED),
        DeliveryStatus.FAILED: _fs(DeliveryStatus.PENDING, DeliveryStatus.DEAD, DeliveryStatus.PAUSED),
        DeliveryStatus.DEAD: _fs(DeliveryStatus.PENDING),
        # результат попытки, начатой до отключения endpoint'а, ещё может прийти
        DeliveryStatus.PAUSED: _fs(DeliveryStatus.PENDING, DeliveryStatus.SUCCEEDED, DeliveryStatus.DEAD),
        DeliveryStatus.SUCCEEDED: _fs(),
    },
)

REGIONS: dict[str, RegionSpec] = {spec.name: spec for spec in (LIFECYCLE, STANDING, AUTH, AVAILABILITY)}
"""Регионы аккаунта (verification — отдельная машина на фактор, P1.3)."""
