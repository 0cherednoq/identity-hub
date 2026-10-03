"""Unified, secret-free queue for facts that require human attention."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.errors import ValidationFailed
from identity_hub.store.base import AttentionIdentityRow

QueueKind = Literal["challenge", "requirement", "manual_review", "drift", "quarantine"]
QueueUrgency = Literal["critical", "high", "medium"]


@dataclass(frozen=True, slots=True)
class HumanQueueItem:
    id: str
    kind: QueueKind
    identity_id: UUID
    service: str
    external_ref: str
    key: str
    reason: str
    urgency: QueueUrgency
    deadline: datetime | None
    expired: bool
    occurred_at: datetime
    action_tab: Literal["state", "verification"]
    action_available: bool
    action_unavailable_reason: str
    details: dict[str, str | int | float | bool | None]


@dataclass(frozen=True, slots=True)
class HumanQueuePage:
    items: tuple[HumanQueueItem, ...]
    next_cursor: str | None
    counts: dict[str, int]


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _urgency(kind: QueueKind, deadline: datetime | None, now: datetime) -> QueueUrgency:
    if deadline is not None:
        remaining = deadline - now
        if remaining <= timedelta(hours=1):
            return "critical"
        if remaining <= timedelta(hours=24):
            return "high"
    if kind in ("manual_review", "drift", "quarantine"):
        return "high"
    return "medium"


def _item(
    fact: AttentionIdentityRow,
    *,
    kind: QueueKind,
    key: str,
    reason: str,
    deadline: datetime | None,
    occurred_at: datetime,
    now: datetime,
    tab: Literal["state", "verification"],
    details: dict[str, str | int | float | bool | None],
) -> HumanQueueItem:
    expired = deadline is not None and deadline <= now
    action_available = kind in ("requirement", "manual_review", "drift", "quarantine") or (
        kind == "challenge" and not expired
    )
    return HumanQueueItem(
        id=f"{kind}:{fact.id}:{key}",
        kind=kind,
        identity_id=fact.id,
        service=fact.service,
        external_ref=fact.external_ref,
        key=key,
        reason=reason,
        urgency=_urgency(kind, deadline, now),
        deadline=deadline,
        expired=expired,
        occurred_at=occurred_at,
        action_tab=tab,
        action_available=action_available,
        action_unavailable_reason=(
            "Откройте карточку для безопасного действия с проверкой revision."
            if action_available
            else "Истёкший challenge не принимает ответы."
        ),
        details=details,
    )


def _items(fact: AttentionIdentityRow, now: datetime, service: Any) -> list[HumanQueueItem]:
    result: list[HumanQueueItem] = []
    if fact.challenge is not None:
        challenge = fact.challenge
        result.append(
            _item(
                fact,
                kind="challenge",
                key=str(challenge.id),
                reason=f"Открытый challenge: {challenge.kind}",
                deadline=challenge.expires_at,
                occurred_at=challenge.created_at,
                now=now,
                tab="verification",
                details={"kind": challenge.kind, "hint": challenge.hint},
            )
        )
    for requirement in fact.requirements:
        code = requirement.get("code")
        if not isinstance(code, str):
            continue
        result.append(
            _item(
                fact,
                kind="requirement",
                key=code,
                reason=f"Требование платформы: {code}",
                deadline=_datetime(requirement.get("deadline")),
                occurred_at=_datetime(requirement.get("since")) or fact.updated_at,
                now=now,
                tab="verification",
                details={"code": code, "instruction": None},
            )
        )
    factor_specs = {item.name: item for item in service.verifications}
    for region, key, state in fact.regions:
        if region == "verification" and state.state == "under_review":
            spec = factor_specs.get(key)
            if spec is not None and str(spec.mode) == "manual":
                result.append(
                    _item(
                        fact,
                        kind="manual_review",
                        key=key,
                        reason=f"Фактор {key} ждёт ручного решения",
                        deadline=state.until,
                        occurred_at=state.changed_at,
                        now=now,
                        tab="verification",
                        details={"factor": key, "code": state.code},
                    )
                )
        if (
            state.pinned_until is not None
            and state.pinned_until > now
            and state.raw_status is not None
            and state.raw_status != state.state
        ):
            label = f"{region}:{key}" if key else region
            result.append(
                _item(
                    fact,
                    kind="drift",
                    key=label,
                    reason=f"Закреплено {state.state}, платформа сообщает {state.raw_status}",
                    deadline=state.pinned_until,
                    occurred_at=state.changed_at,
                    now=now,
                    tab="state",
                    details={
                        "region": label,
                        "current": state.state,
                        "observed": state.raw_status,
                    },
                )
            )
    if fact.availability == "quarantined":
        result.append(
            _item(
                fact,
                kind="quarantine",
                key="availability",
                reason="Аккаунт находится в карантине",
                deadline=None,
                occurred_at=fact.updated_at,
                now=now,
                tab="state",
                details={"error_score": fact.error_score},
            )
        )
    return result


_URGENCY_RANK = {"critical": 0, "high": 1, "medium": 2}
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


def _sort_key(item: HumanQueueItem) -> tuple[int, datetime, str]:
    return (_URGENCY_RANK[item.urgency], item.deadline or _FAR_FUTURE, item.id)


def _encode_cursor(item: HumanQueueItem) -> str:
    rank, deadline, item_id = _sort_key(item)
    raw = json.dumps(
        {"rank": rank, "deadline": deadline.isoformat(), "id": item_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[int, datetime, str]:
    if not 8 <= len(value) <= 2048:
        raise ValidationFailed("cursor очереди некорректен")
    try:
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        rank = int(data["rank"])
        deadline = datetime.fromisoformat(data["deadline"])
        item_id = str(data["id"])
        if rank not in _URGENCY_RANK.values() or deadline.tzinfo is None or not item_id:
            raise ValueError
        return rank, deadline, item_id
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValidationFailed("cursor очереди некорректен") from None


class HumanQueueMixin(SpaceBase):
    async def _human_queue(
        self,
        *,
        kind: QueueKind | None = None,
        service: str | None = None,
        urgency: QueueUrgency | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> HumanQueuePage:
        if not 1 <= limit <= 200:
            raise ValidationFailed("limit: от 1 до 200")
        if kind is not None and kind not in (
            "challenge",
            "requirement",
            "manual_review",
            "drift",
            "quarantine",
        ):
            raise ValidationFailed("неизвестный kind очереди")
        if urgency is not None and urgency not in _URGENCY_RANK:
            raise ValidationFailed("неизвестная urgency очереди")
        async with self._tx(own=True) as tx:
            now = await tx.now()
            facts = await tx.attention_rows()
        all_items = [item for fact in facts for item in _items(fact, now, self.hub.service(fact.service))]
        counts = dict(
            sorted(
                (value, sum(item.kind == value for item in all_items))
                for value in (
                    "challenge",
                    "requirement",
                    "manual_review",
                    "drift",
                    "quarantine",
                )
            )
        )
        items = [
            item
            for item in all_items
            if (kind is None or item.kind == kind)
            and (service is None or item.service == service)
            and (urgency is None or item.urgency == urgency)
        ]
        items.sort(key=_sort_key)
        if after is not None:
            cursor = _decode_cursor(after)
            items = [item for item in items if _sort_key(item) > cursor]
        page = items[: limit + 1]
        visible = tuple(page[:limit])
        return HumanQueuePage(
            items=visible,
            next_cursor=_encode_cursor(visible[-1]) if len(page) > limit and visible else None,
            counts=counts,
        )
