"""Stable HTTP contracts shared by controllers and generated clients."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]
Cursor = Annotated[str, StringConstraints(min_length=8, max_length=2048)]
SortField = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
IdempotencyKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]


class ContractModel(BaseModel):
    """Forbid silent transport drift in every public DTO."""

    model_config = ConfigDict(extra="forbid")


class AdminErrorCode(StrEnum):
    AUTH_REQUIRED = "auth_required"
    ACCESS_DENIED = "access_denied"
    NOT_FOUND = "not_found"
    VALIDATION_FAILED = "validation_failed"
    CONFLICT = "conflict"
    REVISION_MISMATCH = "revision_mismatch"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    RATE_LIMITED = "rate_limited"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    INTERNAL_ERROR = "internal_error"


class ErrorBody(ContractModel):
    code: AdminErrorCode
    message: str
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    request_id: str


class ErrorEnvelope(ContractModel):
    error: ErrorBody


class ResourceEnvelope[ItemT](ContractModel):
    data: ItemT


class CollectionEnvelope[ItemT](ContractModel):
    items: list[ItemT]
    next_cursor: str | None = None
    total: int | None = None
    total_is_estimate: bool = False


class OperationStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class OperationEnvelope[ResultT](ContractModel):
    operation_id: UUID
    status: OperationStatus
    accepted_at: datetime
    result: ResultT | None = None


class MutationContext(ContractModel):
    reason: Reason
    expected_revision: int | None = Field(default=None, ge=0)
    dry_run: bool = False


class Confirmation(ContractModel):
    """Explicit acknowledgement for commands with irreversible consequences."""

    confirmed: Literal[True]
    consequence_codes: list[SortField] = Field(default_factory=list, max_length=20)


class ConfirmedMutationContext(MutationContext):
    confirmation: Confirmation


class MutationHeaders(ContractModel):
    idempotency_key: IdempotencyKey = Field(alias="Idempotency-Key")


class FilterOperator(StrEnum):
    EQ = "eq"
    IN = "in"
    GTE = "gte"
    LTE = "lte"


class FilterClause(ContractModel):
    field: SortField
    operator: FilterOperator = FilterOperator.EQ
    values: list[str] = Field(min_length=1, max_length=100)


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class PageQuery(ContractModel):
    cursor: Cursor | None = None
    limit: int = Field(default=50, ge=1, le=200)
    sort: SortField = "created_at"
    direction: SortDirection = SortDirection.DESC


class CursorPayload(ContractModel):
    """Stable cursor: the selected sort value plus UUID tie-breaker."""

    sort: SortField
    value: str
    id: UUID


class NumericCursorPayload(ContractModel):
    """Stable cursor for journals with a numeric primary-key tie-breaker."""

    sort: SortField
    value: str
    id: int = Field(ge=0)


def encode_cursor(payload: CursorPayload) -> str:
    raw = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> CursorPayload:
    if not 8 <= len(value) <= 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid cursor encoding")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return CursorPayload.model_validate_json(raw)
    except (ValueError, json.JSONDecodeError):
        raise ValueError("invalid cursor payload") from None


def encode_numeric_cursor(payload: NumericCursorPayload) -> str:
    raw = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_numeric_cursor(value: str) -> NumericCursorPayload:
    if not 8 <= len(value) <= 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid cursor encoding")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return NumericCursorPayload.model_validate_json(raw)
    except (ValueError, json.JSONDecodeError):
        raise ValueError("invalid cursor payload") from None


class TimeWindow(StrEnum):
    MINUTES_15 = "15m"
    HOUR_1 = "1h"
    HOURS_6 = "6h"
    HOURS_24 = "24h"
    DAYS_7 = "7d"
    DAYS_30 = "30d"
    CUSTOM = "custom"


class BucketSize(StrEnum):
    MINUTE_1 = "1m"
    MINUTES_5 = "5m"
    MINUTES_15 = "15m"
    HOUR_1 = "1h"
    DAY_1 = "1d"


class TimeRange(ContractModel):
    window: TimeWindow
    bucket: BucketSize
    start: datetime | None = None
    end: datetime | None = None
    timezone: str

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        has_bounds = self.start is not None or self.end is not None
        if self.window == TimeWindow.CUSTOM and (self.start is None or self.end is None):
            raise ValueError("custom window requires start and end; preset window forbids them")
        if self.window != TimeWindow.CUSTOM and has_bounds:
            raise ValueError("custom window requires start and end; preset window forbids them")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class SeriesPoint(ContractModel):
    at: datetime
    value: float


class Series(ContractModel):
    metric: str
    dimension: str | None = None
    points: list[SeriesPoint]
    previous_value: float | None = None
    coverage: Literal["complete", "partial"] = "complete"


class BreakdownItem(ContractModel):
    key: str
    value: float
    share: float = Field(ge=0, le=1)


class Breakdown(ContractModel):
    metric: str
    items: list[BreakdownItem]


class ApiMeta(ContractModel):
    api_version: Literal["v1"] = "v1"
    space_slug: str | None = None
