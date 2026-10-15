"""Командная строка `identity-hub` (ТЗ P9.2)."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import cast
from urllib.parse import urlsplit

from identity_hub.errors import IdentityHubError, describe_error, exit_code
from identity_hub.hub import IdentityHub
from identity_hub.metrics import record_metric

Factory = Callable[[], IdentityHub | Awaitable[IdentityHub]]

logger = logging.getLogger("identity_hub")

DATABASE_URL_ENV = "IDENTITY_HUB_DATABASE_URL"
# прямой адрес PostgreSQL для миграций, когда приложение ходит через PgBouncer в режиме транзакций
MIGRATE_URL_ENV = "IDENTITY_HUB_MIGRATE_DATABASE_URL"
DAEMON_BACKOFF_BASE = 1.0  # пауза после первого сбоя тика, секунды; дальше удваивается
DAEMON_BACKOFF_MAX = 60.0
MAINTENANCE_INTERVAL = 1.0


class _UsageError(Exception):
    """Ошибка аргументов командной строки: код 2 и справка argparse."""


class _FactoryError(Exception):
    """Фабрика хаба завершилась ошибкой; текст исключения фабрики не сохраняется (R2-S18)."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="identity-hub")
    parser.add_argument("--version", action="store_true", help="показать версию")
    sub = parser.add_subparsers(dest="command")

    migrate = sub.add_parser("migrate", help="применить миграции Identity Hub")
    migrate.add_argument(
        "--url",
        help=f"URL базы; пароль в аргументе виден в списке процессов — надёжнее {DATABASE_URL_ENV}",
    )
    migrate.add_argument("--db-schema")
    migrate.add_argument("--revision", default="head")

    keygen = sub.add_parser("keygen", help="создать мастер-ключ для IDENTITY_HUB_KEK")
    keygen.add_argument("--key-id", default="k1", help="идентификатор ключа в кольце")

    dispatcher = sub.add_parser("dispatcher", help="запустить диспетчер событий")
    _factory_argument(dispatcher)
    dispatcher.add_argument("--once", action="store_true", help="обработать видимую очередь и выйти")

    maintenance = sub.add_parser("maintenance", help="запустить фоновые задачи обслуживания")
    _factory_argument(maintenance)
    maintenance.add_argument("--once", action="store_true", help="один полный цикл и выход")

    doctor = sub.add_parser("doctor", help="проверить денормализацию и счётчики")
    _factory_argument(doctor)
    doctor.add_argument("--fix", action="store_true", help="исправить найденные расхождения")
    doctor_sub = doctor.add_subparsers(dest="doctor_command")
    renormalize = doctor_sub.add_parser(
        "renormalize", help="пересчитать normalized_ref после смены normalize_ref_version сервиса"
    )
    _factory_argument(renormalize, inherited=True)
    renormalize.add_argument("--service", required=True, help="имя сервиса")
    renormalize.add_argument("--batch-size", type=int, default=1000)
    rekey = doctor_sub.add_parser(
        "rekey-fingerprints", help="пересчитать отпечатки прокси активным ключом отпечатков"
    )
    _factory_argument(rekey, inherited=True)
    rekey.add_argument("--batch-size", type=int, default=500)

    rotate = sub.add_parser("rotate-key", help="переобернуть DEK активным мастер-ключом")
    _factory_argument(rotate)
    rotate.add_argument("--batch-size", type=int, default=500)

    secrets = sub.add_parser("secrets", help="обслуживание хранения credentials и сессий")
    _factory_argument(secrets)
    secrets_sub = secrets.add_subparsers(dest="secrets_command")
    convert = secrets_sub.add_parser("convert", help="перевести credentials и сессии между форматами")
    _factory_argument(convert, inherited=True)
    convert.add_argument("--to", choices=("plaintext", "encrypted"), required=True)
    convert.add_argument("--space", help="только указанное пространство")
    convert.add_argument("--batch", type=int, default=500)

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
    rollups.add_argument(
        "--force",
        action="store_true",
        help="перестроить диапазон старше срока хранения событий: его история будет стёрта",
    )
    rollups.add_argument("--before", help="необязательная граница ISO 8601 для --purge-minutes")
    _yes_argument(rollups)

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
    _yes_argument(snapshots)
    return parser


