"""Командная строка `identity-hub` (ТЗ P9.2)."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import os
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import cast

from identity_hub.hub import IdentityHub

Factory = Callable[[], IdentityHub | Awaitable[IdentityHub]]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="identity-hub")
    parser.add_argument("--version", action="store_true", help="показать версию")
    sub = parser.add_subparsers(dest="command")

    migrate = sub.add_parser("migrate", help="применить миграции Identity Hub")
    migrate.add_argument("--url", default=os.environ.get("IDENTITY_HUB_DATABASE_URL"))
    migrate.add_argument("--db-schema")
    migrate.add_argument("--revision", default="head")

    dispatcher = sub.add_parser("dispatcher", help="запустить диспетчер событий")
    _factory_argument(dispatcher)
    dispatcher.add_argument("--once", action="store_true", help="обработать видимую очередь и выйти")

    maintenance = sub.add_parser("maintenance", help="запустить фоновые задачи обслуживания")
    _factory_argument(maintenance)
    maintenance.add_argument("--once", action="store_true", help="один полный цикл и выход")

    doctor = sub.add_parser("doctor", help="проверить денормализацию и счётчики")
    _factory_argument(doctor)
    doctor.add_argument("--fix", action="store_true", help="исправить найденные расхождения")

    rotate = sub.add_parser("rotate-key", help="переобернуть DEK активным мастер-ключом")
    _factory_argument(rotate)
    rotate.add_argument("--batch-size", type=int, default=500)

    rollups = sub.add_parser("rollups", help="перестроить или очистить временные роллапы")
    _factory_argument(rollups)
    action = rollups.add_mutually_exclusive_group(required=True)
    action.add_argument("--rebuild", action="store_true", help="перестроить диапазон из ih_events")
    action.add_argument(
        "--purge-minutes", action="store_true", help="удалить минутные корзины старше 48 часов"
    )
    rollups.add_argument("--space", help="slug пространства для --rebuild")
    rollups.add_argument("--start", help="начало диапазона ISO 8601 для --rebuild")
    rollups.add_argument("--end", help="конец диапазона ISO 8601 для --rebuild")
    rollups.add_argument("--before", help="необязательная граница ISO 8601 для --purge-minutes")

    snapshots = sub.add_parser("snapshots", help="создать или очистить почасовые снимки состояния")
    _factory_argument(snapshots)
    snapshot_action = snapshots.add_mutually_exclusive_group(required=True)
    snapshot_action.add_argument(
        "--backfill-current",
        action="store_true",
        help="снять только текущее состояние без синтетической истории",
    )
    snapshot_action.add_argument("--purge", action="store_true", help="удалить старые снимки")
    snapshots.add_argument("--before", help="граница ISO 8601 для --purge")
    return parser


def _factory_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--factory",
        default=os.environ.get("IDENTITY_HUB_FACTORY"),
        help="фабрика IdentityHub в формате package.module:callable (или IDENTITY_HUB_FACTORY)",
    )


def _load_factory(spec: str | None) -> Factory:
    if not spec or ":" not in spec:
        raise ValueError("укажите --factory package.module:callable")
    module_name, attribute = spec.split(":", 1)
    value = getattr(importlib.import_module(module_name), attribute)
    if not callable(value):
        raise TypeError(f"{spec} не является callable")
    return cast(Factory, value)


async def _hub(spec: str | None) -> IdentityHub:
    value = _load_factory(spec)()
    hub = await value if inspect.isawaitable(value) else value
    if not isinstance(hub, IdentityHub):
        raise TypeError("factory должна вернуть IdentityHub")
    await hub.start()
    return hub


async def async_main(args: argparse.Namespace) -> int:
    if args.command == "migrate":
        if not args.url:
            raise ValueError("укажите --url или IDENTITY_HUB_DATABASE_URL")
        from sqlalchemy.ext.asyncio import create_async_engine

        from identity_hub.postgres.migrate import migrate

        engine = create_async_engine(args.url)
        try:
            await migrate(engine, db_schema=args.db_schema, revision=args.revision)
        finally:
            await engine.dispose()
        return 0

    hub = await _hub(args.factory)
    if args.command == "dispatcher":
        if args.once:
            print(await hub.dispatcher.drain())
        else:
            await hub.dispatcher.run()
        return 0
    if args.command == "maintenance":
        if args.once:
            maintenance_report = await hub.maintenance.run_all()
            print(maintenance_report.total)
            return 0
        while True:
            await hub.maintenance.run_due()
            await asyncio.sleep(1)
    if args.command == "doctor":
        issues = await hub.doctor.check(fix=args.fix)
        for issue in issues:
            marker = "fixed" if args.fix else "issue"
            print(f"{marker}: {issue.space} {issue.kind} {issue.entity_id}")
        return 0 if not issues or args.fix else 1
    if args.command == "rotate-key":
        if args.batch_size < 1:
            raise ValueError("--batch-size должен быть положительным")
        total = 0
        while True:
            rotation_report = await hub.doctor.rotate_key(limit=args.batch_size)
            total += rotation_report.total
            if rotation_report.total < args.batch_size:
                break
        print(total)
        return 0
    if args.command == "rollups":
        if args.rebuild:
            if not args.space or not args.start or not args.end:
                raise ValueError("--rebuild требует --space, --start и --end")
            print(
                await hub.rollups.rebuild(
                    args.space,
                    _aware_datetime(args.start),
                    _aware_datetime(args.end),
                )
            )
        return 0
    if args.command == "snapshots":
        if args.backfill_current:
            print(await hub.state_snapshots.backfill_current())
            return 0
        print(await hub.state_snapshots.purge(before=_aware_datetime(args.before) if args.before else None))
        return 0
        print(await hub.rollups.purge_minutes(before=_aware_datetime(args.before) if args.before else None))
        return 0
    raise ValueError("укажите команду")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.version:
        from identity_hub import __version__

        print(__version__)
        return 0
    if args.command is None:
        parser.error("укажите команду")
    try:
        return asyncio.run(async_main(args))
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    return 2


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("время должно содержать timezone offset")
    return parsed
