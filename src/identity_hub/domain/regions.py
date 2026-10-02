"""Машины регионов состояния (ТЗ §3, FR-9).

Состояние аккаунта — несколько ортогональных регионов. Каждый регион описан `RegionSpec`;
`apply()` — чистая функция: текущее состояние + изменение → `RegionDecision`
(новое состояние, события, исход). Хранилище применяет решение атомарно.

Три вида изменений (FR-9.1) и таймер:
- `Command` — наше намерение, проходит таблицу переходов и guard-ы;
- `Observation` — факт от платформы, применяется из любого состояния (кроме sticky, D-102);
- `Override` — ручная правка, обязательны причина и автор, опционально закрепление `pin_for`;
- `Unpin` — явное снятие закрепления с обязательными причиной и автором;
- `Timer` — истёк срок `until` временного состояния.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from identity_hub.domain.redaction import sanitize_text
from identity_hub.enums import ChangeSource, Severity
from identity_hub.errors import TransitionNotAllowed, ValidationFailed

__all__ = [
    "CODE_LIMIT",
    "RAW_STATUS_LIMIT",
    "REASON_LIMIT",
    "Change",
    "Command",
    "Observation",
    "Outcome",
    "Override",
    "RegionDecision",
    "RegionEvent",
    "RegionSpec",
    "RegionState",
    "Timer",
    "Unpin",
    "apply",
]

# размеры колонок ih_region_states: текст длиннее не помещается и не нужен
CODE_LIMIT = 64
RAW_STATUS_LIMIT = 128
REASON_LIMIT = 1000

OBSERVATION_SOURCES = frozenset(
    {ChangeSource.PROBE, ChangeSource.ERROR, ChangeSource.WEBHOOK, ChangeSource.IMPORT}
)


# ---------------------------------------------------------------- изменения


@dataclass(frozen=True, slots=True)
class Command:
    """Наше намерение (FR-9.1): проходит таблицу переходов."""

    to: str
    actor: str
    code: str | None = None
    until: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Observation:
    """Факт от платформы. `state=None` — статус платформы не распознан (FR-9.6)."""

    state: str | None
    observed_at: datetime
    source: ChangeSource = ChangeSource.PROBE
    raw_status: str | None = None
    code: str | None = None
    until: datetime | None = None
    actor: str = "platform"

    def __post_init__(self) -> None:
        if self.source not in OBSERVATION_SOURCES:
            raise ValueError(f"источник наблюдения {self.source} недопустим")


@dataclass(frozen=True, slots=True)
class Override:
    """Ручная правка человеком: причина и автор обязательны (FR-9.1, CHECK в ih_region_states)."""

    to: str
    reason: str
    actor: str
    pin_for: timedelta | None = None
    code: str | None = None
    until: datetime | None = None


@dataclass(frozen=True, slots=True)
class Unpin:
    """Снять ручное закрепление, не меняя наблюдаемое состояние региона."""

    reason: str
    actor: str


@dataclass(frozen=True, slots=True)
class Timer:
    """Истёк срок временного состояния (`until <= now`)."""

    actor: str = "timer"


Change = Command | Observation | Override | Unpin | Timer


# ---------------------------------------------------------------- состояние и решение


@dataclass(frozen=True, slots=True)
class RegionState:
    """Снимок строки ih_region_states (или денормализованного региона на ih_identities)."""

    state: str
    changed_at: datetime
    version: int = 1
    code: str | None = None
    until: datetime | None = None
    change_source: ChangeSource = ChangeSource.COMMAND
    changed_by: str | None = None
    change_reason: str | None = None
    synced_at: datetime | None = None
    raw_status: str | None = None
    observed_at: datetime | None = None
    pending_observation: str | None = None
    pinned_until: datetime | None = None

    def is_pinned(self, now: datetime) -> bool:
        return self.pinned_until is not None and self.pinned_until > now


class Outcome(StrEnum):
    APPLIED = "applied"  # состояние или код изменились
    UNCHANGED = "unchanged"  # подтверждение текущего состояния (обновлены метаданные сверки)
    STALE = "stale"  # наблюдение старше последнего изменения (FR-9.2) — отброшено целиком
    UNKNOWN_STATUS = "unknown_status"  # статус платформы не распознан (FR-9.6)
    PINNED = "pinned"  # действует закрепление override (FR-9.3)
    STICKY = "sticky"  # наблюдение не выводит из sticky-состояния (D-102)
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # первое наблюдение выхода (FR-9.4)
    NOT_DUE = "not_due"  # таймер: срок ещё не вышел или его нет


@dataclass(frozen=True, slots=True)
class RegionEvent:
    """Событие, порождённое переходом. Тип и severity — из спецификации региона."""

    type: str
    severity: Severity
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RegionDecision:
    outcome: Outcome
    previous: RegionState
    state: RegionState
    events: tuple[RegionEvent, ...] = ()
    warning: str | None = None

    @property
    def changed(self) -> bool:
        """Нужно ли записывать строку (изменились состояние или метаданные)."""
        return self.state != self.previous

    @property
    def transitioned(self) -> bool:
        return self.state.state != self.previous.state


# ---------------------------------------------------------------- спецификация


Normalizer = Callable[[str, str], str]
"""(текущее состояние, наблюдаемое) → целевое, например verified → missing ⇒ revoked (FR-9.5)."""


@dataclass(frozen=True)
class RegionSpec:
    name: str
    states: frozenset[str]
    initial: str
    transitions: Mapping[str, frozenset[str]]
    event_type: str
    synced: bool = True
    severity: Mapping[str, Severity] = field(default_factory=dict)
    confirm_leaving: frozenset[str] = frozenset()
    sticky: frozenset[str] = frozenset()
    sticky_exits: frozenset[str] = frozenset()
    requires_reason: frozenset[tuple[str, str]] = frozenset()
    on_expire: Mapping[str, str] = field(default_factory=dict)
    normalize: Normalizer | None = None

    def __post_init__(self) -> None:
        unknown = {self.initial} - self.states
        for src, dsts in self.transitions.items():
            unknown |= ({src} | dsts) - self.states
        unknown |= set(self.on_expire) - self.states | set(self.on_expire.values()) - self.states
        unknown |= (self.confirm_leaving | self.sticky | self.sticky_exits) - self.states
        if unknown:
            raise ValueError(f"регион {self.name}: неизвестные состояния {sorted(unknown)}")

    def initial_state(self, now: datetime, source: ChangeSource = ChangeSource.COMMAND) -> RegionState:
        return RegionState(state=self.initial, changed_at=now, change_source=source)

    def can_command(self, src: str, dst: str) -> bool:
        return dst in self.transitions.get(src, frozenset())

    def severity_of(self, state: str) -> Severity:
        return self.severity.get(state, Severity.INFO)


# ---------------------------------------------------------------- применение


def _sanitized(change: Change) -> Change:
    """Свободный текст изменения — без встроенных секретов и в размер колонок (S-8, R2-S06).

    `raw_status`, `code` и `reason` попадают во внешние события, вебхуки, карточку и read-схему.
    """
    match change:
        case Observation():
            return replace(
                change,
                raw_status=sanitize_text(change.raw_status, limit=RAW_STATUS_LIMIT),
                code=sanitize_text(change.code, limit=CODE_LIMIT),
            )
        case Command():
            return replace(
                change,
                code=sanitize_text(change.code, limit=CODE_LIMIT),
                reason=sanitize_text(change.reason, limit=REASON_LIMIT),
            )
        case Override():
            return replace(
                change,
                code=sanitize_text(change.code, limit=CODE_LIMIT),
                reason=sanitize_text(change.reason, limit=REASON_LIMIT) or "",
            )
        case Unpin():
            return replace(change, reason=sanitize_text(change.reason, limit=REASON_LIMIT) or "")
    return change


def apply(
    spec: RegionSpec, current: RegionState, change: Change, now: datetime, key: str = ""
) -> RegionDecision:
    """Применить изменение к региону. Не бросает для наблюдений; бросает для некорректных команд/override."""
    change = _sanitized(change)
    match change:
        case Command():
            return _apply_command(spec, current, change, now, key)
        case Observation():
            return _apply_observation(spec, current, change, now, key)
        case Override():
            return _apply_override(spec, current, change, now, key)
        case Unpin():
            return _apply_unpin(spec, current, change, now, key)
        case Timer():
            return _apply_timer(spec, current, change, now, key)


def _apply_command(
    spec: RegionSpec, cur: RegionState, cmd: Command, now: datetime, key: str
) -> RegionDecision:
    _check_state(spec, cmd.to)
    if cmd.to == cur.state:
        # идемпотентный повтор; срок только удлиняется (FR-7.4)
        until = cur.until
        if cmd.until is not None and (until is None or cmd.until > until):
            until = cmd.until
        code = cmd.code if cmd.code is not None else cur.code
        if until == cur.until and code == cur.code:
            return RegionDecision(Outcome.UNCHANGED, cur, cur)
        new = replace(cur, until=until, code=code, version=cur.version + 1)
        events = _changed_events(spec, cur, new, key) if code != cur.code else ()
        return RegionDecision(Outcome.APPLIED, cur, new, events)
    if not spec.can_command(cur.state, cmd.to):
        raise TransitionNotAllowed(spec.name, cur.state, cmd.to)
    if (cur.state, cmd.to) in spec.requires_reason and not cmd.reason:
        raise TransitionNotAllowed(spec.name, cur.state, cmd.to, "нужна причина")
    new = replace(
        cur,
        state=cmd.to,
        code=cmd.code,
        until=cmd.until,
        changed_at=now,
        change_source=ChangeSource.COMMAND,
        changed_by=cmd.actor,
        change_reason=cmd.reason,
        pending_observation=None,
        version=cur.version + 1,
    )
    return RegionDecision(Outcome.APPLIED, cur, new, _changed_events(spec, cur, new, key))


def _apply_observation(
    spec: RegionSpec, cur: RegionState, obs: Observation, now: datetime, key: str
) -> RegionDecision:
    if not spec.synced:
        raise ValidationFailed(f"регион {spec.name} не синхронизируется: наблюдения не принимаются")
    if obs.observed_at < cur.changed_at:
        return RegionDecision(Outcome.STALE, cur, cur)

    synced = replace(
        cur,
        synced_at=now,
        raw_status=obs.raw_status,
        observed_at=obs.observed_at,
        version=cur.version + 1,
    )
    if obs.state is None or obs.state not in spec.states:
        warning = f"регион {spec.name}: неизвестный статус платформы {obs.raw_status or obs.state!r}"
        return RegionDecision(Outcome.UNKNOWN_STATUS, cur, synced, warning=warning)

    target = spec.normalize(cur.state, obs.state) if spec.normalize else obs.state
    same = target == cur.state and (obs.code is None or obs.code == cur.code)

    if cur.is_pinned(now):
        events = () if same else (_drift_event(spec, cur, target, obs, key, "pinned"),)
        return RegionDecision(Outcome.PINNED, cur, synced, events)
    if same:
        # подтверждение текущего состояния сбрасывает ожидание выхода (FR-9.4: «подряд»)
        new = replace(synced, pending_observation=None, until=obs.until or cur.until)
        return RegionDecision(Outcome.UNCHANGED, cur, new)
    if cur.state in spec.sticky and target != cur.state and target not in spec.sticky_exits:
        return RegionDecision(
            Outcome.STICKY, cur, synced, (_drift_event(spec, cur, target, obs, key, "sticky"),)
        )
    if cur.state in spec.confirm_leaving and target != cur.state and cur.pending_observation != target:
        return RegionDecision(Outcome.AWAITING_CONFIRMATION, cur, replace(synced, pending_observation=target))

    changed_at = max(cur.changed_at, min(obs.observed_at, now))  # D-103
    new = replace(
        synced,
        state=target,
        code=obs.code,
        until=obs.until,
        changed_at=changed_at,
        change_source=obs.source,
        changed_by=obs.actor,
        change_reason=None,
        pending_observation=None,
    )
    return RegionDecision(Outcome.APPLIED, cur, new, _changed_events(spec, cur, new, key))


def _apply_override(
    spec: RegionSpec, cur: RegionState, ov: Override, now: datetime, key: str
) -> RegionDecision:
    _check_state(spec, ov.to)
    if not ov.reason or not ov.reason.strip():
        raise ValidationFailed("override требует причину")
    if not ov.actor or not ov.actor.strip():
        raise ValidationFailed("override требует автора")
    new = replace(
        cur,
        state=ov.to,
        code=ov.code,
        until=ov.until,
        changed_at=now,
        change_source=ChangeSource.OVERRIDE,
        changed_by=ov.actor,
        change_reason=ov.reason,
        pending_observation=None,
        pinned_until=now + ov.pin_for if ov.pin_for else None,
        version=cur.version + 1,
    )
    overridden = RegionEvent(
        type="identity.state.overridden",
        severity=Severity.WARNING,
        data={
            "region": spec.name,
            "key": key,
            "from": cur.state,
            "to": ov.to,
            "code": ov.code,
            "reason": ov.reason,
            "actor": ov.actor,
            "pinned_until": new.pinned_until.isoformat() if new.pinned_until else None,
        },
    )
    events = (*_changed_events(spec, cur, new, key), overridden) if new.state != cur.state else (overridden,)
    return RegionDecision(Outcome.APPLIED, cur, new, events)


def _apply_unpin(
    spec: RegionSpec, cur: RegionState, command: Unpin, now: datetime, key: str
) -> RegionDecision:
    if not command.reason or not command.reason.strip():
        raise ValidationFailed("unpin требует причину")
    if not command.actor or not command.actor.strip():
        raise ValidationFailed("unpin требует автора")
    if cur.pinned_until is None:
        raise TransitionNotAllowed(spec.name, cur.state, cur.state, "регион не закреплён")
    new = replace(
        cur,
        changed_at=now,
        change_source=ChangeSource.OVERRIDE,
        changed_by=command.actor,
        change_reason=command.reason,
        pinned_until=None,
        version=cur.version + 1,
    )
    event = RegionEvent(
        type="identity.state.unpinned",
        severity=Severity.WARNING,
        data={
            "region": spec.name,
            "key": key,
            "state": cur.state,
            "reason": command.reason,
            "actor": command.actor,
        },
    )
    return RegionDecision(Outcome.APPLIED, cur, new, (event,))


def _apply_timer(spec: RegionSpec, cur: RegionState, timer: Timer, now: datetime, key: str) -> RegionDecision:
    target = spec.on_expire.get(cur.state)
    if target is None or cur.until is None or cur.until > now:
        return RegionDecision(Outcome.NOT_DUE, cur, cur)
    if cur.pinned_until is not None and cur.pinned_until > now:
        # закрепление защищает от всех автоматических изменений, включая таймер (R2-F14)
        return RegionDecision(Outcome.PINNED, cur, cur)
    new = replace(
        cur,
        state=target,
        code=None,
        until=None,
        changed_at=now,
        change_source=ChangeSource.TIMER,
        changed_by=timer.actor,
        change_reason=None,
        pending_observation=None,
        version=cur.version + 1,
    )
    return RegionDecision(Outcome.APPLIED, cur, new, _changed_events(spec, cur, new, key))


# ---------------------------------------------------------------- вспомогательное


def _check_state(spec: RegionSpec, state: str) -> None:
    if state not in spec.states:
        raise ValidationFailed(f"регион {spec.name}: неизвестное состояние {state!r}")


def _changed_events(
    spec: RegionSpec, cur: RegionState, new: RegionState, key: str
) -> tuple[RegionEvent, ...]:
    return (
        RegionEvent(
            type=spec.event_type,
            severity=spec.severity_of(new.state),
            data={
                "region": spec.name,
                "key": key,
                "from": cur.state,
                "to": new.state,
                "code": new.code,
                "until": new.until.isoformat() if new.until else None,
                "source": str(new.change_source),
                "actor": new.changed_by,
                "reason": new.change_reason,
                "raw_status": new.raw_status if new.change_source in OBSERVATION_SOURCES else None,
            },
        ),
    )


def _drift_event(
    spec: RegionSpec, cur: RegionState, target: str, obs: Observation, key: str, cause: str
) -> RegionEvent:
    """Платформа сообщает не то, что зафиксировано (FR-9.3)."""
    return RegionEvent(
        type="identity.state.drift",
        severity=Severity.WARNING,
        data={
            "region": spec.name,
            "key": key,
            "state": cur.state,
            "observed": target,
            "raw_status": obs.raw_status,
            "source": str(obs.source),
            "cause": cause,
        },
    )