def _factory_argument(parser: argparse.ArgumentParser, *, inherited: bool = False) -> None:
    parser.add_argument(
        "--factory",
        # у вложенной команды значение по умолчанию не должно затирать `--factory` родительской
        default=argparse.SUPPRESS if inherited else os.environ.get("IDENTITY_HUB_FACTORY"),
        help="фабрика IdentityHub в формате package.module:callable (или IDENTITY_HUB_FACTORY)",
    )


def _yes_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--yes", action="store_true", help="подтвердить команду, которая удаляет или перезаписывает данные"
    )


def _check_arguments(args: argparse.Namespace) -> None:
    """Проверить и разобрать аргументы до подключения к базе; ошибки — только `_UsageError`."""
    command = args.command
    if command == "migrate":
        if not _migrate_url(args):
            raise _UsageError(f"укажите --url, {MIGRATE_URL_ENV} или {DATABASE_URL_ENV}")
        return
    if command == "keygen":
        return
    if command == "rotate-key" and args.batch_size < 1:
        raise _UsageError("--batch-size должен быть положительным")
    if command == "secrets":
        if args.secrets_command != "convert":
            raise _UsageError("укажите подкоманду secrets convert")
        if args.batch < 1:
            raise _UsageError("--batch должен быть положительным")
    if (
        command == "doctor"
        and args.doctor_command in ("renormalize", "rekey-fingerprints")
        and args.batch_size < 1
    ):
        raise _UsageError("--batch-size должен быть положительным")
    destructive: str | None = None
    if command == "rollups":
        if args.rebuild:
            if not args.space or not args.start or not args.end:
                raise _UsageError("--rebuild требует --space, --start и --end")
            args.start = _aware_datetime("--start", args.start)
            args.end = _aware_datetime("--end", args.end)
            destructive = "rollups --rebuild заменяет корзины диапазона"
        else:
            destructive = "rollups --purge-minutes удаляет минутные корзины"
        args.before = _aware_datetime("--before", args.before) if args.before else None
    if command == "snapshots":
        args.before = _aware_datetime("--before", args.before) if args.before else None
        if args.purge:
            destructive = "snapshots --purge удаляет снимки состояния"
    if destructive and not args.yes:
        raise _UsageError(f"{destructive}; для подтверждения добавьте --yes")


def _load_factory(spec: str | None) -> Factory:
    if not spec or ":" not in spec:
        raise _UsageError("укажите --factory package.module:callable")
    module_name, attribute = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name and (module_name == exc.name or module_name.startswith(f"{exc.name}.")):
            raise _UsageError(f"модуль фабрики {module_name} не найден") from None
        raise _FactoryError(f"импорт модуля фабрики: {describe_error(exc)}") from None
    except Exception as exc:
        # текст чужого исключения не печатается: в нём бывают URL с паролем и ключи
        raise _FactoryError(f"импорт модуля фабрики: {describe_error(exc)}") from None
    value = getattr(module, attribute, None)
    if not callable(value):
        raise _UsageError(f"{spec}: в модуле нет вызываемого объекта {attribute}")
    return cast(Factory, value)


async def _hub(spec: str | None) -> IdentityHub:
    factory = _load_factory(spec)
    try:
        value = factory()
        hub = await value if inspect.isawaitable(value) else value
    except IdentityHubError:
        raise
    except Exception as exc:
        raise _FactoryError(describe_error(exc)) from None
    if not isinstance(hub, IdentityHub):
        raise _FactoryError("фабрика должна вернуть IdentityHub")
    await hub.start()
    return hub


