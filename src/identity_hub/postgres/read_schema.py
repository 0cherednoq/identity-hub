"""Публичная read-схема на SQLAlchemy Core (IMP-12): `hub.read`.

Стабильные selectable для join с таблицами приложения и для `pool.lease(among=select(...))`.
Это отдельные `Table` в собственном `MetaData` с белым списком колонок поверх физических
таблиц `ih_*`: физические ORM-модели остаются приватными и могут меняться, а read-схема
версионируется отдельно (`READ_SCHEMA_VERSION`).

В схеме нет секретных и служебных колонок: шифротекстов, ключей и nonce, token аренды,
fingerprint прокси, fencing, ключей идемпотентности, версий оптимистической блокировки и
отложенных наблюдений. Публичные имена совпадают со словарём API: `identities.c.auth`,
`identities.c.revision`, `proxies.c.group`.

Схема строится на конкретный хаб: при `IdentityHub(db_schema=...)` таблицы получают явную
схему, поэтому собственные запросы приложения работают без `schema_translate_map` и не
переносят таблицы приложения. Только чтение: запись — через `space.*`.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import Column, MetaData, Table

from identity_hub.postgres.models import IdentityHubBase

__all__ = ["READ_SCHEMA_VERSION", "READ_TABLES", "ReadSchema"]

# Меняется только при несовместимом изменении набора, имён или типов публичных колонок.
READ_SCHEMA_VERSION = 4

# Публичное имя таблицы → (физическая таблица, {публичная колонка: физическая колонка}).
READ_TABLES: Mapping[str, tuple[str, Mapping[str, str]]] = {
    "identities": (
        "ih_identities",
        {
            "id": "id",
            "space_id": "space_id",
            "service": "service",
            "external_ref": "external_ref",
            "normalized_ref": "normalized_ref",
            "lifecycle": "lifecycle",
            "standing": "standing",
            "standing_code": "standing_code",
            "standing_until": "standing_until",
            "auth": "auth_state",
            "availability": "availability",
            "availability_until": "availability_until",
            "requirements": "requirements",
            "verified_factors": "verified_factors",
            "tags": "tags",
            "labels": "labels",
            "profile": "profile",
            "attributes": "attributes",
            "note": "note",
            "error_score": "error_score",
            "revision": "revision",
            "created_at": "created_at",
            "updated_at": "updated_at",
        },
    ),
    "leases": (
        "ih_leases",
        {
            "identity_id": "identity_id",
            "space_id": "space_id",
            "lease_id": "lease_id",
            "pool": "pool",
            "holder": "holder",
            "purpose": "purpose",
            "ref": "ref",
            "hold_deadline": "hold_deadline",
            "acquired_at": "acquired_at",
            "heartbeat_at": "heartbeat_at",
            "expires_at": "expires_at",
        },
    ),
    "lease_history": (
        "ih_lease_history",
        {
            "lease_id": "lease_id",
            "space_id": "space_id",
            "identity_id": "identity_id",
            "pool": "pool",
            "holder": "holder",
            "purpose": "purpose",
            "ref": "ref",
            "hold_deadline": "hold_deadline",
            "proxy_id": "proxy_id",
            "acquired_at": "acquired_at",
            "last_heartbeat_at": "last_heartbeat_at",
            "released_at": "released_at",
            "duration": "duration",
            "end_reason": "end_reason",
            "operations": "operations",
            "succeeded": "succeeded",
            "failed": "failed",
            "last_error": "last_error",
        },
    ),
    "region_states": (
        "ih_region_states",
        {
            "identity_id": "identity_id",
            "space_id": "space_id",
            "region": "region",
            "key": "key",
            "state": "state",
            "code": "code",
            "until": "until",
            "changed_at": "changed_at",
            "change_source": "change_source",
            "changed_by": "changed_by",
            "change_reason": "change_reason",
            "synced_at": "synced_at",
            "observed_at": "observed_at",
            "raw_status": "raw_status",
            "pinned_until": "pinned_until",
        },
    ),
    "events": (
        "ih_events",
        {
            "event_id": "event_id",
            "space_id": "space_id",
            "type": "type",
            "severity": "severity",
            "service": "service",
            "identity_id": "identity_id",
            "proxy_id": "proxy_id",
            "lease_id": "lease_id",
            "sequence": "sequence",
            "revision": "revision",
            "actor": "actor",
            "data": "data",
            "external": "external",
            "occurred_at": "occurred_at",
        },
    ),
    "proxies": (
        "ih_proxies",
        {
            "id": "id",
            "space_id": "space_id",
            "group": "pool",
            "scheme": "scheme",
            "host": "host",
            "port": "port",
            "status": "status",
            "error_score": "error_score",
            "fail_streak": "fail_streak",
            "next_check_at": "next_check_at",
            "status_changed_at": "status_changed_at",
            "status_source": "status_source",
            "checked_at": "checked_at",
            "pinned_until": "pinned_until",
            "max_identities": "max_identities",
            "bound_count": "bound_count",
            "enabled": "enabled",
            "disabled_reason": "disabled_reason",
            "disabled_by": "disabled_by",
            "disabled_at": "disabled_at",
            "created_at": "created_at",
        },
    ),
    # sticky-привязки аккаунт ↔ прокси: join списка аккаунтов с прокси и обратно
    "proxy_bindings": (
        "ih_bindings",
        {
            "identity_id": "identity_id",
            "space_id": "space_id",
            "proxy_id": "proxy_id",
            "bound_at": "bound_at",
        },
    ),
}


def _table(metadata: MetaData, physical: Table, columns: Mapping[str, str], schema: str | None) -> Table:
    return Table(
        physical.name,
        metadata,
        *(
            Column(physical.c[source].name, physical.c[source].type, key=public)
            for public, source in columns.items()
        ),
        schema=schema,
    )


class ReadSchema:
    """Typed Core selectable только для чтения: `hub.read.identities.c.standing` и т.д.

    Каждый selectable содержит `space_id`: фильтруйте по `(await space.ref()).id`, чтобы не
    читать чужие пространства.
    """

    version = READ_SCHEMA_VERSION

    def __init__(self, db_schema: str | None = None) -> None:
        self.db_schema = db_schema
        self.metadata = MetaData()
        physical = IdentityHubBase.metadata.tables
        built = {
            name: _table(self.metadata, physical[table], columns, db_schema)
            for name, (table, columns) in READ_TABLES.items()
        }
        self.identities: Table = built["identities"]
        self.leases: Table = built["leases"]
        self.lease_history: Table = built["lease_history"]
        self.region_states: Table = built["region_states"]
        self.events: Table = built["events"]
        self.proxies: Table = built["proxies"]
        self.proxy_bindings: Table = built["proxy_bindings"]

    def __repr__(self) -> str:
        return f"ReadSchema(version={self.version}, db_schema={self.db_schema!r})"
