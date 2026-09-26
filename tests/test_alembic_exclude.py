"""P0.4: exclude_identity_hub исключает объекты библиотеки из autogenerate приложения."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Index, Integer, MetaData, Table

from identity_hub.alembic import exclude_identity_hub
from identity_hub.postgres import metadata

pytestmark = pytest.mark.unit


def test_library_tables_and_indexes_are_excluded() -> None:
    table = metadata.tables["ih_identities"]
    assert exclude_identity_hub(table, table.name, "table", False, None) is False
    index = next(iter(table.indexes))
    assert exclude_identity_hub(index, index.name, "index", False, None) is False
    column = table.c.id
    assert exclude_identity_hub(column, "id", "column", False, None) is False


def test_application_objects_are_kept() -> None:
    app = Table("farm_accounts", MetaData(), Column("id", Integer, primary_key=True))
    index = Index("ix_farm", app.c.id)
    assert exclude_identity_hub(app, app.name, "table", False, None) is True
    assert exclude_identity_hub(index, index.name, "index", False, None) is True
    assert exclude_identity_hub(app.c.id, "id", "column", False, None) is True