async def _daemon(
    hub: IdentityHub, name: str, tick: Callable[[], Awaitable[object]], interval: float
) -> None:
    """Бесконечный цикл фонового процесса: сбой тика не завершает процесс (R2-F16).

    Ошибка логируется без текста исключения, пауза перед повтором удваивается до
    `DAEMON_BACKOFF_MAX`. Успешный тик или долгая работа до сбоя возвращают паузу к началу.
    """
    failures = 0
    while True:
        started = hub.clock.monotonic()
        try:
            await tick()
        except Exception as exc:
            if hub.clock.monotonic() - started > DAEMON_BACKOFF_MAX:
                failures = 0  # до сбоя цикл долго работал: это новая неполадка
            failures += 1
            pause = min(DAEMON_BACKOFF_MAX, DAEMON_BACKOFF_BASE * 2 ** min(failures - 1, 16))
            logger.error("%s: сбой цикла (%s), повтор через %.0f с", name, describe_error(exc), pause)
            record_metric(
                hub.metrics,
                "counter",
                "identity_hub_failures_total",
                space="",
                service="",
                scope=name,
                reason="daemon_tick",
            )
        else:
            failures = 0
            pause = interval
        await hub.clock.sleep(pause)


def _migrate_url(args: argparse.Namespace) -> str | None:
    """Адрес для миграций: `--url`, затем отдельный прямой адрес, затем общий адрес базы."""
    return args.url or os.environ.get(MIGRATE_URL_ENV) or os.environ.get(DATABASE_URL_ENV)


def _keygen(args: argparse.Namespace) -> int:
    from identity_hub.crypto import generate_key

    print(generate_key(args.key_id))  # ключ идёт только в stdout: его забирает вызывающий
    return 0


async def _migrate(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    from identity_hub.postgres import migrate as migrations

    if args.url and _has_password(args.url):
        print(
            "предупреждение: пароль в --url виден в списке процессов; "
            f"передавайте адрес базы через {DATABASE_URL_ENV}",
            file=sys.stderr,
        )
    engine = create_async_engine(_migrate_url(args) or "")
    try:
        await migrations.migrate(engine, db_schema=args.db_schema, revision=args.revision)
    finally:
        await engine.dispose()
    return 0


def _has_password(url: str) -> bool:
    try:
        return bool(urlsplit(url).password)
    except ValueError:
        return False


async def async_main(args: argparse.Namespace) -> int:
    if args.command == "migrate":
        return await _migrate(args)
    if args.command == "keygen":
        return _keygen(args)
    hub = await _hub(args.factory)
    try:
        return await _run(hub, args)
    finally:
        await hub.aclose()


async def _run(hub: IdentityHub, args: argparse.Namespace) -> int:
    if args.command == "dispatcher":
        if args.once:
            print(await hub.dispatcher.drain())
            return 0
        await _daemon(hub, "dispatcher", hub.dispatcher.run, 0.0)
    if args.command == "maintenance":
        if args.once:
            maintenance_report = await hub.maintenance.run_all()
            print(maintenance_report.total)
            return 0
        await _daemon(hub, "maintenance", hub.maintenance.run_due, MAINTENANCE_INTERVAL)
    if args.command == "doctor":
        if args.doctor_command == "renormalize":
            return await _renormalize(hub, args)
        if args.doctor_command == "rekey-fingerprints":
            return await _rekey_fingerprints(hub, args)
        issues = await hub.doctor.check(fix=args.fix)
        for issue in issues:
            marker = "fixed" if args.fix else "issue"
            print(f"{marker}: {issue.space} {issue.kind} {issue.entity_id}")
        # строки под неактивным ключом --fix не исправляет: это делает rotate-key
        inactive = await hub.doctor.inactive_keys()
        for table, keys in sorted(inactive.items()):
            for key_id, count in sorted(keys.items()):
                print(f"issue: inactive-key {table} {key_id} {count}")
        plaintext = await hub.doctor.plaintext_secrets()
        if plaintext:
            from identity_hub.hub import PLAINTEXT_SECRET_WARNING

            print(f"warning: {PLAINTEXT_SECRET_WARNING}: {sum(plaintext.values())}")
        return 1 if inactive or (issues and not args.fix) else 0
    if args.command == "rotate-key":
        rotation_report = await hub.doctor.rotate_key(limit=args.batch_size)
        print(rotation_report.total)
        for table, keys in sorted(rotation_report.remaining.items()):
            for key_id, count in sorted(keys.items()):
                print(f"remaining: {table} {key_id} {count}")
        for row in rotation_report.unreadable:
            print(f"unreadable: {row.table} {row.id}")
        hidden = rotation_report.unreadable_total - len(rotation_report.unreadable)
        if hidden > 0:
            print(f"unreadable: ещё {hidden}")
        # остаток под прежним ключом: удалять его из конфигурации нельзя
        return 0 if rotation_report.complete else 1
    if args.command == "secrets":
        report = await hub.doctor.convert_secrets(to=args.to, space=args.space, batch=args.batch)
        print(report.total)
        return 0
    if args.command == "rollups":
        if args.rebuild:
            print(await hub._rollups.rebuild(args.space, args.start, args.end, force=args.force))
            return 0
        print(await hub._rollups.purge_minutes(before=args.before))
        return 0
    if args.command == "snapshots":
        if args.backfill_current:
            print(await hub._state_snapshots.backfill_current())
            return 0
        print(await hub._state_snapshots.purge(before=args.before))
        return 0
    raise _UsageError("укажите команду")


async def _renormalize(hub: IdentityHub, args: argparse.Namespace) -> int:
    report = await hub.doctor.renormalize(args.service, batch_size=args.batch_size)
    print(f"updated: {report.updated}")
    # только идентификаторы: значения ref (телефоны, email) в вывод не попадают
    for group in report.collisions:
        print("collision: " + " ".join(str(identity_id) for identity_id in group))
    for identity_id in report.invalid:
        print(f"invalid: {identity_id}")
    return 0 if report.complete else 1


async def _rekey_fingerprints(hub: IdentityHub, args: argparse.Namespace) -> int:
    report = await hub.doctor.rekey_fingerprints(batch_size=args.batch_size)
    print(f"updated: {report.updated}")
    for table, count in sorted(report.value_updated.items()):
        print(f"value-updated[{table}]: {count}")
    for proxy_id in report.conflicts:
        print(f"conflict: {proxy_id}")
    for proxy_id in report.unreadable:
        print(f"unreadable: {proxy_id}")
    for row in report.value_unreadable:
        print(f"value-unreadable[{row.table}]: {row.id}")
    return 0 if report.complete else 1


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
        _check_arguments(args)
        return asyncio.run(async_main(args))
    except _UsageError as exc:
        # только свои сообщения о разборе аргументов: исключения из кода сюда не попадают
        parser.error(str(exc))
    except IdentityHubError as exc:
        # код и сообщение без трассировки: сообщения ошибок не содержат секретов (S-8, IMP-13)
        print(f"error[{exc.code}]: {exc}", file=sys.stderr)
        return exit_code(exc)
    except _FactoryError as exc:
        print(f"error[factory_failed]: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # текст и трассировка — только в отладочный лог: в сообщении драйвера бывают URL и SQL
        logger.debug("identity-hub %s: необработанная ошибка", args.command, exc_info=True)
        print(f"error[internal]: {describe_error(exc)}", file=sys.stderr)
        return 1
    return 2


def _aware_datetime(option: str, value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _UsageError(f"{option}: ожидается время в формате ISO 8601") from None
    if parsed.tzinfo is None:
        raise _UsageError(f"{option}: время должно содержать timezone offset")
    return parsed
